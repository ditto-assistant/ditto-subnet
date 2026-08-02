//! Cross-encoder reranker — a 1:1 port of the Ditto production cross-encoder
//! retrieval stage.
//!
//! Pipeline: the composite retriever hands us the top-[`RERANK_POOL_SIZE`]
//! candidates ordered by composite score; we score each `(query, doc)` pair
//! with a TinyBERT-L2 INT8 cross-encoder (ONNX), then fuse the cross-encoder
//! ranking with the incoming composite ranking via Reciprocal Rank Fusion
//! (RRF) and truncate to the caller's limit. Fusing (rather than replacing)
//! keeps a strong composite hit from being demoted by a noisy CE score.
//!
//! Fidelity notes (match production exactly):
//!   * tokenization: BERT uncased WordPiece, `[CLS] q [SEP] doc [SEP]`,
//!     token_type_ids 0 for `[CLS]+q+[SEP]` and 1 for `doc+[SEP]`, longest-first
//!     truncation to `max_len` (256).
//!   * doc text: `"User: {prompt}\n\nDitto: {response}"` (Memory::FullTextContent).
//!   * RRF: `ceWeight/(rrfK + ceRank+1) + (1-ceWeight)/(rrfK + compositeRank+1)`,
//!     defaults rrfK=60, ceWeight=0.7, stable sort (ties keep composite order).

use std::sync::Mutex;

use async_trait::async_trait;
use ditto_harness::retrieval::Reranker;
use ditto_harness::types::{Error as HarnessError, Memory, Result as HarnessResult};
use ort::session::Session;
use ort::value::Tensor;
use tokenizers::Tokenizer;

/// Production RRF/CE defaults (Go: `DefaultRRFK`, `DefaultCEWeight`).
pub const DEFAULT_RRF_K: f64 = 60.0;
pub const DEFAULT_CE_WEIGHT: f64 = 0.7;
/// Max (query+doc) token length (Go: `maxLen` default 256).
pub const DEFAULT_MAX_LEN: usize = 256;

/// Cross-encoder reranker over an ONNX model + BERT WordPiece tokenizer.
pub struct CrossEncoderReranker {
    session: Mutex<Session>,
    tokenizer: Tokenizer,
    rrf_k: f64,
    ce_weight: f64,
}

impl CrossEncoderReranker {
    /// Builds the reranker from in-memory ONNX model bytes + a BERT `vocab.txt`
    /// string (one token per line; line number = id). Production defaults.
    /// Used with `include_bytes!`/`include_str!` so the kit is self-contained.
    pub fn from_bytes(onnx: &[u8], vocab_txt: &str) -> anyhow::Result<CrossEncoderReranker> {
        let session = Session::builder()?.commit_from_memory(onnx)?;
        let tokenizer = build_bert_tokenizer(vocab_txt, DEFAULT_MAX_LEN)
            .map_err(|e| anyhow::anyhow!("build tokenizer: {e}"))?;
        Ok(CrossEncoderReranker {
            session: Mutex::new(session),
            tokenizer,
            rrf_k: DEFAULT_RRF_K,
            ce_weight: DEFAULT_CE_WEIGHT,
        })
    }

    /// Scores each doc against the query, returning one raw relevance logit per
    /// doc (higher = more relevant). Single batched ONNX run, padded to the
    /// batch's longest sequence (mask 0 on padding).
    fn score(&self, query: &str, docs: &[String]) -> anyhow::Result<Vec<f32>> {
        if docs.is_empty() {
            return Ok(Vec::new());
        }
        let pairs: Vec<(String, String)> = docs
            .iter()
            .map(|d| (query.to_string(), d.clone()))
            .collect();
        let encodings = self
            .tokenizer
            .encode_batch(pairs, true)
            .map_err(|e| anyhow::anyhow!("tokenize: {e}"))?;

        let batch = encodings.len();
        let seq_len = encodings
            .iter()
            .map(|e| e.get_ids().len())
            .max()
            .unwrap_or(0);

        let mut ids = vec![0i64; batch * seq_len];
        let mut mask = vec![0i64; batch * seq_len];
        let mut types = vec![0i64; batch * seq_len];
        for (b, enc) in encodings.iter().enumerate() {
            let eids = enc.get_ids();
            let emask = enc.get_attention_mask();
            let etypes = enc.get_type_ids();
            for t in 0..eids.len() {
                let off = b * seq_len + t;
                ids[off] = eids[t] as i64;
                mask[off] = emask[t] as i64;
                types[off] = etypes[t] as i64;
            }
        }

        let shape = [batch as i64, seq_len as i64];
        let ids_t = Tensor::from_array((shape, ids))?;
        let mask_t = Tensor::from_array((shape, mask))?;
        let types_t = Tensor::from_array((shape, types))?;

        let mut session = self.session.lock().expect("ce session lock");
        let outputs = session.run(ort::inputs![
            "input_ids" => ids_t,
            "attention_mask" => mask_t,
            "token_type_ids" => types_t,
        ])?;
        let (out_shape, data) = outputs["logits"].try_extract_tensor::<f32>()?;
        // logits shape [batch, 1] -> one score per doc.
        let per_row = if out_shape.len() == 2 {
            (out_shape[1]).max(1) as usize
        } else {
            1
        };
        let scores = (0..batch).map(|b| data[b * per_row]).collect();
        Ok(scores)
    }
}

#[async_trait]
impl Reranker for CrossEncoderReranker {
    async fn rerank(
        &self,
        query: &str,
        pool: Vec<Memory>,
        top_n: usize,
    ) -> HarnessResult<Vec<Memory>> {
        if pool.is_empty() || top_n == 0 {
            let mut p = pool;
            p.truncate(top_n);
            return Ok(p);
        }
        let docs: Vec<String> = pool.iter().map(full_text_content).collect();
        // CE inference is synchronous CPU work; no await is held across the lock.
        let scores = self
            .score(query, &docs)
            .map_err(|e| HarnessError::Other(format!("cross-encoder rerank: {e}")))?;
        let order = fuse_rrf(&scores, self.rrf_k, self.ce_weight);

        let mut by_index: Vec<Option<Memory>> = pool.into_iter().map(Some).collect();
        let mut out = Vec::with_capacity(top_n.min(by_index.len()));
        for idx in order.into_iter().take(top_n) {
            if let Some(m) = by_index[idx].take() {
                out.push(m);
            }
        }
        Ok(out)
    }
}

/// Reorders candidates (given in composite-rank order, best first) by fusing
/// their cross-encoder ranks with their composite ranks via RRF. Returns
/// candidate indices best-first. 1:1 with Go `fuseRRF`.
fn fuse_rrf(ce_scores: &[f32], rrf_k: f64, ce_weight: f64) -> Vec<usize> {
    let n = ce_scores.len();
    if n == 0 {
        return Vec::new();
    }
    // Rank by CE score desc (stable: ties keep composite/ascending-index order).
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&a, &b| {
        ce_scores[b]
            .partial_cmp(&ce_scores[a])
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut ce_rank = vec![0usize; n];
    for (r, &idx) in order.iter().enumerate() {
        ce_rank[idx] = r;
    }
    let mut fused: Vec<(usize, f64)> = (0..n)
        .map(|i| {
            let score = ce_weight * (1.0 / (rrf_k + (ce_rank[i] + 1) as f64))
                + (1.0 - ce_weight) * (1.0 / (rrf_k + (i + 1) as f64));
            (i, score)
        })
        .collect();
    fused.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    fused.into_iter().map(|(i, _)| i).collect()
}

/// Memory doc text for the cross-encoder (Go: `Memory.FullTextContent`).
fn full_text_content(m: &Memory) -> String {
    let user = m.prompt.trim();
    let assistant = m.response.trim();
    match (user.is_empty(), assistant.is_empty()) {
        (false, false) => format!("User: {user}\n\nDitto: {assistant}"),
        (false, true) => format!("User: {user}"),
        (true, false) => format!("Ditto: {assistant}"),
        (true, true) => String::new(),
    }
}

/// Builds a BERT-uncased WordPiece tokenizer with pair post-processing +
/// longest-first truncation, matching HuggingFace `BertTokenizer` (which the Go
/// WordPiece tokenizer is parity-verified against). `vocab_txt` is the
/// `vocab.txt` content (one token per line; line index = token id).
fn build_bert_tokenizer(
    vocab_txt: &str,
    max_len: usize,
) -> Result<Tokenizer, Box<dyn std::error::Error + Send + Sync>> {
    use tokenizers::models::wordpiece::WordPiece;
    use tokenizers::normalizers::bert::BertNormalizer;
    use tokenizers::pre_tokenizers::bert::BertPreTokenizer;
    use tokenizers::processors::template::TemplateProcessing;
    use tokenizers::tokenizer::TruncationParams;

    let vocab: std::collections::HashMap<String, u32> = vocab_txt
        .lines()
        .enumerate()
        .map(|(i, line)| (line.trim_end_matches(['\r', '\n']).to_string(), i as u32))
        .collect();
    let wp = WordPiece::builder()
        .vocab(vocab)
        .unk_token("[UNK]".into())
        .continuing_subword_prefix("##".into())
        .max_input_chars_per_word(100)
        .build()?;

    let mut tok = Tokenizer::new(wp);
    // clean_text, handle_chinese_chars, strip_accents (None -> infer from lowercase), lowercase.
    tok.with_normalizer(Some(BertNormalizer::new(true, true, None, true)));
    tok.with_pre_tokenizer(Some(BertPreTokenizer));
    tok.with_post_processor(Some(
        TemplateProcessing::builder()
            .try_single("[CLS] $A [SEP]")?
            .try_pair("[CLS] $A [SEP] $B:1 [SEP]:1")?
            .special_tokens(vec![("[CLS]", 101), ("[SEP]", 102)])
            .build()?,
    ));
    tok.with_truncation(Some(TruncationParams {
        max_length: max_len,
        strategy: tokenizers::tokenizer::TruncationStrategy::LongestFirst,
        stride: 0,
        direction: tokenizers::tokenizer::TruncationDirection::Right,
    }))?;
    Ok(tok)
}

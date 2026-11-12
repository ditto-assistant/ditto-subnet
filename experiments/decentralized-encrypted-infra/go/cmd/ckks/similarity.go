package main

import (
	"flag"
	"fmt"
	"math"
	"math/rand/v2"
	"sort"
	"sync"
	"time"

	"github.com/tuneinsight/lattigo/v6/circuits/ckks/lintrans"
	"github.com/tuneinsight/lattigo/v6/core/rlwe"
	"github.com/tuneinsight/lattigo/v6/schemes/ckks"
)

// Encrypted query x database of 768-d unit vectors. One N=2^13 ciphertext
// scores 4096 vectors: the query is padded to 1024 and replicated 4 times, and
// the block is a tall 4096x1024 matrix applied with 1024 generalized diagonals.
const (
	simDim = 768
	simPad = 1024
)

type similarityResult struct {
	Experiment      string        `json:"experiment"`
	Synthetic       bool          `json:"synthetic"`
	Host            hostInfo      `json:"host"`
	Mode            string        `json:"mode"`
	Params          adapterParams `json:"params"`
	VectorsPerBlock int           `json:"vectors_per_block"`
	Blocks          int           `json:"blocks"`
	Sizes           struct {
		GaloisKeys          int     `json:"galois_keys"`
		GaloisKeyMiB        float64 `json:"galois_keys_mib"`
		QueryCiphertextKiB  float64 `json:"query_ciphertext_kib"`
		ScoreCiphertextKiB  float64 `json:"score_ciphertext_kib"`
		BlockF32MiB         float64 `json:"block_plaintext_f32_mib"`
		BlockHeldByMinerMiB float64 `json:"block_held_by_miner_mib"`
		ExpansionVsF32      float64 `json:"expansion_vs_f32"`
	} `json:"sizes"`
	Setup struct {
		KeyGenMs         float64 `json:"keygen_ms"`
		BlockPrepMsTotal float64 `json:"block_prep_ms_total"`
	} `json:"setup"`
	Ditto struct {
		EncryptQuery  latency `json:"encrypt_query"`
		DecryptScores latency `json:"decrypt_scores_per_block"`
	} `json:"ditto_side"`
	MinerPerBlock latency `json:"miner_per_block_single_thread"`
	Throughput    struct {
		Workers       int     `json:"workers"`
		BlockEvals    int     `json:"block_evals"`
		WallMs        float64 `json:"wall_ms"`
		VectorsPerSec float64 `json:"vectors_scored_per_sec"`
	} `json:"miner_throughput"`
	Accuracy struct {
		MaxAbsScoreErr float64 `json:"max_abs_score_err"`
		Top10Overlap   float64 `json:"top10_overlap"`
	} `json:"accuracy"`
	SingleThreadMsPer10kVectors float64 `json:"single_thread_ms_per_10k_vectors"`
}

func randUnit(r *rand.Rand) []float64 {
	v := make([]float64, simDim)
	var n float64
	for i := range v {
		v[i] = r.NormFloat64()
		n += v[i] * v[i]
	}
	n = math.Sqrt(n)
	for i := range v {
		v[i] /= n
	}
	return v
}

func topK(scores []float64, k int) []int {
	idx := make([]int, len(scores))
	for i := range idx {
		idx[i] = i
	}
	sort.Slice(idx, func(a, b int) bool { return scores[idx[a]] > scores[idx[b]] })
	return idx[:k]
}

type simBlock struct {
	lt  lintrans.LinearTransformation
	enc *encMatrix
}

func runSimilarity(argv []string) (any, error) {
	fs := flag.NewFlagSet("similarity", flag.ExitOnError)
	mode := fs.String("mode", "plain", "plain: miner holds a public corpus in the clear; encrypted: Ditto-encrypted corpus")
	nBlocks := fs.Int("blocks", 2, "blocks of 4096 synthetic vectors")
	workers := fs.Int("workers", 10, "goroutines for the throughput phase")
	reps := fs.Int("reps", 4, "timed evaluations per block")
	n1 := fs.Int("bsgs-n1", 32, "baby steps for the encrypted-corpus product")
	seed := fs.Uint64("seed", 473, "synthetic data seed")
	_ = fs.Parse(argv)
	if *mode != "plain" && *mode != "encrypted" {
		return nil, fmt.Errorf("unknown -mode %q", *mode)
	}

	logQ := []int{55, 40}
	params, err := ckks.NewParametersFromLiteral(ckks.ParametersLiteral{
		LogN: 13, LogQ: logQ, LogP: []int{61}, LogDefaultScale: 40,
	})
	if err != nil {
		return nil, err
	}
	if params.LogQP() > 218 {
		return nil, fmt.Errorf("logQP %.1f exceeds the 128-bit bound 218 for N=2^13", params.LogQP())
	}
	slots := params.MaxSlots()
	res := similarityResult{Experiment: "sn473-ckks-similarity", Synthetic: true, Host: newHostInfo(), Mode: *mode, VectorsPerBlock: slots, Blocks: *nBlocks}
	res.Params = adapterParams{LogN: 13, LogQ: logQ, LogP: []int{61}, LogDefaultScale: 40, LogQP: params.LogQP(), MaxLevel: params.MaxLevel(), Slots: slots}

	// Synthetic corpus with ten planted near-duplicates of the query.
	r := rand.New(rand.NewPCG(*seed, 0x5a473))
	query := randUnit(r)
	total := slots * *nBlocks
	db := make([][]float64, total)
	for i := range db {
		db[i] = randUnit(r)
	}
	for p := 0; p < 10; p++ {
		v := make([]float64, simDim)
		noise := randUnit(r)
		for j := range v {
			v[j] = query[j] + 0.3*float64(p+1)*noise[j]
		}
		db[r.IntN(total)] = normalized(v)
	}
	want := make([]float64, total)
	for i, v := range db {
		for j := range v {
			want[i] += v[j] * query[j]
		}
	}

	kgen := rlwe.NewKeyGenerator(params)
	sk := kgen.GenSecretKeyNew()
	ecd := ckks.NewEncoder(params)
	enc := rlwe.NewEncryptor(params, sk)
	dec := rlwe.NewDecryptor(params, sk)
	level := params.MaxLevel()

	blocks := make([]simBlock, *nBlocks)
	var galEls []uint64
	tPrep := time.Now()
	heldBytes := 0
	for b := range blocks {
		diags := make(map[int][]float64, simPad)
		for k := 0; k < simPad; k++ {
			d := make([]float64, slots)
			for i := 0; i < slots; i++ {
				if col := (i + k) % simPad; col < simDim {
					d[i] = db[b*slots+i][col]
				}
			}
			diags[k] = d
		}
		if *mode == "encrypted" {
			m, err := newEncMatrix(params, ecd, enc, diags, *n1, level, params.LogMaxSlots())
			if err != nil {
				return nil, err
			}
			blocks[b].enc = m
			heldBytes += m.bytes()
			if b == 0 {
				galEls = rotationElements(params, m.rotations())
			}
			continue
		}
		d := lintrans.Diagonals[float64](diags)
		ltp := lintrans.Parameters{
			DiagonalsIndexList:        d.DiagonalsIndexList(),
			LevelQ:                    level,
			LevelP:                    params.MaxLevelP(),
			Scale:                     rlwe.NewScale(params.Q()[level]),
			LogDimensions:             params.LogMaxDimensions(),
			LogBabyStepGiantStepRatio: 1,
		}
		lt := lintrans.NewTransformation(params, ltp)
		if err := lintrans.Encode(ecd, d, lt); err != nil {
			return nil, err
		}
		blocks[b].lt = lt
		// Encoded diagonals are held in the NTT domain over Q and P.
		for _, p := range lt.Vec {
			heldBytes += p.BinarySize()
		}
		if b == 0 {
			galEls = lintrans.GaloisElements(params, ltp)
		}
	}
	res.Setup.BlockPrepMsTotal = float64(time.Since(tPrep).Microseconds()) / 1e3

	tKey := time.Now()
	rlk := kgen.GenRelinearizationKeyNew(sk)
	gks, gkBytes := galoisKeysFor(params, kgen, sk, galEls)
	res.Setup.KeyGenMs = float64(time.Since(tKey).Microseconds()) / 1e3
	evk := rlwe.NewMemEvaluationKeySet(rlk, gks...)
	res.Sizes.GaloisKeys = len(gks)
	res.Sizes.GaloisKeyMiB = mib(gkBytes)
	f32Block := slots * simDim * 4
	res.Sizes.BlockF32MiB = mib(f32Block)
	res.Sizes.BlockHeldByMinerMiB = mib(heldBytes / *nBlocks)
	res.Sizes.ExpansionVsF32 = float64(heldBytes / *nBlocks) / float64(f32Block)

	// Ditto side: encrypt the replicated query.
	var encT []time.Duration
	var ctQ *rlwe.Ciphertext
	for i := 0; i < 8; i++ {
		t := time.Now()
		v := make([]float64, slots)
		for j := range v {
			if c := j % simPad; c < simDim {
				v[j] = query[c]
			}
		}
		pt := ckks.NewPlaintext(params, level)
		if err := ecd.Encode(v, pt); err != nil {
			return nil, err
		}
		if ctQ, err = enc.EncryptNew(pt); err != nil {
			return nil, err
		}
		encT = append(encT, time.Since(t))
	}
	res.Ditto.EncryptQuery = summarize(encT)
	res.Sizes.QueryCiphertextKiB = float64(ctQ.BinarySize()) / 1024

	evalBlock := func(ev *ckks.Evaluator, b int) (*rlwe.Ciphertext, error) {
		var out *rlwe.Ciphertext
		var err error
		if blocks[b].enc != nil {
			out, err = blocks[b].enc.apply(ev, ctQ)
		} else {
			out, err = lintrans.NewEvaluator(ev).EvaluateNew(ctQ, blocks[b].lt)
		}
		if err != nil {
			return nil, err
		}
		if err = ev.Rescale(out, out); err != nil {
			return nil, err
		}
		return out, nil
	}

	ev := ckks.NewEvaluator(params, evk)
	if _, err := evalBlock(ev, 0); err != nil { // warm-up
		return nil, err
	}
	var perBlock []time.Duration
	outs := make([]*rlwe.Ciphertext, *nBlocks)
	for rep := 0; rep < *reps; rep++ {
		for b := range blocks {
			t := time.Now()
			out, err := evalBlock(ev, b)
			if err != nil {
				return nil, err
			}
			perBlock = append(perBlock, time.Since(t))
			outs[b] = out
		}
	}
	res.MinerPerBlock = summarize(perBlock)
	res.Sizes.ScoreCiphertextKiB = float64(outs[0].BinarySize()) / 1024
	res.SingleThreadMsPer10kVectors = res.MinerPerBlock.P50Ms * 10000 / float64(slots)

	got := make([]float64, 0, total)
	var decT []time.Duration
	for _, out := range outs {
		t := time.Now()
		vals := make([]float64, slots)
		if err := ecd.Decode(dec.DecryptNew(out), vals); err != nil {
			return nil, err
		}
		decT = append(decT, time.Since(t))
		got = append(got, vals...)
	}
	res.Ditto.DecryptScores = summarize(decT)
	for i := range want {
		res.Accuracy.MaxAbsScoreErr = math.Max(res.Accuracy.MaxAbsScoreErr, math.Abs(got[i]-want[i]))
	}
	wantTop := map[int]bool{}
	for _, i := range topK(want, 10) {
		wantTop[i] = true
	}
	hits := 0
	for _, i := range topK(got, 10) {
		if wantTop[i] {
			hits++
		}
	}
	res.Accuracy.Top10Overlap = float64(hits) / 10

	w := max(1, *workers)
	jobs := max(2*w, *nBlocks)
	next := make(chan int, jobs)
	for i := 0; i < jobs; i++ {
		next <- i % *nBlocks
	}
	close(next)
	var wg sync.WaitGroup
	errs := make(chan error, w)
	start := time.Now()
	for j := 0; j < w; j++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			wev := ckks.NewEvaluator(params, evk)
			for b := range next {
				if _, err := evalBlock(wev, b); err != nil {
					errs <- err
					return
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	if err := <-errs; err != nil {
		return nil, err
	}
	wall := time.Since(start)
	res.Throughput.Workers = w
	res.Throughput.BlockEvals = jobs
	res.Throughput.WallMs = float64(wall.Microseconds()) / 1e3
	res.Throughput.VectorsPerSec = float64(jobs*slots) / wall.Seconds()
	return res, nil
}

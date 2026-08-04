package persona

// Reproduce-under-transform audit (v3 Part A1).
//
// The metamorphic twins in questions.go are GENERATOR-chosen: which fact
// becomes a family, and the phrasing set, are fixed in the dataset at
// generation time. A harness that can see the dataset can therefore pre-handle
// the specific twin. This file upgrades that to a POST-COMMIT audit: a share of
// cases are re-asked under a transform the harness cannot see coming, because
// both the selection and the transform are keyed to the block-hash-derived
// dataset seed, which postdates the submission's commit.
//
// Honest scope (keep this framing everywhere; see docs/anti-gaming.md):
// the transform audit defeats SURFACE-BRITTLE strategies — answers keyed to a
// question's exact wording: template dispatchers, trailing-question strippers,
// lookup keyed to the original phrasing, precompute keyed to the original
// question. It does NOT defeat a robust local solver that recomputes the answer
// from the cleartext haystack per question; that solver recomputes correctly
// under the transform too. Making a cleartext-haystack answer model-required is
// not achievable in a deterministic score. That residual is the screener
// behavioral oracle's job plus the per-submission fee. Do not describe this as
// closing the general timeline-solver gap.
//
// Two invariants this file must never break:
//  1. Trustless reproducible scoring. Selection, transformed text, and the
//     transformed expected answer are pure functions of the PUBLIC inputs
//     (seed, case id, transform id). A third party regenerates them from the
//     published seed. No validator secret, no model, no nondeterminism.
//  2. Simple validator. Audit cases are ordinary graded cases; they ride the
//     normal generated suite for the seed. No extra round trip, no new harness
//     protocol, no new answer source.

import (
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// AuditBps is the public per-case audit rate in basis points. Public constant,
// not a tunable secret: every validator and any third party must select the
// same cases.
//
// Raised 1500 -> 2500 after the 2026-07-18 calibration. At 1500 a full run
// produced only ~3.8 audit pairs, of which ~12% were discordant, so a per-run
// verdict rested on roughly one informative pair and could not reach
// significance no matter how the metric was defined. The cost is real and
// bounded: each audit pair spends ONE extra case slot (the base case was
// already in the run), so this moves ~7 of a full run's ~59 memory slots from
// base coverage to audit coverage.
const AuditBps = 2500

// TransformSpace is the size of the transform id space. It is deliberately far
// larger than the number of concrete surface pools below: a transform id maps
// to a COMPOSITION of parts, and the id->parts mapping is itself re-keyed per
// seed (see partIndex), so the enumerable-lexicon problem that the pre-merge
// review flagged on the fixed injection pool does not reappear here. Memorizing
// "transform 41 means X" is worthless because under the next seed transform 41
// composes different parts.
const TransformSpace = 4096

// AuditDomain / TransformDomain are the hash domain separators. They are part
// of the public derivation contract and are mirrored byte-for-byte by the
// validator and the platform; changing either re-pins every audit selection.
const (
	AuditDomain     = "audit-v3"
	TransformDomain = "xform-v3"
)

// TransformClass names what the transform does to the expected answer. It is
// generator-side only: the wire form of an audit case is indistinguishable from
// a normal case, which is required — otherwise a hybrid harness reasons only
// where it knows it is watched.
type TransformClass string

const (
	// ClassInvariance rephrases the question; the expected answer is UNCHANGED.
	// A surface-keyed harness answers the base case and misses this one.
	ClassInvariance TransformClass = "invariance"
	// ClassCovariance shifts the question's semantics (currently: the
	// point-in-time as-of date) so the expected answer CHANGES deterministically,
	// recomputed by the same generator code that produced the base answer. A
	// memorized answer returns the old value and is wrong.
	ClassCovariance TransformClass = "covariance"
)

// AuditTwinPrefix marks an audit case's TwinGroup. The base case and its
// transformed sibling share a group so the scorer can pair them and derive the
// transform-robustness metric with the existing twin plumbing, and so any third
// party can recompute that pairing from the published dataset.
const AuditTwinPrefix = "auditxf-"

// auditHash is THE audit derivation primitive: SHA-256 over the ':'-joined
// public inputs, first 8 bytes big-endian. It deliberately does NOT reuse
// variantIndex (an FNV/multiply mix): this value is re-derived in Python by the
// validator and the platform, so it uses the same SHA-256 ':'-joined convention
// as the on-chain seed derivation (ditto-subnet ditto/validator/onchain_seed.py),
// which is trivial to mirror byte-for-byte in any language. The cross-repo test
// vectors pin the pairing.
func auditHash(seed int64, caseID, domain string) uint64 {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%d:%s:%s", seed, caseID, domain)))
	return binary.BigEndian.Uint64(sum[:8])
}

// AuditSelected reports whether a case is picked for the transform audit.
// Unpredictable at commit time because the seed is derived from a block hash
// that postdates the commit, yet fully reproducible afterward from public data.
func AuditSelected(seed int64, caseID string) bool {
	return auditHash(seed, caseID, AuditDomain)%10000 < AuditBps
}

// AuditTransformID is the transform this case is audited under.
func AuditTransformID(seed int64, caseID string) int {
	return int(auditHash(seed, caseID, TransformDomain) % TransformSpace)
}

// --- invariance surface pools -----------------------------------------------
//
// Every entry must be semantically inert: it may change how the question is
// worded or what surrounds it, but it must never change what is being asked or
// supply any part of the answer. Same discipline as the haystack lead-in and
// trailer pools in surface.go. A reviewer adding an entry here should ask: "if a
// grounded reader answers the base question correctly, does this wording still
// admit exactly that answer?" If not, it belongs in a covariance transform.

// xfContexts are no-op preamble sentences. They pressure a parser that anchors
// on the question being the whole (or the last) utterance.
var xfContexts = []string{
	"",
	"",
	"Switching topics for a second.",
	"Unrelated to what we were just on.",
	"One more thing before I forget.",
	"While I have you.",
	"Quick tangent.",
	"Different subject.",
}

// xfOpeners front the question itself.
var xfOpeners = []string{
	"",
	"",
	"Remind me, ",
	"I forget: ",
	"Quick one: ",
	"Sorry, asking again: ",
	"Can you check: ",
	"Just so I have it right: ",
}

// xfClosers trail the question. None carry a recoverable fact.
var xfClosers = []string{
	"",
	"",
	" Thanks.",
	" No rush.",
	" If you have it handy.",
	" Just checking.",
	" Whenever you get a sec.",
	" Appreciate it.",
}

// partIndex decomposes a transform id into a per-pool index, then rotates that
// index by a (seed, caseID, part)-keyed offset. The rotation is what keeps the
// transform space from degenerating into a small memorizable lexicon: the same
// transform id composes different concrete surfaces under a different seed, so
// there is nothing stable to pre-handle. stride separates the parts so two pools
// do not move in lockstep.
func partIndex(seed int64, caseID, part string, id, stride, n int) int {
	if n <= 0 {
		return 0
	}
	off := int(auditHash(seed, caseID, "part:"+part) % uint64(n))
	return ((id/stride)%n + off) % n
}

// Transform derives the audit sibling of a base question. It returns ok=false
// when the case carries no sound transform (the caller then leaves the case
// alone rather than inventing one — a transform whose expected answer is not
// produced by the existing generator path would break the trustless invariant).
//
// The returned Question is an ORDINARY graded case: it flows through the normal
// grade path and lands in per_case with its own score, carrying no wire-visible
// audit flag. Only the shared TwinGroup ties it to its base, and that field
// already exists on every metamorphic family.
func Transform(p *Plan, base Question, transformID int) (Question, TransformClass, bool) {
	// Covariance first where it is available. Invariance defeats surface
	// brittleness; only covariance makes a MEMORIZED answer actively wrong, so
	// prefer it whenever the case admits a sound one.
	if q, ok := covariantTransform(p, base, transformID); ok {
		return q, ClassCovariance, true
	}
	if q, ok := priorStateTransform(p, base, transformID); ok {
		return q, ClassCovariance, true
	}
	if q, ok := invariantTransform(p, base, transformID); ok {
		return q, ClassInvariance, true
	}
	return Question{}, "", false
}

// priorStateAsk phrases the "value before the latest change" question. %[1]s is
// the attribute noun. Every phrasing must be unambiguous that the PREVIOUS
// state is wanted, because the whole point is that the current value (the one a
// memorizer holds) is the wrong answer here.
var priorStateAsk = []string{
	"Before the most recent change, what was my %[1]s?",
	"What was my %[1]s before the latest one?",
	"What did my %[1]s used to be, just before the current one?",
	"Going one step back in my history, what was my previous %[1]s?",
}

// priorStateTransform is the covariance transform for the knowledge-update
// family, which is far more common in a run than point-in-time is. The base
// case asks for the CURRENT value of an updated attribute; the transform asks
// for the state immediately before it. The expected answer is read off the same
// supersession chain (scalarChain) that produced the base answer, so there is no
// new answer source — only a different index into it.
//
// This is the transform that prices memorization. A harness holding "attribute
// city -> Berlin" answers the base correctly and returns Berlin here too, where
// the answer is the superseded value, and scores 0. A harness that actually
// resolved the update chain has both states and answers correctly.
func priorStateTransform(p *Plan, base Question, transformID int) (Question, bool) {
	if base.Type != QTKnowledgeUpdate || base.Attribute == "" {
		return Question{}, false
	}
	noun := attrNoun[base.Attribute]
	if noun == "" {
		return Question{}, false
	}
	ch := scalarChain(p, base.Attribute)
	if len(ch) < 2 {
		return Question{}, false
	}
	cur, prev := ch[len(ch)-1], ch[len(ch)-2]
	// Only sound if the base case really does ask for cur and the prior state is a
	// genuinely different value; a chain that re-states a value would make the
	// transform's expected answer indistinguishable from the base's.
	if cur.Value != base.Answer || prev.Value == cur.Value {
		return Question{}, false
	}
	q := base
	q.ID = auditCaseID(base.ID)
	q.Text = fmt.Sprintf(priorStateAsk[partIndex(p.Seed, base.ID, "prior", transformID, 1, len(priorStateAsk))], noun)
	q.Answer = prev.Value
	q.Tier = TierHard
	q.Evidence = []string{prev.ID, cur.ID}
	q.TwinGroup = AuditTwinPrefix + base.ID
	// The base case's distractors are the non-self confusables for this attribute
	// and stay valid. The DumpGuard, however, was computed for the base answer and
	// may now contain the expected answer itself; recomputing it is the dump-guard
	// post-pass's job, so drop it rather than carry a stale guard that could zero a
	// correct response.
	q.DumpGuard = nil
	return q, true
}

// covariantTransform shifts a point-in-time question to a DIFFERENT eligible
// as-of boundary on the same update chain. The expected answer is whatever
// state held at that date, recomputed by pitQuestionAt — the same function that
// built the base case — so there is no second answer source to trust. A harness
// that memorized the base date's answer returns the superseded value and is
// wrong; a harness that actually resolves the timeline is unaffected.
func covariantTransform(p *Plan, base Question, transformID int) (Question, bool) {
	if base.Type != QTPointInTime || base.Attribute == "" {
		return Question{}, false
	}
	sc, bs, ok := pitChain(p, base.Attribute)
	if !ok || len(bs) < 2 {
		return Question{}, false // needs an alternate boundary to shift TO
	}
	// The base case's boundary, so we can pick a different one.
	baseIdx := variantIndex(p.Seed, "pit:"+base.Attribute, len(bs))
	alt := (baseIdx + 1 + partIndex(p.Seed, base.ID, "pit", transformID, 1, len(bs)-1)) % len(bs)
	if alt == baseIdx { // defensive; the +1 above should already exclude it
		return Question{}, false
	}
	b := bs[alt]
	q := pitQuestionAt(p, base.Attribute, auditCaseID(base.ID), "xf-pit-ask:"+base.Attribute, sc, b)
	// A covariance transform is only sound if it actually moved the answer.
	// Equal-valued neighbouring states do occur on a chain that re-states a value;
	// grading those as an audit would penalize nothing and dilute the metric.
	if q.Answer == base.Answer {
		return Question{}, false
	}
	q.Attribute = base.Attribute
	q.TwinGroup = AuditTwinPrefix + base.ID
	return q, true
}

// invariantTransform rephrases the question and leaves the expected answer and
// every grading field untouched. The strongest lever is re-drawing the core ask
// from the SAME scalarAsk pool the base was drawn from, guaranteed to be a
// different phrasing; around that it composes an inert preamble, opener, and
// closer. Cases with no attribute-backed ask pool still get the surrounding
// composition, which alone defeats a whole-utterance template match.
func invariantTransform(p *Plan, base Question, transformID int) (Question, bool) {
	core := base.Text
	if core == "" {
		return Question{}, false
	}
	// Re-draw the core ask, guaranteed distinct from the base phrasing.
	if variants := scalarAsk[base.Attribute]; len(variants) >= 2 {
		cur := -1
		for i, v := range variants {
			if v == core {
				cur = i
				break
			}
		}
		if cur >= 0 {
			alt := (cur + 1 + partIndex(p.Seed, base.ID, "ask", transformID, 1, len(variants)-1)) % len(variants)
			core = variants[alt]
		}
	}
	ctx := xfContexts[partIndex(p.Seed, base.ID, "ctx", transformID, 1, len(xfContexts))]
	open := xfOpeners[partIndex(p.Seed, base.ID, "open", transformID, len(xfContexts), len(xfOpeners))]
	close := xfClosers[partIndex(p.Seed, base.ID, "close", transformID, len(xfContexts)*len(xfOpeners), len(xfClosers))]

	if open != "" {
		// An opener makes the question mid-sentence; lower the first letter unless
		// it is "I" or the protected answer value leads the text.
		core = lowerFirstUnlessProtected(core, base.Answer)
	}
	text := open + core + close
	if ctx != "" {
		text = ctx + " " + text
	}
	if strings.TrimSpace(text) == strings.TrimSpace(base.Text) {
		return Question{}, false // composed to a no-op; not a usable audit case
	}

	q := base // copy every grading field: answer, distractors, forbidden, kind, items, dump guard
	q.ID = auditCaseID(base.ID)
	q.Text = text
	q.TwinGroup = AuditTwinPrefix + base.ID
	return q, true
}

// auditCaseID names the transformed sibling. The prefix is generator-internal
// bookkeeping that also appears on the wire as the case id, exactly as every
// other family prefix (q-inv-, q-pit-, q-injtwin-) does. It is NOT an audit
// tell a harness can exploit: knowing a case is audited buys nothing, because
// the harness still cannot predict WHICH cases carry one before the seed is
// pinned, and answering an audit case correctly is precisely answering the
// question correctly. What must stay hidden is the mapping from base to
// transform BEFORE commit, and that is what the block-hash seed provides.
func auditCaseID(baseID string) string {
	return AuditCaseIDPrefix + strings.TrimPrefix(baseID, "q-")
}

// AuditCaseIDPrefix marks the TRANSFORMED half of an audit pair. Both halves
// share a TwinGroup, so this is what tells them apart where that matters.
// Consumers computing transform robustness do NOT need it: the metric is the
// agreement rate within each pair, which is symmetric.
const AuditCaseIDPrefix = "q-xf-"

// SelectAudits picks up to quota audit siblings from the run's selected cases.
// Selection walks the cases in their existing order and takes those the
// public per-case draw marks as audited, so which cases are audited is a pure
// function of (seed, case id) and is reproducible by anyone holding the seed.
// Returns the transformed cases; the caller appends them to the suite.
func SelectAudits(p *Plan, selected []Question, quota int) []Question {
	if quota <= 0 {
		return nil
	}
	var out []Question
	for _, base := range selected {
		if len(out) >= quota {
			break
		}
		// Never audit a case that is itself part of a metamorphic family: those
		// already carry a consistency signal, and a transformed sibling of a twin
		// would double-count one fact in both the twin rate and the robustness rate.
		if base.TwinGroup != "" || base.Abstain {
			continue
		}
		// Never audit an injection case. The invariance composition appends an
		// inert closer, which would put a benign tail AFTER the attack text — and
		// a case whose last clause is innocuous is exactly what a trailing-question
		// stripper answers correctly by accident. injectionTwins already covers
		// framing invariance for this family; see the trailing-stripper red-team
		// gate in gen/redteam_test.go, which catches this if it regresses.
		if base.Type == QTInjection {
			continue
		}
		// Never audit the canary. Transform copies every grading field onto the
		// sibling, including Forbidden (the bait nonce), so an audited canary puts
		// the SAME nonce/bait pair in the suite twice. The scorer's canary
		// disqualifier is charged per leaking case, so one leaking behaviour would
		// be penalised twice for a single breach -- and the canary already carries
		// its own integrity signal, so it needs no robustness sibling.
		//
		// Gated on v4: this changes which cases a seed draws, and v3 is a frozen
		// contract whose bytes must keep regenerating identically.
		if base.Type == QTCanary && p.BenchVersion >= protocol.BenchVersionV4 {
			continue
		}
		if !AuditSelected(p.Seed, base.ID) {
			continue
		}
		q, _, ok := Transform(p, base, AuditTransformID(p.Seed, base.ID))
		if !ok {
			continue
		}
		out = append(out, q)
	}
	return out
}

// AuditQuota is the number of audit slots reserved in a run of n cases. It is
// reserved from the run budget rather than added on top, so an audited run has
// the same case count (and the same cost to a miner) as an unaudited one.
func AuditQuota(n int) int {
	q := n * AuditBps / 10000
	if q < 1 && n >= 8 {
		q = 1 // below this the metric has no signal at all; above it, always audit
	}
	return q
}

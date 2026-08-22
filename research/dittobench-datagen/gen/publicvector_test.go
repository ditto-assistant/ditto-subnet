package gen

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TestKnownVector pins the canonical hash of a fixed seed so any change that
// alters generator output is caught in review. The validators run this same
// module, so this is also the byte-parity anchor between this public repo and the
// private scoring service. The hash is a function of (seed, bench_version): a
// bench_version bump rotates the surface (RotateSeed) and moves it by design.
// Update it only on a deliberate generator change or a bench_version bump.
func TestKnownVector(t *testing.T) {
	const (
		seed = int64(123456789)
		// Deliberate change (audit follow-up item A, bench_version 2): the five
		// audited low-variety tool categories moved from flat template lists to
		// CFG grammar expansion (persona.Expand), which changes both the emitted
		// prompts and the per-case RNG draw counts.
		//
		// Moved again (anti-gaming addendum N2, bench_version 2): the metamorphic
		// invariance twin became a j=3 sibling FAMILY (invarianceTwins) instead of
		// a seed-selected pair, so the run emits one more consistency case and the
		// twin phrasing-set selection changed.
		//
		// Moved again (prod hardening P1+P6, bench_version 2): write-then-read
		// lifecycle chains joined the memory suite (gen/lifecycle.go) and the
		// point-in-time modality joined question derivation, changing the case
		// mix, wave-0 pairs, and RNG draw counts.
		//
		// Moved again (metamorphic multi-family, bench_version 2): question
		// derivation now emits several invariance families (invarianceTwins) and
		// the memory suite selects twinFamiliesFor(n) of them (full=3), so the twin
		// case count and attribute selection changed. This smooths the
		// metamorphic-consistency composite factor from a per-run coin flip.
		//
		// Moved to bench_version 3 (anti-gaming hardening; on-chain scoring went
		// live 2026-07-13 so this is the first post-launch version). The version
		// bump rotates the surface via RotateSeed, and value-recall memory cases
		// now carry a DumpGuard (the user's other current self values) so the
		// grader can zero a whole-self-table answer dump. DumpGuard is part of the
		// auditable answer-key artifact, like DistractorAnswers, never sent to the
		// harness. Further v3 surface changes (needle gating, synthesis types,
		// adversarial distractors) re-pin this hash as they land.
		//
		// Re-pinned (v3 needle gating): the result-usage needle is now served only
		// by the case's needle-bearing tool with a per-seed sentence template and an
		// inline decoy, so NeedleText (part of the artifact) changed.
		//
		// Re-pinned (v3 synthesis anti-shortcut): recurring-topic mentions now use
		// coreference (only the anchor names the topic; follow-ups are oblique) so
		// literal-label counting undercounts, and opinion reversals are conveyed by
		// sentiment rather than a fixed cessation lexicon. Both change haystack
		// surface (RNG draw counts held constant, so timestamps/durations did not
		// shift).
		//
		// Re-pinned (v3 red-team gate): the recurring anchor's acknowledgment turn
		// no longer echoes the full topic label (it stays oblique), so the label
		// appears exactly once in the haystack and a literal-label counter always
		// undercounts. Surface-only change.
		//
		// Re-pinned (v3 adversarial distractor): each run now seeds one NoOp /
		// knowledge-conflict "considered but rejected" mention (KindNegated) whose
		// value becomes a distractor for that attribute's recall, so a similarity
		// retriever that grabs it scores 0. Added via a derived rng + deterministic
		// session, so the main stream (timestamps/durations) is unperturbed.
		//
		// Re-pinned (v3 anti-laundering, this change): three surface-affecting
		// additions. (1) Coined tokens (canary nonce/bait, injection payload,
		// lifecycle read answers) now share one per-seed SHAPE family
		// (persona.CoinShaped) — grammar collision, so a token-shape output
		// scrubber deletes its own answers — changing every coined token's text.
		// (2) The run's injection payload is planted once as an innocuous stored
		// "warranty reference" fact (KindDistractor), a new haystack pair, to
		// defeat the context-membership scrub. (3) An injection-framing invariance
		// family (persona.injectionTwins) joins the memory suite on medium/full
		// runs — one fact attacked via several override framings, one of them an
		// action-bait framing carrying MemoryCase.BaitTool — changing the case mix
		// and RNG draw counts. The coined-token character stream also moved to
		// splitmix64 mixing (the prior LCG low bits were nearly salt-independent,
		// so distinct salts could collide to the same token). The broad single
		// injection loop now also SKIPS the injection-twin family's attribute (it
		// otherwise emitted a byte-identical attack case under a second id).
		//
		// Re-pinned (v3 canary + injection diversification): a SECOND attributed
		// canary decoy nonce is planted (rare-token-dump shortcut now trips a
		// decoy), added as a distractor on the canary question, and three
		// injection templates that embed the real question mid-attack (not
		// trailing) were added to the pool — both change the haystack surface and
		// the injection template selection. The second decoy's entity is drawn to
		// differ from the first decoy's (a small resample loop), which also shifts
		// the RNG stream.
		//
		// Re-pinned (v3 canary decoy session separation): the second canary decoy's
		// session now provably differs from the first's (on a draw collision it
		// steps to the next session; same rng draw count), enforcing the documented
		// cross-session attribution contract. This seed had a collided pair, so its
		// haystack layout moved.
		//
		// Re-pinned (v3 result-usage position-tell fix, 2026-07-18): the bearer's
		// served needle sentence no longer places the needle in a fixed first clause
		// before a fixed "(separately," marker. The needle and inline decoy clauses
		// are now joined by a per-seed connective (needleConnectives) in a per-seed
		// order, and the non-bearer decoy sentence is built from the same two-clause
		// family — so a fixed-position / fixed-marker grep can no longer extract the
		// needle or single out the bearer. NeedleText (part of the artifact) changed.
		//
		// Re-pinned (v3 reproduce-under-transform audit, 2026-07-18): a public,
		// seed-keyed share of each run's cases (persona.AuditBps) is now re-asked
		// under a post-commit transform the harness cannot predict at commit time —
		// an invariance rephrasing (answer unchanged) or a covariance shift (prior
		// state on an update chain, or a different point-in-time boundary, answer
		// recomputed from the same chain). The audit cases are paid for out of the
		// run budget, so the case COUNT is unchanged, but which cases the run
		// carries moved. See persona/transform.go.
		//
		// Re-pinned (v3 dump-guard half-hedge / candidate salt, 2026-07-18): each
		// value-recall case now carries up to persona.saltDistractors additional
		// same-attribute distractors, drawn from the attribute's own pool and
		// verified absent from the rendered haystack, so a parser that narrows to a
		// few candidates and emits them all trips the distractor scan regardless of
		// DumpFloor. Case distractor lists changed; the haystack did not.
		//
		// Re-pinned (v3 injection naturalization, 2026-07-18): injection framings are
		// now COMPOSED per seed from independently-varied parts (authority marker,
		// negation clause, payload demand, question placement) instead of drawn from
		// 13 fixed templates, so the shape space is the product of the part pools
		// rather than their sum. Every injection case's text changed. Across a
		// 40-seed sweep the most common shape now covers 1.2% of injection cases.
		//
		// Re-pinned (v3 cross-user lifecycle probe, 2026-07-18): the isolation layer
		// now carries a write/delete-under-A then read-under-B chain (gen/isolation.go
		// crossUserLifecycle), adding two pairs to the secondary user's haystack and
		// five cases. Lifecycle chains previously carried no user id at all, so a
		// harness with a global saved/deleted map was never exercised across the
		// user boundary.
		//
		// Re-pinned (v3 audit pairing, 2026-07-18): the audited BASE case now carries
		// the same TwinGroup as its transformed sibling. Without it the pair is
		// unrecoverable downstream -- the scorer pairs base to transform through
		// TwinGroup alone, and a group holding only the transform would look
		// trivially self-consistent and measure nothing.
		//
		// Re-pinned (v3 audit rate 1500 -> 2500 bps, 2026-07-18): the 2026-07-18
		// calibration found a full run produced only ~3.8 audit pairs, of which
		// ~12% were discordant, so a verdict rested on about one informative pair
		// and could not reach significance however the metric was framed. Each
		// audit pair costs ONE extra case slot (the base case was already in the
		// run), so this moves ~7 of a full run's ~59 memory slots to audit
		// coverage.
		want = "dfb4fc243d7d3e84bb4e896d5873bbc9bda114e16f5215f913c13adbfbc4a7fe"
	)
	prof, _ := ProfileFor("full")
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV2)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	got, _, err := artifact.SHA256Hex()
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if got != want {
		t.Fatalf("known-vector hash drift for seed %d full:\n got %s\nwant %s", seed, got, want)
	}
}

// TestV3KnownVector publishes the first rotated benchmark contract alongside
// the immutable v2 vector above. Both remain pinned forever.
func TestV3KnownVector(t *testing.T) {
	const (
		seed = int64(123456789)
		// v3 is the ANTI-GAMING release, so this vector covers the hardened
		// generator: the reproduce-under-transform audit, the distractor salt,
		// composed injection framings, and the cross-user lifecycle probe. It was
		// re-pinned when that work landed on top of the v2/v3 contract split; v2
		// above is unaffected, which is the property that split exists to give.
		want = "766183922b5a56725bdf44573fc31adf05355dc80fe9654d436935363fcdb3f2"
	)
	prof, _ := ProfileFor("full")
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV3)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	got, _, err := artifact.SHA256Hex()
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if got != want {
		t.Fatalf("v3 known-vector hash drift for seed %d full:\n got %s\nwant %s", seed, got, want)
	}
}

func TestVersionChangesCanonicalBytes(t *testing.T) {
	prof, _ := ProfileFor("full")
	v2, err := GenerateDataset(42, prof, protocol.BenchVersionV2)
	if err != nil {
		t.Fatal(err)
	}
	v3, err := GenerateDataset(42, prof, protocol.BenchVersionV3)
	if err != nil {
		t.Fatal(err)
	}
	v2Hash, _, _ := v2.SHA256Hex()
	v3Hash, _, _ := v3.SHA256Hex()
	if v2Hash == v3Hash {
		t.Fatal("v2 and v3 produced identical canonical bytes")
	}
	if v2.BenchVersion != 2 || v3.BenchVersion != 3 || v2.GeneratedAt == v3.GeneratedAt {
		t.Fatalf("version provenance not rotated: v2=%+v v3=%+v", v2, v3)
	}
}

// TestV4KnownVector pins the false-positive release alongside the immutable v2
// and v3 vectors above. All three remain pinned forever.
//
// v4 corrects v3 mechanisms that penalized legitimate behaviour. Two of those
// corrections move dataset bytes -- the canary is no longer eligible for a
// transform-audit sibling (which duplicated its nonce/bait pair and let one
// breach be charged twice), and delete INSTRUCTIONS now carry AnswerAcknowledge
// instead of being graded on echoing a noun phrase. Both are gated on the
// version, so v3 above regenerates identically; that is the property the
// contract split exists to give.
func TestV4KnownVector(t *testing.T) {
	const (
		seed = int64(123456789)
		want = "43e90780aa33505661047a2584381f6983875ac4a0eb85d46f83103389748b06"
	)
	prof, _ := ProfileFor("full")
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV4)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	got, _, err := artifact.SHA256Hex()
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if got != want {
		t.Fatalf("v4 known-vector hash drift for seed %d full:\n got %s\nwant %s", seed, got, want)
	}
}

// TestV5KnownVector pins the chat-quality/efficiency contract. The token
// transform lives in the validator; this vector pins the independently rotated
// v5 dataset used by both scored runs and starter-kit calibration.
func TestV5KnownVector(t *testing.T) {
	const (
		seed = int64(123456789)
		// v5 is the ACTIVE (not-yet-frozen) release; re-pin on any deliberate v5
		// generator change. Re-pinned when the conversational-sanity gate, declarative
		// writes, code-mode, scaled volume, multi-hop/temporal-depth hardening, and the
		// high-entropy + metamorphic-twin anti-overfit rework landed, and when this
		// vector switched to the canonical v5 profile (ProfileForVersion) the validator
		// actually serves, rather than the v4-sized ProfileFor.
		want = "ee70387b2470bb72a7ce457cd76187b9d89819016f3d58276f895a55b30a9f1c"
	)
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV5)
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV5)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	got, _, err := artifact.SHA256Hex()
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if got != want {
		t.Fatalf("v5 known-vector hash drift for seed %d full:\n got %s\nwant %s", seed, got, want)
	}
}

// TestV6KnownVector pins the memory-as-data (stored-instruction injection) release.
// v6 administers the same v5 suite plus the plan's Phase-B complexity classes:
// stored-instruction injection / memory-as-data (4.8), multi-query fan-out (4.6),
// non-verbatim computed answers graded by accept-set (4.3), and passive
// cross-session consolidation (4.4). v6 is version-gated, so v5's bytes above are
// untouched (its vector still passes). Re-pin on any deliberate v6 generator change.
func TestV6KnownVector(t *testing.T) {
	const (
		seed = int64(123456789)
		want = "38a0df83a95bdad271f80a271d59d676509290e2fd762683abd960952ff84016"
	)
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV6)
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV6)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	got, _, err := artifact.SHA256Hex()
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if got != want {
		t.Fatalf("v6 known-vector hash drift for seed %d full:\n got %s\nwant %s", seed, got, want)
	}
}

// TestV7KnownVector pins the DIFFICULTY release. v7 keeps the OpenRouter
// gpt-oss-20b execution boundary it already carried and adds a version-gated
// difficulty suite: scaled-up profiles, a sharpened memory-type mix, five new
// reasoning-required memory classes (deep write chains, three-hop joins,
// near-miss abstention, temporal arithmetic, composed stored-instruction
// injection), and four new tool classes (negation-cue restraint, stale-context
// routing, a dependent link chain, and a job-chain+recovery composition). All
// gating is on the version, so v2..v6 bytes above are untouched (their vectors
// still pass). v7 is pre-launch (epoch 2026-11-01), so this vector is re-pinned
// as the suite lands. Re-pin on any deliberate v7 generator change.
func TestV7KnownVector(t *testing.T) {
	const (
		seed = int64(123456789)
		want = "f5f42f7a550e0bfef8ef2b14f810cbbd4b140ca5985e9f0cceaa509689d9e218"
	)
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV7)
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV7)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	got, _, err := artifact.SHA256Hex()
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if got != want {
		t.Fatalf("v7 known-vector hash drift for seed %d full:\n got %s\nwant %s", seed, got, want)
	}
}

// TestV8KnownVector pins the answering-machine-proof release. V8 retains the
// v7 execution envelope while making the tool route depend on fresh seeded
// state and increasing the share of context-bound computed/non-verbatim memory
// cases, including multi-leg trip totals. The public bytes also pin natural
// companion-linked travel memories, warm conversational acknowledgements, and
// linear human stories with minimally anchored multi-record questions. V8 uses
// seeded world facts throughout its memory surface rather than any legacy
// sess-* question or transcript family.
func TestV8KnownVector(t *testing.T) {
	const (
		seed = int64(123456789)
		want = "6a09587706c95b5f61d3e65e0e34b317fc8ce24d0c927c66864d2869c8728e98"
	)
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV8)
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV8)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	got, _, err := artifact.SHA256Hex()
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if got != want {
		t.Fatalf("v8 known-vector hash drift for seed %d full:\n got %s\nwant %s", seed, got, want)
	}
}

// TestV9KnownVector pins the pre-activation family-mix contract. V9 inherits
// the coherent v8 world but selects its tool histogram from an independent
// seed stream, preserves one of every final full-run semantic family after the
// world conversion, and carries set_effort in every public profile. This vector
// may move while the v9 stack is still being assembled, but a change must always
// be explicit and must never repin any v2-v8 vector above.
func TestV9KnownVector(t *testing.T) {
	const (
		seed = int64(123456789)
		want = "b12edd3649dece3af415ad289a24a1a8615b7d906773e718ee637da14cbd541f"
	)
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV9)
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV9)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	got, _, err := artifact.SHA256Hex()
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if got != want {
		t.Fatalf("v9 known-vector hash drift for seed %d full:\n got %s\nwant %s", seed, got, want)
	}
}

// TestV10KnownVector pins the private, pre-activation generator-as-spec
// contract without changing the currently advertised benchmark version.
func TestV10KnownVector(t *testing.T) {
	const (
		seed = int64(123456789)
		want = "04d6f3d9099dd9922f931d9a6f90caffd18e70d041d074986d68752ddf928a0f"
	)
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV10)
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV10)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	got, _, err := artifact.SHA256Hex()
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if got != want {
		t.Fatalf("v10 known-vector hash drift for seed %d full:\n got %s\nwant %s", seed, got, want)
	}
}

// TestV12KnownVector pins the private, pre-activation anti-template-fitting
// substrate contract without changing the currently advertised benchmark
// version. It is the byte-identity guard for v12: any change to the shuffled
// prose substrate, the compositional markers, or the routing cues moves this
// hash and must be a deliberate, reviewed new-contract decision.
func TestV12KnownVector(t *testing.T) {
	const (
		seed = int64(123456789)
		// Re-pinned after v12 dropped the v11 safelisted typo projector (F1/C5:
		// a partial projector hands the adversary a stable clause-verb anchor).
		//
		// Re-pinned again when the v12 parser-divergence canary family landed
		// (gen/divergence.go, countermeasure C11): a bounded share of the v12
		// memory mix is now spent on cases whose surface/template reading diverges
		// from the correct one (negation, retraction, hypothetical, reported
		// speech). They are carved out of the world-question budget, so the total
		// memory envelope is unchanged, but the dataset bytes move. v2..v11 vectors
		// above are untouched — the family is gated on bench_version >= 12.
		//
		// Re-pinned again when the anti-family-compiler families landed
		// (gen/familycompiler.go, countermeasure C12): record-determined operation,
		// family-ambiguity, and counterfactual-pair cases whose required computation
		// is not recoverable from the question surface. Also carved out of the
		// world-question budget, so the total memory envelope is unchanged; the
		// dataset bytes move. v2..v11 vectors above are untouched — the family is
		// gated on bench_version >= 12.
		//
		// Re-pinned again when v12 query programs became business events
		// (universe/v12_contract.go, ditto-subnet#1108/#1109): owner/status/event/
		// client/next-action programs plus a cents coverage floor, and the world
		// mix drops remainder oracles. v2..v11 vectors stay frozen. v12 is not
		// live; this is a pre-activation contract change.
		want = "6774a6c4d2c89f7fe6ec0cf4d91359f381c83cba3903d171d6ad5023ab4173a5"
	)
	prof, _ := ProfileForVersion("full", protocol.BenchVersionV12)
	artifact, err := GenerateDataset(seed, prof, protocol.BenchVersionV12)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	got, _, err := artifact.SHA256Hex()
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if got != want {
		t.Fatalf("v12 known-vector hash drift for seed %d full:\n got %s\nwant %s", seed, got, want)
	}
}

func TestUnsupportedVersionRejected(t *testing.T) {
	prof, _ := ProfileFor("small")
	if _, err := GenerateDataset(42, prof, protocol.BenchVersionV12+1); err == nil {
		t.Fatal("unsupported version accepted")
	}
}

// TestSameSeedSameBytes is the core determinism guarantee: one seed, one artifact.
func TestSameSeedSameBytes(t *testing.T) {
	prof, _ := ProfileFor("full")
	for _, version := range []int{protocol.BenchVersionV2, protocol.BenchVersionV3, protocol.BenchVersionV4, protocol.BenchVersionV5, protocol.BenchVersionV6, protocol.BenchVersionV7, protocol.BenchVersionV8, protocol.BenchVersionV9, protocol.BenchVersionV10, protocol.BenchVersionV11, protocol.BenchVersionV12} {
		artifactA, err := GenerateDataset(42, prof, version)
		if err != nil {
			t.Fatalf("v%d generate a: %v", version, err)
		}
		a, _, err := artifactA.SHA256Hex()
		if err != nil {
			t.Fatalf("v%d hash a: %v", version, err)
		}
		artifactB, err := GenerateDataset(42, prof, version)
		if err != nil {
			t.Fatalf("v%d generate b: %v", version, err)
		}
		b, _, err := artifactB.SHA256Hex()
		if err != nil {
			t.Fatalf("v%d hash b: %v", version, err)
		}
		if a != b {
			t.Fatalf("v%d same seed produced different bytes: %s vs %s", version, a, b)
		}
	}
}

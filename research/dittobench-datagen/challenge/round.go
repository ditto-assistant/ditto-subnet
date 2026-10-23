package challenge

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strings"
)

// RoundContractV1 is the round dataset contract.
//
// A round is the unit of the adversarial matrix. It is anchored on the chain's
// SubnetEpochIndex (one round per Platform-configured number of epochs), and it
// freezes a set of generator columns and one secret seed. Every column's
// generator runs on its own seed derived from it (RoundColumnSeed); the packs
// are bundled into one round dataset, and every panel agent of the round is
// scored on the identical bytes.
const RoundContractV1 = "dittobench-generator-round/v1"

// RoundCaseCategory and RoundFaultCategory label round cases in a score report.
// A fault is a case the harness never answered (transport error or timeout):
// it is excluded from every comparison rather than counted as a miss.
const (
	RoundCaseCategory  = "generator_round"
	RoundFaultCategory = "generator_round_fault"
)

// MaxRoundColumns bounds the columns of one round (counted columns plus one
// challenger).
const MaxRoundColumns = 8

// MaxRoundDatasetBytes bounds one encoded round dataset. It matches the
// private dataset transport limit validators already enforce.
const MaxRoundDatasetBytes = 32 << 20

const (
	roundCommitDomain = "dittobench-generator-round-commit/v1"
	roundSeedDomain   = "dittobench-generator-round-seed/v1"
	columnSeedDomain  = "dittobench-generator-round-column-seed/v1"
)

var columnIDPattern = regexp.MustCompile(`^c[0-9]{1,2}$`)

// RoundCommitment is the public commitment to a round secret:
//
//	sha256(domain ‖ 0x00 ‖ u64be(round) ‖ secret)
//
// Platform publishes it (on Platform and as an on-chain commitment) one round
// before the round opens, and reveals the secret after the privacy window.
// Anyone can then check the secret against the commitment and recompute the
// seed with RoundSeed.
func RoundCommitment(round uint64, secret []byte) string {
	h := sha256.New()
	h.Write([]byte(roundCommitDomain))
	h.Write([]byte{0})
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], round)
	h.Write(b[:])
	h.Write(secret)
	return hex.EncodeToString(h.Sum(nil))
}

// RoundSeed derives a round's generator seed from its committed secret and the
// hash of the block the round opened at:
//
//	sha256(domain ‖ 0x00 ‖ u64be(round) ‖ secret ‖ block_hash)
//
// The secret is committed a round before the opening block exists, so neither
// Platform (which cannot grind the block) nor a miner (who cannot see the
// secret) can predict the seed before the round opens.
func RoundSeed(round uint64, secret []byte, openBlockHash string) (string, error) {
	block, err := hex.DecodeString(strings.TrimPrefix(strings.ToLower(openBlockHash), "0x"))
	if err != nil || len(block) != 32 {
		return "", errors.New("round open block hash must be 32 bytes of hex")
	}
	h := sha256.New()
	h.Write([]byte(roundSeedDomain))
	h.Write([]byte{0})
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], round)
	h.Write(b[:])
	h.Write(secret)
	h.Write(block)
	return hex.EncodeToString(h.Sum(nil)), nil
}

// RoundColumnSeed derives one column's GenerateRequest seed from the round
// seed. Every column runs on its own seed, so the public default generator can
// fill several columns without repeating cases, and no column's cases are a
// function of another column's request.
func RoundColumnSeed(roundSeed, columnID string) string {
	h := sha256.New()
	h.Write([]byte(columnSeedDomain))
	h.Write([]byte{0})
	h.Write([]byte(roundSeed))
	h.Write([]byte{0})
	h.Write([]byte(columnID))
	return hex.EncodeToString(h.Sum(nil))
}

// RoundCaseID is a case's identity inside a round dataset and its score
// report: the column ID and the pack's case ID.
func RoundCaseID(columnID, caseID string) string { return columnID + "/" + caseID }

// RoundDataset is the encoded bundle every panel agent of a round runs.
type RoundDataset struct {
	Contract   string               `json:"contract"`
	RoundIndex int64                `json:"round_index"`
	Seed       string               `json:"seed"`
	Columns    []RoundDatasetColumn `json:"columns"`
}

// RoundDatasetColumn is one generator's pack for the round. PackSHA256 is the
// SHA-256 of the compact pack JSON exactly as encoded in the dataset.
type RoundDatasetColumn struct {
	ColumnID   string          `json:"column_id"`
	PackSHA256 string          `json:"pack_sha256"`
	Pack       json.RawMessage `json:"pack"`
}

// CompactPack returns a pack's canonical compact bytes and their SHA-256.
func CompactPack(raw []byte) ([]byte, string, error) {
	var buf bytes.Buffer
	if err := json.Compact(&buf, bytes.TrimSpace(raw)); err != nil {
		return nil, "", fmt.Errorf("pack is not JSON: %w", err)
	}
	sum := sha256.Sum256(buf.Bytes())
	return buf.Bytes(), hex.EncodeToString(sum[:]), nil
}

// EncodeRoundDataset bundles validated packs into canonical round dataset
// bytes. Each pack must have been generated for RoundColumnSeed(seed, id). Columns are sorted by column ID and every pack is compacted, so the
// same packs always encode to the same bytes and digest.
func EncodeRoundDataset(round int64, seed string, packs map[string][]byte) ([]byte, string, error) {
	if !shaPattern.MatchString(seed) {
		return nil, "", errors.New("round seed must be 64 lowercase hex characters")
	}
	if len(packs) == 0 || len(packs) > MaxRoundColumns {
		return nil, "", fmt.Errorf("round dataset needs 1..%d columns", MaxRoundColumns)
	}
	ids := make([]string, 0, len(packs))
	for id := range packs {
		if !columnIDPattern.MatchString(id) {
			return nil, "", fmt.Errorf("invalid column id %q", id)
		}
		ids = append(ids, id)
	}
	sort.Strings(ids)
	ds := RoundDataset{Contract: RoundContractV1, RoundIndex: round, Seed: seed}
	for _, id := range ids {
		compact, sum, err := CompactPack(packs[id])
		if err != nil {
			return nil, "", fmt.Errorf("column %s: %w", id, err)
		}
		p, err := Decode(compact)
		if err != nil {
			return nil, "", fmt.Errorf("column %s: %w", id, err)
		}
		if p.Generator.Seed != RoundColumnSeed(seed, id) {
			return nil, "", fmt.Errorf("column %s pack was not generated for its column seed", id)
		}
		ds.Columns = append(ds.Columns, RoundDatasetColumn{ColumnID: id, PackSHA256: sum, Pack: compact})
	}
	raw, err := json.Marshal(ds)
	if err != nil {
		return nil, "", err
	}
	if len(raw) > MaxRoundDatasetBytes {
		return nil, "", fmt.Errorf("round dataset exceeds %d bytes", MaxRoundDatasetBytes)
	}
	sum := sha256.Sum256(raw)
	return raw, hex.EncodeToString(sum[:]), nil
}

// RoundColumnPack is a decoded, validated round column.
type RoundColumnPack struct {
	ColumnID string
	Pack     Pack
}

// DecodeRoundDataset verifies round dataset bytes against the digest a lease
// pinned and returns every column's validated pack, in column order.
//
// Every pack must pass the full pack contract, match its recorded digest, and
// echo its column seed (RoundColumnSeed): a dataset whose packs were generated
// for another seed is refused, so a validator never scores a dataset other
// than the round's.
func DecodeRoundDataset(raw []byte, expectedSHA string) (RoundDataset, []RoundColumnPack, error) {
	fail := func(reason string) (RoundDataset, []RoundColumnPack, error) {
		return RoundDataset{}, nil, errors.New("round dataset: " + reason)
	}
	if len(raw) == 0 || len(raw) > MaxRoundDatasetBytes {
		return fail("invalid size")
	}
	sum := sha256.Sum256(raw)
	if !shaPattern.MatchString(expectedSHA) || hex.EncodeToString(sum[:]) != expectedSHA {
		return fail("lease digest mismatch")
	}
	var ds RoundDataset
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&ds); err != nil {
		return fail("invalid JSON")
	}
	if dec.More() {
		return fail("trailing data after the dataset")
	}
	var extra json.RawMessage
	if err := dec.Decode(&extra); err != io.EOF {
		return fail("trailing data after the dataset")
	}
	if ds.Contract != RoundContractV1 || !shaPattern.MatchString(ds.Seed) || ds.RoundIndex < 0 {
		return fail("identity mismatch")
	}
	if len(ds.Columns) == 0 || len(ds.Columns) > MaxRoundColumns {
		return fail("column count out of range")
	}
	out := make([]RoundColumnPack, 0, len(ds.Columns))
	for i, col := range ds.Columns {
		if !columnIDPattern.MatchString(col.ColumnID) || (i > 0 && ds.Columns[i-1].ColumnID >= col.ColumnID) {
			return fail("columns must have unique, sorted ids")
		}
		compact, digest, err := CompactPack(col.Pack)
		if err != nil || digest != col.PackSHA256 || !bytes.Equal(compact, col.Pack) {
			return fail("column " + col.ColumnID + " pack digest mismatch")
		}
		if r := ValidateBytes(compact); !r.Valid {
			return fail(fmt.Sprintf("column %s pack is invalid (%d issues)", col.ColumnID, len(r.Issues)))
		}
		p, err := Decode(compact)
		if err != nil {
			return fail("column " + col.ColumnID + " pack does not decode")
		}
		if p.Generator.Seed != RoundColumnSeed(ds.Seed, col.ColumnID) {
			return fail("column " + col.ColumnID + " pack was not generated for the round seed")
		}
		out = append(out, RoundColumnPack{ColumnID: col.ColumnID, Pack: p})
	}
	return ds, out, nil
}

// RoundColumn is one generator column as the round is scored.
type RoundColumn struct {
	ColumnID string `json:"column_id"`
	// OwnerKey is the column generator's owner family; empty for the public
	// default generator, which nobody owns.
	OwnerKey string `json:"owner_key"`
	// Counted columns enter agent comparisons. The challenger column is
	// scored as a generator but never moves an agent.
	Counted bool `json:"counted"`
	// Cases are the column's cases, with RoundCaseID identities.
	Cases []CaseRef `json:"cases"`
}

// RoundAgent is one panel agent's results for the whole round dataset.
type RoundAgent struct {
	AgentID  string `json:"agent_id"`
	OwnerKey string `json:"owner_key"`
	// Reference agents are subnet-run baselines. Each counts as its own
	// independent solver owner, which a set of colluding miners cannot fake.
	Reference bool              `json:"reference,omitempty"`
	Champion  bool              `json:"champion,omitempty"`
	Results   []PanelCaseResult `json:"results"`
}

// RoundInput is everything ScoreRound needs.
type RoundInput struct {
	// Columns in claim order: an earlier column's fingerprints are claimed for
	// every later column, so a copy of an earlier generator scores duplicates.
	Columns []RoundColumn `json:"columns"`
	Agents  []RoundAgent  `json:"agents"`
	// ClaimedFingerprints are fingerprints earlier rounds already hold.
	ClaimedFingerprints []string `json:"claimed_fingerprints,omitempty"`
}

// ColumnScore is one column's generator score for the round.
type ColumnScore struct {
	ColumnID string    `json:"column_id"`
	Pack     PackScore `json:"pack"`
}

// AgentStanding is one agent's pairwise-neutral standing for the round.
type AgentStanding struct {
	AgentID string `json:"agent_id"`
	Rank    int    `json:"rank"`
	// PairwiseScore is (wins + ties/2) / opponents over every agent it shares
	// at least one neutral case with.
	PairwiseScore float64 `json:"pairwise_score"`
	Wins          int     `json:"wins"`
	Ties          int     `json:"ties"`
	Losses        int     `json:"losses"`
	Opponents     int     `json:"opponents"`
	// CommonMean is the agent's mean score over the common cases: counted
	// columns no panel agent's owner owns (the public default generator and
	// generators of owners with no agent on the panel). Every agent is
	// compared on the identical common set, so it is the neutral tie-break.
	CommonMean  float64 `json:"common_mean"`
	CommonCases int     `json:"common_cases"`
}

// RoundScore is the scored matrix.
type RoundScore struct {
	Columns []ColumnScore   `json:"columns"`
	Agents  []AgentStanding `json:"agents"`
}

// referenceOwner is the owner key a reference agent solves under: unique per
// agent, so two reference baselines are two independent solvers.
func referenceOwner(agentID string) string { return "reference:" + agentID }

// ScoreRound scores one round's agent × generator matrix.
//
// Generator columns are scored with ScoreCases against the whole panel. The
// column owner's own agents never count, and each reference agent counts as
// an independent owner. Columns are scored in input order and each column
// claims its fingerprints for later columns.
//
// Agents are ranked pairwise-neutral. Agents A and B are compared only on the
// cases of counted columns owned by neither A's owner nor B's owner, and only
// on cases neither faulted. A wins the pair when its summed score difference
// over those cases is positive. Ties on pairwise score break on CommonMean,
// the mean over columns no panel owner owns, then on AgentID. A miner who mines both sides therefore cannot
// move their own agent relative to anyone with their own generator: the column
// is excluded from every comparison their agent takes part in.
//
// Agents are processed in AgentID order and cases in column, then case,
// order, so the float sums are identical in Platform's Python port.
func ScoreRound(in RoundInput, params ScoreParams) (RoundScore, error) {
	agents := append([]RoundAgent(nil), in.Agents...)
	sort.Slice(agents, func(i, j int) bool { return agents[i].AgentID < agents[j].AgentID })
	panel := make([]PanelMember, len(agents))
	results := make([]map[string]PanelCaseResult, len(agents))
	for i, a := range agents {
		if a.AgentID == "" || (i > 0 && agents[i-1].AgentID == a.AgentID) {
			return RoundScore{}, fmt.Errorf("round agents need unique, non-empty ids (%q)", a.AgentID)
		}
		owner := a.OwnerKey
		if a.Reference {
			owner = referenceOwner(a.AgentID)
		}
		panel[i] = PanelMember{AgentID: a.AgentID, OwnerKey: owner, Champion: a.Champion, Results: a.Results}
		results[i] = map[string]PanelCaseResult{}
		for _, r := range a.Results {
			if _, dup := results[i][r.CaseID]; dup {
				return RoundScore{}, fmt.Errorf("agent %q has two results for case %q", a.AgentID, r.CaseID)
			}
			results[i][r.CaseID] = r
		}
	}
	seen := map[string]bool{}
	for _, col := range in.Columns {
		if col.ColumnID == "" || seen[col.ColumnID] {
			return RoundScore{}, fmt.Errorf("round columns need unique, non-empty ids (%q)", col.ColumnID)
		}
		seen[col.ColumnID] = true
	}

	out := RoundScore{Columns: make([]ColumnScore, 0, len(in.Columns))}
	claimed := append([]string(nil), in.ClaimedFingerprints...)
	for _, col := range in.Columns {
		ps, err := ScoreCases(col.Cases, ScoreInput{
			GeneratorOwnerKey:   col.OwnerKey,
			Panel:               panel,
			ClaimedFingerprints: claimed,
		}, params)
		if err != nil {
			return RoundScore{}, fmt.Errorf("column %s: %w", col.ColumnID, err)
		}
		out.Columns = append(out.Columns, ColumnScore{ColumnID: col.ColumnID, Pack: ps})
		for _, c := range col.Cases {
			claimed = append(claimed, c.Fingerprint)
		}
	}

	panelOwners := map[string]bool{}
	for _, m := range panel {
		panelOwners[m.OwnerKey] = true
	}
	standings := make([]AgentStanding, len(agents))
	for i, a := range agents {
		standings[i].AgentID = a.AgentID
		var sum float64
		for _, col := range in.Columns {
			if !col.Counted || (col.OwnerKey != "" && panelOwners[col.OwnerKey]) {
				continue
			}
			for _, c := range col.Cases {
				if r, ok := results[i][c.CaseID]; ok && !r.Fault {
					sum += r.Score
					standings[i].CommonCases++
				}
			}
		}
		if standings[i].CommonCases > 0 {
			standings[i].CommonMean = sum / float64(standings[i].CommonCases)
		}
	}
	for i := range agents {
		for j := i + 1; j < len(agents); j++ {
			var diff float64
			shared := 0
			for _, col := range in.Columns {
				if !col.Counted || (col.OwnerKey != "" && (col.OwnerKey == panel[i].OwnerKey || col.OwnerKey == panel[j].OwnerKey)) {
					continue
				}
				for _, c := range col.Cases {
					ri, okI := results[i][c.CaseID]
					rj, okJ := results[j][c.CaseID]
					if !okI || !okJ || ri.Fault || rj.Fault {
						continue
					}
					diff += ri.Score - rj.Score
					shared++
				}
			}
			if shared == 0 {
				continue
			}
			standings[i].Opponents++
			standings[j].Opponents++
			switch {
			case diff > 0:
				standings[i].Wins++
				standings[j].Losses++
			case diff < 0:
				standings[j].Wins++
				standings[i].Losses++
			default:
				standings[i].Ties++
				standings[j].Ties++
			}
		}
	}
	for i := range standings {
		if s := &standings[i]; s.Opponents > 0 {
			s.PairwiseScore = (float64(s.Wins) + float64(s.Ties)/2) / float64(s.Opponents)
		}
	}
	sort.SliceStable(standings, func(i, j int) bool {
		a, b := standings[i], standings[j]
		if a.PairwiseScore != b.PairwiseScore {
			return a.PairwiseScore > b.PairwiseScore
		}
		if a.CommonMean != b.CommonMean {
			return a.CommonMean > b.CommonMean
		}
		return a.AgentID < b.AgentID
	})
	for i := range standings {
		standings[i].Rank = i + 1
	}
	out.Agents = standings
	return out, nil
}

// RoundCaseRefs returns a column pack's cases with round identities.
func RoundCaseRefs(columnID string, p Pack) []CaseRef {
	refs := p.Refs()
	for i := range refs {
		refs[i].CaseID = RoundCaseID(columnID, refs[i].CaseID)
	}
	return refs
}

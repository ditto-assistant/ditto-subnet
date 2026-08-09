package longmemeval

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"sort"
	"strings"
)

const selectionDomain = "ditto-v9-longmemeval-selection-v1"

// Case is the private dataset metadata required by the selector. Reference
// answers and histories never enter selection.
type Case struct {
	QuestionID   string
	QuestionType string
}

type SelectedCase struct {
	QuestionID string     `json:"question_id"`
	Capability Capability `json:"capability"`
}

type Selection struct {
	ProfileChecksum string         `json:"profile_checksum"`
	DatasetSHA256   string         `json:"dataset_sha256"`
	Cases           []SelectedCase `json:"cases"`
	CaseSetDigest   string         `json:"case_set_digest"`
}

// Select deterministically chooses an equal quota from every capability. It
// intentionally has no artifact parameter, ensuring all candidates receive a
// common case set for the same frozen profile.
func Select(profile Profile, cases []Case) (Selection, error) {
	profileChecksum, err := profile.Checksum()
	if err != nil {
		return Selection{}, err
	}
	if len(cases) == 0 {
		return Selection{}, errors.New("dataset contains no cases")
	}

	strata := make(map[Capability][]rankedCase, len(capabilityOrder))
	seen := make(map[string]struct{}, len(cases))
	for _, candidate := range cases {
		if strings.TrimSpace(candidate.QuestionID) == "" {
			return Selection{}, errors.New("question_id must be non-empty")
		}
		if _, ok := seen[candidate.QuestionID]; ok {
			return Selection{}, fmt.Errorf("duplicate question_id %q", candidate.QuestionID)
		}
		seen[candidate.QuestionID] = struct{}{}
		capability, mapErr := capabilityForCase(candidate)
		if mapErr != nil {
			return Selection{}, mapErr
		}
		strata[capability] = append(strata[capability], rankedCase{
			Case: candidate,
			Rank: selectionRank(profileChecksum, profile.SelectionSeed, candidate.QuestionID),
		})
	}

	selected := make([]SelectedCase, 0, profile.CasesPerCapability*len(capabilityOrder))
	for _, capability := range capabilityOrder {
		available := strata[capability]
		if len(available) < profile.CasesPerCapability {
			return Selection{}, fmt.Errorf(
				"capability %q has %d cases, requires %d",
				capability, len(available), profile.CasesPerCapability,
			)
		}
		sort.Slice(available, func(i, j int) bool {
			if available[i].Rank == available[j].Rank {
				return available[i].Case.QuestionID < available[j].Case.QuestionID
			}
			return bytes.Compare(available[i].Rank[:], available[j].Rank[:]) < 0
		})
		for _, item := range available[:profile.CasesPerCapability] {
			selected = append(selected, SelectedCase{
				QuestionID: item.Case.QuestionID,
				Capability: capability,
			})
		}
	}

	selection := Selection{
		ProfileChecksum: profileChecksum,
		DatasetSHA256:   profile.DatasetSHA256,
		Cases:           selected,
	}
	selection.CaseSetDigest = caseSetDigest(selection)
	return selection, nil
}

type rankedCase struct {
	Case Case
	Rank [sha256.Size]byte
}

func capabilityForCase(candidate Case) (Capability, error) {
	if strings.HasSuffix(candidate.QuestionID, "_abs") {
		return CapabilityAbstention, nil
	}
	switch candidate.QuestionType {
	case "single-session-user", "single-session-assistant":
		return CapabilityExtraction, nil
	case "multi-session":
		return CapabilityMultiSessionReasoning, nil
	case "temporal-reasoning":
		return CapabilityTemporalReasoning, nil
	case "knowledge-update":
		return CapabilityKnowledgeUpdate, nil
	case "single-session-preference":
		return CapabilityPreference, nil
	default:
		return "", fmt.Errorf(
			"question %q has unsupported question_type %q",
			candidate.QuestionID, candidate.QuestionType,
		)
	}
}

func selectionRank(profileChecksum string, seed uint64, questionID string) [sha256.Size]byte {
	hash := sha256.New()
	hash.Write([]byte(selectionDomain))
	hash.Write([]byte{0})
	hash.Write([]byte(profileChecksum))
	hash.Write([]byte{0})
	var seedBytes [8]byte
	binary.BigEndian.PutUint64(seedBytes[:], seed)
	hash.Write(seedBytes[:])
	hash.Write([]byte{0})
	hash.Write([]byte(questionID))
	var result [sha256.Size]byte
	copy(result[:], hash.Sum(nil))
	return result
}

func caseSetDigest(selection Selection) string {
	hash := sha256.New()
	hash.Write([]byte("ditto-v9-longmemeval-case-set-v1"))
	hash.Write([]byte{0})
	hash.Write([]byte(selection.ProfileChecksum))
	hash.Write([]byte{0})
	hash.Write([]byte(selection.DatasetSHA256))
	for _, item := range selection.Cases {
		hash.Write([]byte{0})
		hash.Write([]byte(item.Capability))
		hash.Write([]byte{0})
		hash.Write([]byte(item.QuestionID))
	}
	return fmt.Sprintf("%x", hash.Sum(nil))
}

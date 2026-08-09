package gen

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

type classifierExample struct {
	role     string
	family   string
	features []string
}

type categoricalClassifier struct {
	labels       []string
	labelCounts  map[string]int
	featureCount map[string]map[string]int
	featureKinds []map[string]bool
	total        int
}

func trainCategorical(examples []classifierExample, label func(classifierExample) string) categoricalClassifier {
	c := categoricalClassifier{labelCounts: map[string]int{}, featureCount: map[string]map[string]int{}}
	labelSet := map[string]bool{}
	for _, example := range examples {
		value := label(example)
		labelSet[value] = true
		c.labelCounts[value]++
		c.total++
		for len(c.featureKinds) < len(example.features) {
			c.featureKinds = append(c.featureKinds, map[string]bool{})
		}
		for i, feature := range example.features {
			c.featureKinds[i][feature] = true
			key := fmt.Sprintf("%d\x00%s", i, feature)
			if c.featureCount[key] == nil {
				c.featureCount[key] = map[string]int{}
			}
			c.featureCount[key][value]++
		}
	}
	for value := range labelSet {
		c.labels = append(c.labels, value)
	}
	sort.Strings(c.labels)
	return c
}

func (c categoricalClassifier) predict(features []string) string {
	best, bestScore := "", math.Inf(-1)
	for _, label := range c.labels {
		labelN := c.labelCounts[label]
		score := math.Log(float64(labelN+1) / float64(c.total+len(c.labels)))
		for i, feature := range features {
			key := fmt.Sprintf("%d\x00%s", i, feature)
			count := c.featureCount[key][label]
			kinds := len(c.featureKinds[i]) + 1
			score += math.Log(float64(count+1) / float64(labelN+kinds))
		}
		if score > bestScore {
			best, bestScore = label, score
		}
	}
	return best
}

func opaqueBytes(value string) []byte {
	if !uuidPattern.MatchString(value) {
		return nil
	}
	raw, _ := hex.DecodeString(strings.ReplaceAll(value, "-", ""))
	return raw
}

func identifierToken(value string) string {
	if uuidPattern.MatchString(value) {
		return "uuid"
	}
	parts := strings.Split(value, "-")
	if len(parts) >= 2 {
		return parts[0] + "-" + parts[1]
	}
	return value
}

func classifierFeatures(caseID, userID string, position, total int, body []byte) []string {
	caseBytes, userBytes := opaqueBytes(caseID), opaqueBytes(userID)
	caseHead, caseTail, userHead := "legacy", "legacy", "legacy"
	if len(caseBytes) == 16 {
		caseHead = fmt.Sprintf("%x", caseBytes[0]>>4)
		caseTail = fmt.Sprintf("%x", caseBytes[15]>>4)
	}
	if len(userBytes) == 16 {
		userHead = fmt.Sprintf("%x", userBytes[0]>>4)
	}
	quartile := 0
	if total > 0 {
		quartile = min(3, position*4/total)
	}
	var shape map[string]json.RawMessage
	_ = json.Unmarshal(body, &shape)
	keys := make([]string, 0, len(shape))
	for key := range shape {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return []string{
		"case_token=" + identifierToken(caseID),
		"user_token=" + identifierToken(userID),
		"case_head=" + caseHead,
		"case_tail=" + caseTail,
		"user_head=" + userHead,
		fmt.Sprintf("position_quartile=%d", quartile),
		fmt.Sprintf("body_size_bucket=%d", len(body)/16),
		"json_keys=" + strings.Join(keys, ","),
	}
}

func balancedClassifierFixture() ([]protocol.ToolCase, []StagedCase, []protocol.SeedRequest) {
	families := []string{"email-route", "memory-join", "project-calc", "travel-time"}
	roles := []string{PrimaryUser, SecondaryUser}
	var cases []StagedCase
	for _, family := range families {
		for _, role := range roles {
			for i := 0; i < 8; i++ {
				cases = append(cases, StagedCase{
					Case:   protocol.MemoryCase{ID: fmt.Sprintf("%s-%s-%02d", family, role, i), Question: "Which ordinary production value applies?", ExpectedAnswer: "value"},
					UserID: role,
				})
			}
		}
	}
	waves := []protocol.SeedRequest{
		{UserID: PrimaryUser, Pairs: []protocol.MemoryPair{{PairID: "primary-record", SessionID: "primary-session", Timestamp: "2026-01-01T00:00:00Z", Prompt: "ordinary fact", Response: "value"}}},
		{UserID: SecondaryUser, Pairs: []protocol.MemoryPair{{PairID: "secondary-record", SessionID: "secondary-session", Timestamp: "2026-01-01T00:00:00Z", Prompt: "ordinary fact", Response: "value"}}},
	}
	return nil, cases, waves
}

func examplesForProjection(t *testing.T, keyIndex int, projected bool) []classifierExample {
	t.Helper()
	tools, cases, waves := balancedClassifierFixture()
	if !projected {
		examples := make([]classifierExample, 0, len(cases))
		for position, sc := range cases {
			body, err := json.Marshal(protocol.RunRequest{CaseID: sc.Case.ID, SystemPrompt: "help", UserInput: sc.Case.Question, Tools: []protocol.ToolDefinition{}, BenchVersion: protocol.BenchVersionV9, UserID: sc.UserID})
			if err != nil {
				t.Fatal(err)
			}
			examples = append(examples, classifierExample{role: sc.UserID, family: strings.Join(strings.Split(sc.Case.ID, "-")[:2], "-"), features: classifierFeatures(sc.Case.ID, sc.UserID, position, len(cases), body)})
		}
		return examples
	}
	key := sha256.Sum256([]byte(fmt.Sprintf("projection-classifier-key-%03d", keyIndex)))
	p, err := BuildHarnessProjection(537, key[:], protocol.BenchVersionV9, tools, cases, waves)
	if err != nil {
		t.Fatal(err)
	}
	examples := make([]classifierExample, 0, len(p.MemoryCases))
	for position, sc := range p.MemoryCases {
		internalID, err := p.InternalCaseID(sc.Case.ID)
		if err != nil {
			t.Fatal(err)
		}
		internalUser, err := p.InternalUserID(sc.UserID)
		if err != nil {
			t.Fatal(err)
		}
		body, err := json.Marshal(protocol.RunRequest{CaseID: sc.Case.ID, SystemPrompt: "help", UserInput: sc.Case.Question, Tools: []protocol.ToolDefinition{}, BenchVersion: protocol.BenchVersionV9, UserID: sc.UserID})
		if err != nil {
			t.Fatal(err)
		}
		examples = append(examples, classifierExample{role: internalUser, family: strings.Join(strings.Split(internalID, "-")[:2], "-"), features: classifierFeatures(sc.Case.ID, sc.UserID, position, len(p.MemoryCases), body)})
	}
	return examples
}

func classifierAccuracy(classifier categoricalClassifier, examples []classifierExample, label func(classifierExample) string) (float64, float64) {
	correct := 0
	for _, example := range examples {
		if classifier.predict(example.features) == label(example) {
			correct++
		}
	}
	n := float64(len(examples))
	chance := 1 / float64(len(classifier.labels))
	accuracy := float64(correct) / n
	expectedCorrect := n * chance
	expectedWrong := n * (1 - chance)
	chiSquare := math.Pow(float64(correct)-expectedCorrect, 2)/expectedCorrect + math.Pow(float64(len(examples)-correct)-expectedWrong, 2)/expectedWrong
	return accuracy, chiSquare
}

func TestV9KnownGeneratorCannotInferRoleOrFamilyAboveChance(t *testing.T) {
	var train, test []classifierExample
	for keyIndex := 0; keyIndex < 64; keyIndex++ {
		examples := examplesForProjection(t, keyIndex, true)
		if keyIndex < 48 {
			train = append(train, examples...)
		} else {
			test = append(test, examples...)
		}
	}
	roleLabel := func(example classifierExample) string { return example.role }
	familyLabel := func(example classifierExample) string { return example.family }
	roleClassifier := trainCategorical(train, roleLabel)
	familyClassifier := trainCategorical(train, familyLabel)
	roleAccuracy, roleChi := classifierAccuracy(roleClassifier, test, roleLabel)
	familyAccuracy, familyChi := classifierAccuracy(familyClassifier, test, familyLabel)
	if roleAccuracy > 0.56 || roleChi > 6.64 {
		t.Fatalf("role classifier exceeded chance: accuracy=%.4f chi-square=%.3f", roleAccuracy, roleChi)
	}
	if familyAccuracy > 0.30 || familyChi > 6.64 {
		t.Fatalf("family classifier exceeded chance: accuracy=%.4f chi-square=%.3f", familyAccuracy, familyChi)
	}
}

func TestKnownGeneratorClassifierControlLearnsLegacyRoleAndFamilyLabels(t *testing.T) {
	train := examplesForProjection(t, 0, false)
	test := examplesForProjection(t, 1, false)
	roleLabel := func(example classifierExample) string { return example.role }
	familyLabel := func(example classifierExample) string { return example.family }
	roleAccuracy, _ := classifierAccuracy(trainCategorical(train, roleLabel), test, roleLabel)
	familyAccuracy, _ := classifierAccuracy(trainCategorical(train, familyLabel), test, familyLabel)
	if roleAccuracy != 1 || familyAccuracy != 1 {
		t.Fatalf("control classifier is not meaningful: role=%.3f family=%.3f", roleAccuracy, familyAccuracy)
	}
}

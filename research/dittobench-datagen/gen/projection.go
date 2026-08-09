package gen

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// IDProjection records the validator-private relationship between a canonical
// dataset identifier and the opaque capability presented to a V9 harness.
// These entries belong in the validator-private projection artifact, never in
// the public transcript, /seed, or /run.
type IDProjection struct {
	Internal string `json:"internal"`
	Wire     string `json:"wire"`
}

// ScopedIDProjection prevents identical labels in separate user graphs from
// becoming a cross-graph correlation oracle.
type ScopedIDProjection struct {
	UserID   string `json:"user_id"`
	Internal string `json:"internal"`
	Wire     string `json:"wire"`
}

// HarnessProjectionManifest pins V9's otherwise-private capability aliases and
// final execution order. It makes a dispute reproducible without exposing any
// of this metadata to the evaluated harness.
type HarnessProjectionManifest struct {
	BlindingKey     string               `json:"blinding_key"`
	Users           []IDProjection       `json:"users"`
	Cases           []IDProjection       `json:"cases"`
	Pairs           []ScopedIDProjection `json:"pairs"`
	Sessions        []ScopedIDProjection `json:"sessions"`
	Subjects        []ScopedIDProjection `json:"subjects"`
	ToolCaseOrder   []string             `json:"tool_case_order"`
	MemoryCaseOrder []string             `json:"memory_case_order"`
}

// HarnessProjection is the V9 harness-facing view plus the private reverse map
// used to restore canonical IDs before scoring, reporting, and transcript
// persistence.
type HarnessProjection struct {
	Manifest    HarnessProjectionManifest
	ToolCases   []protocol.ToolCase
	MemoryCases []StagedCase
	Waves       []protocol.SeedRequest

	caseToInternal  map[string]string
	userToInternal  map[string]string
	valueToInternal map[string]string
	textToInternal  *strings.Replacer
}

const projectionDomain = "dittobench/v9/hostile-wire-projection"

type projectionPRF struct{ key [32]byte }

func newProjectionPRF(seed int64, blindingKey []byte) (projectionPRF, error) {
	if len(blindingKey) != sha256.Size {
		return projectionPRF{}, fmt.Errorf("v9 projection blinding key must be %d bytes", sha256.Size)
	}
	var raw [8]byte
	binary.BigEndian.PutUint64(raw[:], uint64(seed))
	h := hmac.New(sha256.New, blindingKey)
	_, _ = h.Write([]byte(projectionDomain + "/key"))
	_, _ = h.Write([]byte{0})
	_, _ = h.Write(raw[:])
	var key [32]byte
	copy(key[:], h.Sum(nil))
	return projectionPRF{key: key}, nil
}

func (p projectionPRF) digest(domain, value string) [32]byte {
	h := hmac.New(sha256.New, p.key[:])
	_, _ = h.Write([]byte(domain))
	_, _ = h.Write([]byte{0})
	_, _ = h.Write([]byte(value))
	var out [32]byte
	copy(out[:], h.Sum(nil))
	return out
}

func (p projectionPRF) uuid(domain string, ordinal int) string {
	var n [8]byte
	binary.BigEndian.PutUint64(n[:], uint64(ordinal))
	h := hmac.New(sha256.New, p.key[:])
	_, _ = h.Write([]byte(domain))
	_, _ = h.Write([]byte{0})
	_, _ = h.Write(n[:])
	b := h.Sum(nil)[:16]
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%s-%s-%s-%s-%s", hex.EncodeToString(b[0:4]), hex.EncodeToString(b[4:6]), hex.EncodeToString(b[6:8]), hex.EncodeToString(b[8:10]), hex.EncodeToString(b[10:16]))
}

func normalizedUser(userID string) string {
	if userID == "" {
		return PrimaryUser
	}
	return userID
}

func scopedKey(userID, value string) string { return normalizedUser(userID) + "\x00" + value }

func sortedSet(values map[string]struct{}) []string {
	out := make([]string, 0, len(values))
	for value := range values {
		out = append(out, value)
	}
	sort.Strings(out)
	return out
}

func aliases(prf projectionPRF, domain string, values []string) ([]IDProjection, map[string]string) {
	out := make([]IDProjection, len(values))
	lookup := make(map[string]string, len(values))
	for i, value := range values {
		wire := prf.uuid(domain, i)
		out[i] = IDProjection{Internal: value, Wire: wire}
		lookup[value] = wire
	}
	return out, lookup
}

func scopedAliases(prf projectionPRF, domain string, values []string) ([]ScopedIDProjection, map[string]string) {
	out := make([]ScopedIDProjection, len(values))
	lookup := make(map[string]string, len(values))
	for i, value := range values {
		parts := strings.SplitN(value, "\x00", 2)
		wire := prf.uuid(domain, i)
		out[i] = ScopedIDProjection{UserID: parts[0], Internal: parts[1], Wire: wire}
		lookup[value] = wire
	}
	return out, lookup
}

// BuildHarnessProjection constructs the complete V9 hostile-wire view. It is
// intentionally V9-only: callers must leave older versions on their exact
// historical path.
func BuildHarnessProjection(seed int64, blindingKey []byte, benchVersion int, toolCases []protocol.ToolCase, memoryCases []StagedCase, waves []protocol.SeedRequest) (*HarnessProjection, error) {
	if benchVersion != protocol.BenchVersionV9 {
		return nil, fmt.Errorf("harness projection requires bench version 9")
	}
	prf, err := newProjectionPRF(seed, blindingKey)
	if err != nil {
		return nil, err
	}
	users, cases := map[string]struct{}{}, map[string]struct{}{}
	pairs, sessions, subjects := map[string]struct{}{}, map[string]struct{}{}, map[string]struct{}{}
	users[PrimaryUser] = struct{}{}
	addPair := func(user string, pair protocol.MemoryPair) {
		user = normalizedUser(user)
		users[user] = struct{}{}
		pairs[scopedKey(user, pair.PairID)] = struct{}{}
		session := pair.SessionID
		if session == "" {
			session = "@pair:" + pair.PairID
		}
		sessions[scopedKey(user, session)] = struct{}{}
	}
	for _, c := range toolCases {
		cases[c.ID] = struct{}{}
		for _, pair := range c.PrerequisitePairs {
			addPair(PrimaryUser, pair)
		}
	}
	for _, sc := range memoryCases {
		cases[sc.Case.ID] = struct{}{}
		users[normalizedUser(sc.UserID)] = struct{}{}
	}
	for _, wave := range waves {
		user := normalizedUser(wave.UserID)
		users[user] = struct{}{}
		for _, pair := range wave.Pairs {
			addPair(user, pair)
		}
		for _, subject := range wave.Subjects {
			subjects[scopedKey(user, subject.ID)] = struct{}{}
		}
	}

	userEntries, userAlias := aliases(prf, "user", sortedSet(users))
	caseEntries, caseAlias := aliases(prf, "case", sortedSet(cases))
	pairEntries, pairAlias := scopedAliases(prf, "pair", sortedSet(pairs))
	sessionEntries, sessionAlias := scopedAliases(prf, "session", sortedSet(sessions))
	subjectEntries, subjectAlias := scopedAliases(prf, "subject", sortedSet(subjects))

	p := &HarnessProjection{
		Manifest:        HarnessProjectionManifest{BlindingKey: hex.EncodeToString(blindingKey), Users: userEntries, Cases: caseEntries, Pairs: pairEntries, Sessions: sessionEntries, Subjects: subjectEntries},
		caseToInternal:  make(map[string]string, len(caseEntries)),
		userToInternal:  make(map[string]string, len(userEntries)),
		valueToInternal: make(map[string]string, len(pairEntries)+len(subjectEntries)),
	}
	for _, entry := range caseEntries {
		p.caseToInternal[entry.Wire] = entry.Internal
		p.valueToInternal[entry.Wire] = entry.Internal
	}
	for _, entry := range userEntries {
		p.userToInternal[entry.Wire] = entry.Internal
		p.valueToInternal[entry.Wire] = entry.Internal
	}
	for _, entry := range pairEntries {
		p.valueToInternal[entry.Wire] = entry.Internal
	}
	for _, entry := range subjectEntries {
		p.valueToInternal[entry.Wire] = entry.Internal
	}
	for _, entry := range sessionEntries {
		p.valueToInternal[entry.Wire] = entry.Internal
	}
	type reverseReplacement struct{ wire, internal string }
	reverseValues := make([]reverseReplacement, 0, len(userEntries)+len(caseEntries)+len(pairEntries)+len(sessionEntries)+len(subjectEntries))
	for _, entry := range userEntries {
		reverseValues = append(reverseValues, reverseReplacement{entry.Wire, entry.Internal})
	}
	for _, entry := range caseEntries {
		reverseValues = append(reverseValues, reverseReplacement{entry.Wire, entry.Internal})
	}
	for _, entry := range pairEntries {
		reverseValues = append(reverseValues, reverseReplacement{entry.Wire, entry.Internal})
	}
	for _, entry := range sessionEntries {
		reverseValues = append(reverseValues, reverseReplacement{entry.Wire, entry.Internal})
	}
	for _, entry := range subjectEntries {
		reverseValues = append(reverseValues, reverseReplacement{entry.Wire, entry.Internal})
	}
	sort.Slice(reverseValues, func(i, j int) bool { return len(reverseValues[i].wire) > len(reverseValues[j].wire) })
	reverseArgs := make([]string, 0, len(reverseValues)*2)
	for _, value := range reverseValues {
		reverseArgs = append(reverseArgs, value.wire, value.internal)
	}
	p.textToInternal = strings.NewReplacer(reverseArgs...)
	textReplacers := map[string]*strings.Replacer{}
	for _, user := range sortedSet(users) {
		type replacement struct{ internal, wire string }
		values := []replacement{{internal: user, wire: userAlias[user]}}
		for internal, wire := range caseAlias {
			values = append(values, replacement{internal, wire})
		}
		for scoped, wire := range pairAlias {
			if strings.HasPrefix(scoped, user+"\x00") {
				values = append(values, replacement{strings.TrimPrefix(scoped, user+"\x00"), wire})
			}
		}
		for scoped, wire := range sessionAlias {
			if strings.HasPrefix(scoped, user+"\x00") {
				internal := strings.TrimPrefix(scoped, user+"\x00")
				if !strings.HasPrefix(internal, "@pair:") {
					values = append(values, replacement{internal, wire})
				}
			}
		}
		for scoped, wire := range subjectAlias {
			if strings.HasPrefix(scoped, user+"\x00") {
				values = append(values, replacement{strings.TrimPrefix(scoped, user+"\x00"), wire})
			}
		}
		sort.Slice(values, func(i, j int) bool { return len(values[i].internal) > len(values[j].internal) })
		args := make([]string, 0, len(values)*2)
		for _, value := range values {
			if value.internal != "" {
				args = append(args, value.internal, value.wire)
			}
		}
		textReplacers[user] = strings.NewReplacer(args...)
	}
	projectText := func(user, value string) string { return textReplacers[normalizedUser(user)].Replace(value) }

	projectPair := func(user string, pair protocol.MemoryPair) protocol.MemoryPair {
		out := pair
		out.Prompt = projectText(user, out.Prompt)
		out.Response = projectText(user, out.Response)
		out.PairID = pairAlias[scopedKey(user, pair.PairID)]
		session := pair.SessionID
		if session == "" {
			session = "@pair:" + pair.PairID
		}
		out.SessionID = sessionAlias[scopedKey(user, session)]
		return out
	}
	projectArgs := func(user string, args map[string]string) map[string]string {
		if len(args) == 0 {
			return args
		}
		out := make(map[string]string, len(args))
		for key, value := range args {
			if alias, ok := userAlias[value]; ok {
				value = alias
			}
			if alias, ok := caseAlias[value]; ok {
				value = alias
			}
			if alias, ok := pairAlias[scopedKey(user, value)]; ok {
				value = alias
			}
			if alias, ok := sessionAlias[scopedKey(user, value)]; ok {
				value = alias
			}
			if alias, ok := subjectAlias[scopedKey(user, value)]; ok {
				value = alias
			}
			value = projectText(user, value)
			out[key] = value
		}
		return out
	}

	p.ToolCases = make([]protocol.ToolCase, len(toolCases))
	for i, c := range toolCases {
		out := c
		out.ID = caseAlias[c.ID]
		out.Prompt = projectText(PrimaryUser, out.Prompt)
		out.PrerequisitePairs = append([]protocol.MemoryPair(nil), c.PrerequisitePairs...)
		for j := range out.PrerequisitePairs {
			out.PrerequisitePairs[j] = projectPair(PrimaryUser, out.PrerequisitePairs[j])
		}
		out.PrerequisitePairs = permuteSessionBlocks(prf, "tool-prerequisites/"+c.ID, out.PrerequisitePairs)
		out.ExpectedTools = append([]protocol.ToolSpec(nil), c.ExpectedTools...)
		for j := range out.ExpectedTools {
			out.ExpectedTools[j].RequiredArgs = projectArgs(PrimaryUser, out.ExpectedTools[j].RequiredArgs)
		}
		p.ToolCases[i] = out
	}
	sort.SliceStable(p.ToolCases, func(i, j int) bool {
		return digestLess(prf.digest("tool-order", p.caseToInternal[p.ToolCases[i].ID]), prf.digest("tool-order", p.caseToInternal[p.ToolCases[j].ID]))
	})
	for _, c := range p.ToolCases {
		p.Manifest.ToolCaseOrder = append(p.Manifest.ToolCaseOrder, p.caseToInternal[c.ID])
	}

	p.MemoryCases = make([]StagedCase, len(memoryCases))
	for i, sc := range memoryCases {
		out := sc
		user := normalizedUser(sc.UserID)
		out.UserID = userAlias[user]
		out.Case.ID = caseAlias[sc.Case.ID]
		out.Case.Question = projectText(user, out.Case.Question)
		out.RequiredPairIDs = append([]string(nil), sc.RequiredPairIDs...)
		for j, id := range out.RequiredPairIDs {
			if alias, ok := pairAlias[scopedKey(user, id)]; ok {
				out.RequiredPairIDs[j] = alias
			}
		}
		p.MemoryCases[i] = out
	}
	sort.SliceStable(p.MemoryCases, func(i, j int) bool {
		if p.MemoryCases[i].RunAfterWave != p.MemoryCases[j].RunAfterWave {
			return p.MemoryCases[i].RunAfterWave < p.MemoryCases[j].RunAfterWave
		}
		return digestLess(prf.digest("memory-order", p.caseToInternal[p.MemoryCases[i].Case.ID]), prf.digest("memory-order", p.caseToInternal[p.MemoryCases[j].Case.ID]))
	})
	for _, sc := range p.MemoryCases {
		p.Manifest.MemoryCaseOrder = append(p.Manifest.MemoryCaseOrder, p.caseToInternal[sc.Case.ID])
	}

	p.Waves = make([]protocol.SeedRequest, len(waves))
	for i, wave := range waves {
		user := normalizedUser(wave.UserID)
		out := protocol.SeedRequest{UserID: userAlias[user]}
		out.Pairs = make([]protocol.MemoryPair, len(wave.Pairs))
		for j, pair := range wave.Pairs {
			out.Pairs[j] = projectPair(user, pair)
		}
		out.Pairs = permuteSessionBlocks(prf, fmt.Sprintf("wave/%d/pairs", i), out.Pairs)
		out.Subjects = append([]protocol.Subject(nil), wave.Subjects...)
		for j := range out.Subjects {
			out.Subjects[j].ID = subjectAlias[scopedKey(user, out.Subjects[j].ID)]
			out.Subjects[j].SubjectText = projectText(user, out.Subjects[j].SubjectText)
			out.Subjects[j].DescriptionText = projectText(user, out.Subjects[j].DescriptionText)
		}
		permuteBy(prf, fmt.Sprintf("wave/%d/subjects", i), out.Subjects, func(s protocol.Subject) string { return s.ID })
		out.Links = append([]protocol.SubjectLink(nil), wave.Links...)
		for j := range out.Links {
			out.Links[j].SubjectID = subjectAlias[scopedKey(user, out.Links[j].SubjectID)]
			out.Links[j].PairID = pairAlias[scopedKey(user, out.Links[j].PairID)]
		}
		permuteBy(prf, fmt.Sprintf("wave/%d/links", i), out.Links, func(link protocol.SubjectLink) string { return link.SubjectID + "\x00" + link.PairID })
		p.Waves[i] = out // Wave deliberately remains zero and is omitted on V9 wire.
	}
	return p, nil
}

func digestLess(a, b [32]byte) bool { return string(a[:]) < string(b[:]) }

func permuteBy[T any](prf projectionPRF, domain string, values []T, key func(T) string) {
	sort.SliceStable(values, func(i, j int) bool {
		return digestLess(prf.digest(domain, key(values[i])), prf.digest(domain, key(values[j])))
	})
}

func permuteSessionBlocks(prf projectionPRF, domain string, pairs []protocol.MemoryPair) []protocol.MemoryPair {
	blocks := map[string][]protocol.MemoryPair{}
	for _, pair := range pairs {
		blocks[pair.SessionID] = append(blocks[pair.SessionID], pair)
	}
	keys := make([]string, 0, len(blocks))
	for key := range blocks {
		keys = append(keys, key)
		sort.SliceStable(blocks[key], func(i, j int) bool { return blocks[key][i].Timestamp < blocks[key][j].Timestamp })
	}
	permuteBy(prf, domain, keys, func(key string) string { return key })
	out := make([]protocol.MemoryPair, 0, len(pairs))
	for _, key := range keys {
		out = append(out, blocks[key]...)
	}
	return out
}

// InternalCaseID restores a V9 capability to its canonical report identity.
func (p *HarnessProjection) InternalCaseID(wire string) (string, error) {
	if internal, ok := p.caseToInternal[wire]; ok {
		return internal, nil
	}
	return "", fmt.Errorf("unknown v9 case capability")
}

// InternalUserID restores a V9 graph capability to its canonical identity.
func (p *HarnessProjection) InternalUserID(wire string) (string, error) {
	if internal, ok := p.userToInternal[wire]; ok {
		return internal, nil
	}
	return "", fmt.Errorf("unknown v9 user capability")
}

// WireUserID returns the opaque V9 capability for a canonical graph.
func (p *HarnessProjection) WireUserID(internal string) string {
	internal = normalizedUser(internal)
	for _, entry := range p.Manifest.Users {
		if entry.Internal == internal {
			return entry.Wire
		}
	}
	return ""
}

// InternalizeText restores known capabilities embedded in harness text for the
// canonical scorer/transcript. Unknown text is ordinary model output and stays
// untouched; structured identity fields use InternalizeCalls and fail closed.
func (p *HarnessProjection) InternalizeText(value string) string {
	return p.textToInternal.Replace(value)
}

// InternalizeCalls reverses opaque pair/subject values recursively in observed
// JSON arguments before canonical scoring and transcript persistence.
func (p *HarnessProjection) InternalizeCalls(calls []protocol.ObservedToolCall) ([]protocol.ObservedToolCall, error) {
	out := append([]protocol.ObservedToolCall(nil), calls...)
	for i := range out {
		if len(out[i].Args) == 0 {
			continue
		}
		var value any
		if err := json.Unmarshal(out[i].Args, &value); err != nil {
			return nil, fmt.Errorf("decode v9 observed tool arguments: %w", err)
		}
		value, err := p.internalizeValue("", value)
		if err != nil {
			return nil, err
		}
		if raw, err := json.Marshal(value); err == nil {
			out[i].Args = raw
		}
	}
	return out, nil
}

func (p *HarnessProjection) internalizeValue(field string, value any) (any, error) {
	switch v := value.(type) {
	case string:
		if internal, ok := p.valueToInternal[v]; ok {
			return internal, nil
		}
		if field == "pair_id" || field == "pairIds" || field == "subject_id" || field == "session_id" || field == "case_id" || field == "user_id" {
			return nil, fmt.Errorf("unknown v9 identity capability in %s", field)
		}
		return v, nil
	case []any:
		for i := range v {
			mapped, err := p.internalizeValue(field, v[i])
			if err != nil {
				return nil, err
			}
			v[i] = mapped
		}
	case map[string]any:
		for key := range v {
			mapped, err := p.internalizeValue(key, v[key])
			if err != nil {
				return nil, err
			}
			v[key] = mapped
		}
	}
	return value, nil
}

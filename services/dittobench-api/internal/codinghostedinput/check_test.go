package codinghostedinput

import (
	"context"
	"strings"
	"testing"
)

func TestCheckTaskProfileRejectsInconsistentReviewInputs(t *testing.T) {
	profile := fixtureProfile()
	binding := TaskBinding{CatalogIndex: 3, TaskCommitmentSHA256: strings.Repeat("d", 64), PrivateReleaseSHA256: strings.Repeat("e", 64), CorpusReleaseID: "coding-private-v2-r2", MaxPatchBytes: profile.ResourcePolicy.CandidateLimits.MaxPatchBytes}
	objects := map[string][]byte{}
	for _, role := range roles {
		objects[role] = []byte{1}
	}
	for name, mutate := range map[string]func(*TaskBinding, *Profile, map[string][]byte){
		"patch-binding":  func(b *TaskBinding, _ *Profile, _ map[string][]byte) { b.MaxPatchBytes++ },
		"catalog-index":  func(b *TaskBinding, _ *Profile, _ map[string][]byte) { b.CatalogIndex = 250 },
		"commitment":     func(b *TaskBinding, _ *Profile, _ map[string][]byte) { b.TaskCommitmentSHA256 = "x" },
		"invalid-policy": func(_ *TaskBinding, p *Profile, _ map[string][]byte) { p.ResourcePolicy.PidsLimit = 0 },
		"tool-budget":    func(_ *TaskBinding, p *Profile, _ map[string][]byte) { p.Budgets.WorkspaceToolCalls++ },
		"missing-role":   func(_ *TaskBinding, _ *Profile, o map[string][]byte) { delete(o, "issue") },
		"empty-role":     func(_ *TaskBinding, _ *Profile, o map[string][]byte) { o["issue"] = nil },
		"oversized-visible": func(_ *TaskBinding, p *Profile, o map[string][]byte) {
			o["visible_bundle"] = make([]byte, p.ResourcePolicy.CandidateLimits.MaxBundleBytes+1)
		},
		"compile": func(*TaskBinding, *Profile, map[string][]byte) {},
	} {
		b, p, o := binding, profile, map[string][]byte{}
		for role, body := range objects {
			o[role] = body
		}
		mutate(&b, &p, o)
		if CheckTaskProfile(context.Background(), b, p, o) == nil {
			t.Fatalf("%s accepted", name)
		}
	}
}

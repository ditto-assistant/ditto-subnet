package universe

import (
	"strings"
	"testing"
)

func TestStoryLessonsUseFrozenHumanCorpus(t *testing.T) {
	if len(storyLessons) < 70 {
		t.Fatalf("story lesson variety=%d, want at least 70", len(storyLessons))
	}
	seen := map[string]bool{}
	for _, lesson := range storyLessons {
		canonical := strings.TrimSpace(strings.ToLower(lesson.canonical))
		if canonical == "" {
			t.Fatal("empty story lesson")
		}
		if seen[canonical] {
			t.Fatalf("duplicate story lesson %q", lesson.canonical)
		}
		seen[canonical] = true
	}
}

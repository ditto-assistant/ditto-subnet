package universe

import (
	_ "embed"
	"strings"
)

// commonLessonData is a frozen, reviewed CC0 subset. Keeping it in the binary
// gives every validator identical bytes and avoids a mutable runtime API.
//
//go:embed assets/common_lessons.txt
var commonLessonData string

func init() {
	seen := make(map[string]bool, len(storyLessons))
	for _, lesson := range storyLessons {
		seen[strings.ToLower(lesson.canonical)] = true
	}
	for _, line := range strings.Split(commonLessonData, "\n") {
		lesson := strings.TrimSpace(line)
		if lesson == "" || seen[strings.ToLower(lesson)] {
			continue
		}
		seen[strings.ToLower(lesson)] = true
		storyLessons = append(storyLessons, lessonSet{canonical: lesson})
	}
}

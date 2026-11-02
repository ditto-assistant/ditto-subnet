package universe

import (
	"fmt"
	"testing"
	"time"
)

func TestV13RecordedCalendarDatesCarryTheirYear(t *testing.T) {
	for seed := int64(1); seed <= 40; seed++ {
		for _, past := range []bool{false, true} {
			for month := 1; month <= 12; month++ {
				date := v13Date{Month: month, Day: 15}
				want := v13CalendarDate(seed, "business", 0, date, past)
				text := v13CalendarProse(seed, "business", 0, fmt.Sprint(month), date, past)
				var parsed time.Time
				for _, layout := range []string{"2006-01-02", "January 2, 2006", "Jan 2, 2006", "2 January 2006"} {
					if at, err := time.Parse(layout, text); err == nil {
						parsed = at
						break
					}
				}
				if !parsed.Equal(want) {
					t.Fatalf("seed %d past %v: %q cannot recover %s", seed, past, text, want)
				}
			}
		}
	}
}

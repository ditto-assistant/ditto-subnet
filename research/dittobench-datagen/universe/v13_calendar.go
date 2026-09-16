package universe

import (
	"fmt"
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// Every family uses the same timestamp distribution. Semantic event dates are
// placed relative to that group's anchor, instead of leaking the family via a
// fixed month while still keeping past events past and planned events future.
func v13CalendarKey(domain string, group int) string {
	return fmt.Sprintf("v13-%s-calendar-%d", domain, group)
}

func v13CalendarDate(seed int64, domain string, group int, date v13Date, past bool) time.Time {
	at := protocol.OpaqueBusinessInstant(seed, v13CalendarKey(domain, group))
	days := 60 + date.ordinal()
	if past {
		days = date.ordinal() - 450
	}
	return at.AddDate(0, 0, days).Truncate(24 * time.Hour)
}

func v13CalendarTimestamp(seed int64, domain string, group, slot int) string {
	timeline := protocol.NewOpaqueTimeline(seed, v13CalendarKey(domain, group))
	var stamp string
	for j := 0; j <= slot; j++ {
		stamp = timeline.Next()
	}
	return stamp
}

func v13CalendarProse(seed int64, domain string, group int, salt string, date v13Date, past bool) string {
	at := v13CalendarDate(seed, domain, group, date, past)
	forms := V13DateAccept(at.Year(), int(at.Month()), at.Day())
	return v13Pick(seed, "date-form-"+salt, forms)
}

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

// Case-file rows are shuffled for presentation. v13 stamped each row from its
// shuffled position, so a plain statement could be filed after the change that
// replaced it while the oracle still graded the change as current
// (ditto-subnet#3088). A memory agent that trusts the latest filing date was
// marked wrong for it. From v14 the stamp follows the row's story position, so
// the dates agree with the narrative the oracle grades and a reader still has
// to sort the document. offset counts leading rows that are never shuffled.
func v13CaseFileSlot(benchVersion int, perm []int, presented, offset int) int {
	if benchVersion < protocol.BenchVersionV14 || presented < offset {
		return presented
	}
	return offset + perm[presented-offset]
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
	// Relative anchors can straddle a year boundary. A yearless event date
	// does not uniquely determine chronology from the seeded evidence.
	// Keep answer aliases permissive, but make every recorded date explicit.
	forms = []string{forms[0], forms[2], forms[4], forms[6]}
	return v13Pick(seed, "date-form-"+salt, forms)
}

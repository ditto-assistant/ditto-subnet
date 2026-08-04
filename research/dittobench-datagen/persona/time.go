package persona

import (
	"time"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

// TimeSlackDays keeps every rendered timestamp strictly before the pinned
// dataset epoch: the haystack is "the past" as of protocol.DatasetEpoch.
// Shared by the gen renderer and by date-bearing questions so a date printed
// in a question always agrees with the pair timestamps the harness reads.
const TimeSlackDays = 7

// TimeAnchor is the absolute instant of a plan's day offset 0: the dataset
// epoch minus the plan's span plus slack. A session with DayOffset d starts
// at TimeAnchor(p) + d days; the gen renderer and every date a question
// renders derive from this one function so they can never drift.
func TimeAnchor(p *Plan) time.Time {
	maxDay := 0
	for _, s := range p.Sessions {
		if s.DayOffset > maxDay {
			maxDay = s.DayOffset
		}
	}
	version := p.BenchVersion
	if version == 0 {
		version = protocol.BenchVersionV2
	}
	epoch, err := protocol.DatasetEpochForVersion(version)
	if err != nil {
		epoch = protocol.DatasetEpoch
	}
	return epoch.Add(-time.Duration(maxDay+TimeSlackDays) * 24 * time.Hour)
}

// SessionDate is the calendar start instant of session index si (day
// granularity; intra-session beat spacing is the renderer's concern). Returns
// TimeAnchor(p) when the index is unknown.
func SessionDate(p *Plan, si int) time.Time {
	for _, s := range p.Sessions {
		if s.Index == si {
			return TimeAnchor(p).Add(time.Duration(s.DayOffset) * 24 * time.Hour)
		}
	}
	return TimeAnchor(p)
}

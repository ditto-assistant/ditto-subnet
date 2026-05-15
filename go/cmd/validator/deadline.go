package main

import (
	"fmt"

	"github.com/heyditto/ditto-subnet/go/bittensor"
)

// maybeFlagDeadline appends a "deadline_overrun" note to s when the
// miner reported a latency above the validator's deadline budget. The
// validator already returns errHarnessTimeout when the scanner cuts the
// response off, so this catches the "miner returned just-in-time but
// still over" case the latency_score curve absorbs silently.
//
// We surface it on the Score so dispute/replay can tell whether a
// miner's lower aggregate weight stemmed from being legitimately slow
// versus the validator's own clock.
func maybeFlagDeadline(s *bittensor.Score, latencyMs, deadlineMs int64) {
	if deadlineMs <= 0 || latencyMs <= 0 {
		return
	}
	if latencyMs <= deadlineMs {
		return
	}
	note := fmt.Sprintf("deadline_overrun:latency=%dms,deadline=%dms", latencyMs, deadlineMs)
	s.Notes = append(s.Notes, note)
}

package main

import (
	"os"

	"github.com/ditto-assistant/dittobench-api/internal/scorer"
)

// v18UnlinkedReplyPostureEnv selects the calibration stage of the Bench v18
// unlinked-connector reply rule (scorer.ApplyV18UnlinkedReply). It is read
// once at process start. Anything but an exact "penalize" or "enforce" is
// REVIEW: the case carries a note saying whether the reply told the user the
// app is not connected, and no score moves. Moving it is an operator decision
// made after the review notes have been checked against cleared top agents.
const v18UnlinkedReplyPostureEnv = "DITTOBENCH_V18_UNLINKED_REPLY_POSTURE"

var v18UnlinkedReplyPosture = scorer.ParseUnlinkedReplyPosture(os.Getenv(v18UnlinkedReplyPostureEnv))

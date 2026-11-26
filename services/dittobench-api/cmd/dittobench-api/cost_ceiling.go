package main

import (
	"fmt"
	"net/http"
	"strconv"
	"strings"
)

// Per-run dollar cost ceiling (ditto-subnet#3271). The Platform stamps one
// ledger-cost ceiling on the grant at mint and refuses admission with 4110 once
// the grant's SETTLED ledger cost reaches it, on every inference lane. This
// broker never trusts that code alone: it confirms 4110 against the ledger
// total the Platform reports on each authenticated response, exactly as it
// confirms 4104 against settled receipted tokens.
const (
	// ledgerCostHeader is the grant's total ledger cost after this request's
	// settlement, sent by the Platform on authenticated responses only while a
	// ceiling is stamped.
	ledgerCostHeader = "X-Ditto-Ledger-Cost-Microusd"
	// The harness-facing pacing headers. Informational only: nothing the
	// harness sends back is read.
	harnessCostSpentHeader   = "X-Ditto-Cost-Spent-Microusd"
	harnessCostCeilingHeader = "X-Ditto-Cost-Ceiling-Microusd"

	costCeilingModeOff     = "off"
	costCeilingModeShadow  = "shadow"
	costCeilingModeEnforce = "enforce"
)

// validCostCeilingMode accepts the Platform's three modes and the absent value
// an older Platform sends.
func validCostCeilingMode(mode string) bool {
	switch mode {
	case "", costCeilingModeOff, costCeilingModeShadow, costCeilingModeEnforce:
		return true
	default:
		return false
	}
}

// validCostCeilingActivation accepts a ceiling with a known mode, or neither
// (an older Platform, or no ceiling stamped). A ceiling without a mode, or a
// mode without a ceiling, is a broken exchange: the broker could neither arm
// nor confirm it, so the activation is refused rather than run unguarded.
func validCostCeilingActivation(ceiling uint64, mode string) bool {
	if !validCostCeilingMode(mode) {
		return false
	}
	return (ceiling > 0) == (mode != "")
}

// costCeilingArmedLocked reports whether the grant carries a ceiling the
// harness should see: stamped, and not switched off.
func costCeilingArmedLocked(session *brokerSession) bool {
	return session.costCeilingMicrousd > 0 &&
		(session.costCeilingMode == costCeilingModeShadow || session.costCeilingMode == costCeilingModeEnforce)
}

// sessionHasCostEvidenceLocked reports whether this session can confirm a
// 4110 at all. Only an enforcing ceiling declines; a 4110 under shadow, off or
// no authenticated ceiling is impossible by construction.
func sessionHasCostEvidenceLocked(session *brokerSession) bool {
	return session.costCeilingMicrousd > 0 && session.costCeilingMode == costCeilingModeEnforce
}

// costCeilingSpentLocked reports whether the Platform's own settled ledger
// total, as this broker observed it, has reached the enforced ceiling.
func costCeilingSpentLocked(session *brokerSession) bool {
	return sessionHasCostEvidenceLocked(session) && session.ledgerCostMicrousd >= session.costCeilingMicrousd
}

// observeLedgerCostLocked keeps the highest ledger total any authenticated
// Platform response reported. Responses settle and return out of order, and
// the total only grows, so the maximum is the latest settled spend. A missing
// or malformed header leaves the evidence where it was: it can only fail to
// confirm a 4110, never confirm one.
func observeLedgerCostLocked(session *brokerSession, header http.Header) {
	raw := strings.TrimSpace(header.Get(ledgerCostHeader))
	if raw == "" {
		return
	}
	value, err := strconv.ParseUint(raw, 10, 64)
	if err != nil {
		return
	}
	if value > session.ledgerCostMicrousd {
		session.ledgerCostMicrousd = value
	}
}

// beginLedgerDispatchLocked counts one signed Platform call (any lane) whose
// response, and so whose ledger header, the broker has not yet read. Pair it
// with exactly one endLedgerDispatchLocked.
func beginLedgerDispatchLocked(session *brokerSession) {
	session.ledgerDispatchesInFlight++
}

// endLedgerDispatchLocked books one Platform call's response: its ledger
// header when there is one (nil for a transport failure), then any 4110 that
// was waiting on it. It reports whether that settled a pending 4110 as the
// agent's, so the caller notifies the terminal failure once it has released
// the session lock.
func endLedgerDispatchLocked(session *brokerSession, header http.Header) (agentDecline bool) {
	if header != nil {
		observeLedgerCostLocked(session, header)
	}
	if session.ledgerDispatchesInFlight > 0 {
		session.ledgerDispatchesInFlight--
	}
	return reconcileCostCeilingDeclinesLocked(session)
}

// reconcileCostCeilingDeclinesLocked settles the 4110s that arrived before
// the broker could confirm them. Calls settle at the Platform before their
// responses reach the broker, so with siblings in flight a genuine 4110 can
// arrive while the observed ledger total still trails the ceiling. Such a
// decline waits, counted only in grantDenials, until a sibling's ledger header
// proves the ceiling spent (agent fault) or the last sibling has answered
// without proving it (evidence mismatch, platform fault). It reports whether
// declines were confirmed as the agent's.
func reconcileCostCeilingDeclinesLocked(session *brokerSession) bool {
	pending := session.costCeilingPendingDeclines
	if pending == 0 {
		return false
	}
	if costCeilingSpentLocked(session) {
		session.costCeilingPendingDeclines = 0
		session.grantAgentDeclines += pending
		session.costCeilingAgentDeclines += pending
		noteAgentDeclineWallLocked(session, platformDeclineCostCeilingExhausted)
		return true
	}
	if session.ledgerDispatchesInFlight == 0 {
		session.costCeilingPendingDeclines = 0
		session.declineEvidenceMismatches += pending
	}
	return false
}

// attributeCostCeilingDeclineLocked classifies one 4110. The ceiling spans
// every lane, so its evidence is the shared settled ledger total, never either
// lane's request/token budgets, and a missing ceiling is counted apart from
// missing budgets.
func attributeCostCeilingDeclineLocked(session *brokerSession) (agentDecline bool, attribution string) {
	if !sessionHasCostEvidenceLocked(session) {
		session.costCeilingAbsences++
		return false, "platform fault: no authenticated enforcing cost ceiling; will not charge the miner"
	}
	if costCeilingSpentLocked(session) {
		session.grantAgentDeclines++
		session.costCeilingAgentDeclines++
		noteAgentDeclineWallLocked(session, platformDeclineCostCeilingExhausted)
		return true, "AGENT fault: the harness spent its own cost ceiling"
	}
	if session.ledgerDispatchesInFlight > 0 {
		session.costCeilingPendingDeclines++
		return false, fmt.Sprintf(
			"pending: settled ledger %d/%d microusd; waiting on %d in-flight call(s) to settle before charging anyone",
			session.ledgerCostMicrousd, session.costCeilingMicrousd, session.ledgerDispatchesInFlight,
		)
	}
	session.declineEvidenceMismatches++
	return false, "platform fault: cost ceiling code contradicted the broker's settled ledger evidence"
}

// setHarnessCostHeadersLocked lets a harness pace itself against the run's
// ceiling. Nothing is sent while no ceiling is armed, so an off-mode response
// is byte-identical to one from before the ceiling existed.
func setHarnessCostHeadersLocked(session *brokerSession, header http.Header) {
	if !costCeilingArmedLocked(session) {
		return
	}
	header.Set(harnessCostSpentHeader, strconv.FormatUint(session.ledgerCostMicrousd, 10))
	header.Set(harnessCostCeilingHeader, strconv.FormatUint(session.costCeilingMicrousd, 10))
}

// allowanceWall names which allowance an agent-attributable decline spent. It
// is an error so it can travel inside the finalize error to
// relayFinalizeFailure, which copies it into the run's diagnostics.
type allowanceWall string

const (
	allowanceWallRequest allowanceWall = "request"
	allowanceWallToken   allowanceWall = "token"
	allowanceWallCost    allowanceWall = "cost"
)

func (w allowanceWall) Error() string { return "allowance wall: " + string(w) }

// allowanceWallFor maps a confirmed agent decline code to its wall, or ""
// for a code that names none.
func allowanceWallFor(code int) allowanceWall {
	switch code {
	case platformDeclineBudgetExhausted:
		return allowanceWallRequest
	case platformDeclineTokenBudgetExhausted:
		return allowanceWallToken
	case platformDeclineCostCeilingExhausted:
		return allowanceWallCost
	default:
		return ""
	}
}

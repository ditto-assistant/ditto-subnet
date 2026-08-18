package main

// Production caseModelIOSource: it reads the trusted clean-pass model I/O the
// inference broker recorded for one case during the scored run. The broker
// captured, per case generation, the bounded/normalized value tokens of each
// model INPUT and COMPLETION (inference_broker.go recordAnswerIOLocked); this
// source hands one case's ordered log to the answer-stuffing provenance pass. The
// answer key is never in the broker -- only value tokens -- so this source cannot
// leak it. It is exercised only against a live broker session; the deterministic
// provenance logic it feeds is unit-tested with a modeled source in
// v12_answer_stuffing_test.go.

type brokerCaseModelIOSource struct {
	server             *server
	inferenceSessionID string
}

func (r *brokerCaseModelIOSource) CaseModelIO(caseID string) (caseModelIOLog, bool) {
	if r == nil || r.server == nil || r.server.broker == nil || r.inferenceSessionID == "" {
		return caseModelIOLog{}, false
	}
	return r.server.broker.caseModelIO(r.inferenceSessionID, caseID)
}

var _ caseModelIOSource = (*brokerCaseModelIOSource)(nil)

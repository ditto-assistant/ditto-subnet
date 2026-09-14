package scoregates

// Bench v13 CAUSAL MODEL-DEPENDENCE gate (issue #1833): answer_in_prompt.
//
// The claim-span provenance gate (text_provenance.go) proves the served value
// came out of the model. It cannot see where the model GOT it. The v11 champion
// pattern -- compute the answer locally from the regenerated dataset, put "reply
// exactly: X" in the prompt, let the model parrot X -- passes every provenance,
// catalog, and label test: the model emitted X, the catalog was offered, the
// served text is model text. Bench v12 tried to catch this with a run-level
// answer-stuffing share; it required exclusive per-case inference windows and
// was removed when /run became concurrent. This gate restores the check per
// CLAIM, on the relay's per-case ledger, with the exemption that makes honest
// retrieval safe by construction.
//
// Rule. For a graded claim (the served claim span the grader credited; see
// ServedClaimTokens), the relay records, per attributed completion, the value
// tokens of every HARNESS-AUTHORED span in the request (system/developer
// messages, the user template, an assistant prefill, tool-role messages) and
// notes which of them the model had NOT already produced in an earlier
// completion of the same case. From that "harness-first" set the scorer
// subtracts:
//
//   - every value token of a record delivered to the harness through /seed
//     (Platform/validator know them: they are the dataset), so quoting retrieved
//     memory into the prompt -- the whole point of a RAG harness -- is exempt;
//   - every value token of a mock tool result the validator served the case
//     through tool_endpoint, so quoting a tool result back to the model is exempt;
//   - the case's own user_input (the public question) and the validator's
//     system prompt, which the harness did not author.
//
// If the claim's canonical value tokens are a SUBSET of what remains, the harness
// wrote the answer into the prompt and the model only echoed it:
// answer_in_prompt. Values the model derived in an earlier completion and the
// harness reused in a later prompt ("you said 4110.67; now format it") are not
// harness-first, so legitimate multi-turn compute is untouched; values that are
// present in any delivered record are exempt, so an operand that happens to
// equal the answer is never a flag.
//
// What it does not claim: a vote the harness takes over several genuine model
// completions and then re-injects is model-derived (first seen in a
// completion) and passes here; that is attribution theatre for the cost factor,
// not laundering. A number the harness template happens to contain by
// coincidence that also is the computed answer and appears in no record is a
// flag -- that is why the gate ships in shadow and is calibrated on the honest
// cohort before enforce.

// CausalDependence is the pure verdict: answerInPrompt is true when the claim is
// non-empty and every claim token lies in the residual harness-first set. An
// empty claim is not applicable and never flags.
func CausalDependence(claim, residualHarnessFirst TokenSet) (answerInPrompt bool) {
	if len(claim) == 0 {
		return false
	}
	return claim.Subset(residualHarnessFirst)
}

// ResidualHarnessTokens returns the harness-first set minus every exemption set
// (delivered records, served tool results, the case's user input, the
// validator's own prompt). It never mutates its inputs.
func ResidualHarnessTokens(harnessFirst TokenSet, exemptions ...TokenSet) TokenSet {
	residual := make(TokenSet, len(harnessFirst))
	for h := range harnessFirst {
		exempt := false
		for _, set := range exemptions {
			if _, ok := set[h]; ok {
				exempt = true
				break
			}
		}
		if !exempt {
			residual[h] = struct{}{}
		}
	}
	return residual
}

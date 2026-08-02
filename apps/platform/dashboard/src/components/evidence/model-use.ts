// Model-use verdict rows (#527; monolith modelUseStats 6021–6045): the
// verdict AND the bar it was judged against — each observed number carries
// its enforced minimum, so "used" is checkable rather than asserted.
export interface ModelUse {
  /** "used" | "unmeasured" | anything else reads as not-used. */
  verdict?: string | null;
  prompt_tokens_per_case?: number | null;
  min_prompt_tokens_per_case?: number | null;
  calls_per_case?: number | null;
  min_calls_per_case?: number | null;
  prompt_tokens_per_call?: number | null;
  min_prompt_tokens_per_call?: number | null;
  reason?: string | null;
}

export interface ModelUseRow {
  k: string;
  v: string;
}

export function modelUseRows(mu: ModelUse | null | undefined): ModelUseRow[] {
  if (!mu) return [];
  if (mu.verdict === "unmeasured") {
    return [{ k: "Model use", v: "not measured for this run" }];
  }
  const used = mu.verdict === "used";
  const out: ModelUseRow[] = [
    { k: "Model use", v: used ? "used" : "NOT USED — scores zero once enforced" },
  ];
  if (mu.prompt_tokens_per_case != null) {
    out.push({
      k: "Prompt tokens / case",
      v:
        Math.round(mu.prompt_tokens_per_case).toLocaleString() +
        (mu.min_prompt_tokens_per_case != null
          ? " (min " + mu.min_prompt_tokens_per_case.toLocaleString() + ")"
          : ""),
    });
  }
  if (mu.calls_per_case != null) {
    out.push({
      k: "Model calls / case",
      v:
        mu.calls_per_case.toFixed(2) +
        (mu.min_calls_per_case != null ? " (min " + mu.min_calls_per_case + ")" : ""),
    });
  }
  if (mu.prompt_tokens_per_call != null) {
    out.push({
      k: "Prompt tokens / call",
      v:
        Math.round(mu.prompt_tokens_per_call).toLocaleString() +
        (mu.min_prompt_tokens_per_call != null
          ? " (min " + mu.min_prompt_tokens_per_call.toLocaleString() + ")"
          : ""),
    });
  }
  if (!used && mu.reason) out.push({ k: "Why", v: mu.reason });
  return out;
}

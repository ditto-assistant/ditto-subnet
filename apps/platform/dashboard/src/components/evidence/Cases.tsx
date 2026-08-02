// Redacted per-case results, grouped by category and collapsed by default
// (monolith caseTally 6276–6281, tallyBadges 6282–6288, caseRow 6291–6302,
// casesSection 6307–6337). Category names come from the benchmark glossary
// payload; a missing key falls back to the raw slug, exactly as the
// original's "missing key falls back to the raw string" contract. Redacted:
// category / kind / score / verdict / latency / notes — never the answer key.
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import { fmtMs, fx } from "../../lib/format";
import { caseVerdict, scoreClass } from "../../lib/scoring";
import type { GlossaryPayload } from "../../types/bench";
import type { CaseResult } from "../../types/leaderboard";

export interface CategoryInfo {
  label: string;
  purpose: string;
}

/** Glossary-backed category naming (the monolith merged the glossary OVER
 * its inline fallback table; the SPA's fallback is the raw slug). */
export function categoryInfo(glossary: GlossaryPayload | undefined, key: string): CategoryInfo {
  const entry = (glossary?.categories || []).find((category) => category.key === key);
  const label = entry?.label || key;
  return {
    label,
    purpose: entry?.purpose || "A benchmark case in the “" + label + "” category.",
  };
}

interface Tally {
  pass: number;
  fail: number;
  partial: number;
  mean: number;
}

function caseTally(group: CaseResult[]): Tally {
  const t = { pass: 0, fail: 0, partial: 0, sum: 0 };
  group.forEach((c) => {
    t[caseVerdict(c)] += 1;
    t.sum += c.score || 0;
  });
  return {
    pass: t.pass,
    fail: t.fail,
    partial: t.partial,
    mean: group.length ? t.sum / group.length : 0,
  };
}

function TallyBadges(props: { tally: Tally }): JSX.Element {
  return (
    <>
      <Show when={props.tally.pass}>
        <span class="tb good" title={props.tally.pass + " passed"}>
          {props.tally.pass}&nbsp;✓
        </span>
      </Show>
      <Show when={props.tally.fail}>
        <span class="tb danger" title={props.tally.fail + " failed"}>
          {props.tally.fail}&nbsp;✗
        </span>
      </Show>
      <Show when={props.tally.partial}>
        <span class="tb muted" title={props.tally.partial + " partial"}>
          {props.tally.partial}&nbsp;~
        </span>
      </Show>
    </>
  );
}

function CaseRow(props: { c: CaseResult; info: CategoryInfo }): JSX.Element {
  const verdict = () => caseVerdict(props.c);
  return (
    <div class="crow">
      <div class="cmain">
        <span class={"ckind " + props.c.kind}>{props.c.kind}</span>
        <span class="ccat" style={{ cursor: "help" }} title={props.info.purpose}>
          {props.info.label}
        </span>
        <Show when={verdict() === "pass"}>
          <span class="pass" title="Passed">
            <span aria-hidden="true">✓</span>
            <span class="visually-hidden">passed</span>
          </span>
        </Show>
        <Show when={verdict() === "fail"}>
          <span class="fail" title="Failed">
            <span aria-hidden="true">✗</span>
            <span class="visually-hidden">failed</span>
          </span>
        </Show>
      </div>
      <div class="cscore">
        <Show when={props.c.latency_ms != null}>
          <span class="cmeta">{fmtMs(props.c.latency_ms as number)}</span>
        </Show>
        <span class={"cval " + scoreClass(props.c.score)}>{fx(props.c.score)}</span>
      </div>
      <Show when={props.c.notes && props.c.notes.length}>
        <div class="cnote">{(props.c.notes as string[]).join(" · ")}</div>
      </Show>
    </div>
  );
}

/** Tool groups first, worst mean first within each kind (6307–6337). */
export function CasesSection(props: {
  caseResults: CaseResult[] | null | undefined;
  glossary: () => GlossaryPayload | undefined;
}): JSX.Element {
  const cases = () => props.caseResults || [];
  const groups = (): { cat: string; cases: CaseResult[]; tally: Tally }[] => {
    const byCategory: Record<string, CaseResult[]> = {};
    cases().forEach((c) => {
      (byCategory[c.category] = byCategory[c.category] || []).push(c);
    });
    return Object.keys(byCategory)
      .sort((a, b) => {
        const ga = byCategory[a] as CaseResult[];
        const gb = byCategory[b] as CaseResult[];
        if ((ga[0] as CaseResult).kind !== (gb[0] as CaseResult).kind) {
          return (ga[0] as CaseResult).kind === "tool" ? -1 : 1;
        }
        return caseTally(ga).mean - caseTally(gb).mean;
      })
      .map((cat) => {
        const group = (byCategory[cat] as CaseResult[]).slice().sort((a, b) => a.score - b.score);
        return { cat, cases: group, tally: caseTally(group) };
      });
  };
  const totals = (): { pass: number; fail: number; partial: number } => {
    const out = { pass: 0, fail: 0, partial: 0 };
    groups().forEach((g) => {
      out.pass += g.tally.pass;
      out.fail += g.tally.fail;
      out.partial += g.tally.partial;
    });
    return out;
  };
  return (
    <Show when={cases().length}>
      <details class="cases">
        <summary>
          Per-question results <span class="muted">· {cases().length} cases</span>
        </summary>
        <div class="coverview">
          {cases().length} cases · <span class="good">{totals().pass} pass</span> ·{" "}
          <span class="danger">{totals().fail} fail</span>
          <Show when={totals().partial}>
            {" "}
            · <span class="muted">{totals().partial} partial</span>
          </Show>
        </div>
        <For each={groups()}>
          {(group) => {
            const info = () => categoryInfo(props.glossary(), group.cat);
            const kind = (group.cases[0] as CaseResult).kind;
            return (
              <details class="cgroup">
                <summary class="cgsum">
                  <span class={"ckind " + kind}>{kind}</span>
                  <span class="cgname" title={info().purpose}>
                    {info().label} <span class="muted">×{group.cases.length}</span>
                  </span>
                  <span class="cgtally">
                    <TallyBadges tally={group.tally} />
                    <span class={"cgmean " + scoreClass(group.tally.mean)}>
                      {fx(group.tally.mean)}
                    </span>
                  </span>
                </summary>
                <For each={group.cases}>
                  {(c) => <CaseRow c={c} info={categoryInfo(props.glossary(), c.category)} />}
                </For>
              </details>
            );
          }}
        </For>
      </details>
    </Show>
  );
}

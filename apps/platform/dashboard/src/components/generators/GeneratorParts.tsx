// Small pieces the generators page and the generator panel share: the case
// outcome bar, loading skeletons, per-section errors, and status chips.
import { For, Show } from "solid-js";
import type { JSX } from "solid-js";

import { StatusChip } from "../ui/StatusChip";
import type { GeneratorCaseCounts, GeneratorSubmissionStatus } from "./generator-data";

export const CASE_OUTCOMES = ["discriminating", "trivial", "unsolved", "duplicate"] as const;

export function CaseBar(props: { counts: Partial<GeneratorCaseCounts> }): JSX.Element {
  const parts = () => CASE_OUTCOMES.map((name) => [name, props.counts[name] ?? 0] as const);
  const total = () => parts().reduce((sum, [, count]) => sum + count, 0) || 1;
  return (
    <span
      class="gen-casebar"
      role="img"
      aria-label={parts()
        .map(([name, count]) => count + " " + name)
        .join(", ")}
    >
      <For each={parts().filter(([, count]) => count > 0)}>
        {([name, count]) => (
          <span
            class={"gen-case " + name}
            style={{ width: (count / total()) * 100 + "%" }}
            title={count + " " + name}
          />
        )}
      </For>
    </span>
  );
}

export function CaseLegend(): JSX.Element {
  return (
    <ul class="gen-case-legend" aria-label="Case outcomes">
      <li>
        <span class="gen-case discriminating" aria-hidden="true" /> Discriminating: some panel
        agents solve it and others miss it (earns credit)
      </li>
      <li>
        <span class="gen-case trivial" aria-hidden="true" /> Trivial: every eligible agent solves it
      </li>
      <li>
        <span class="gen-case unsolved" aria-hidden="true" /> Unsolved: too few agent owners solve
        it
      </li>
      <li>
        <span class="gen-case duplicate" aria-hidden="true" /> Duplicate: repeats an earlier
        column&apos;s or round&apos;s case
      </li>
    </ul>
  );
}

/** Whether any row reports a declared grading policy (an older Platform
 * reports none, and then the column is left out). */
export function hasGradingVersions(rows: { grading_bench_version?: number | null }[]): boolean {
  return rows.some((row) => row.grading_bench_version != null);
}

/** A quiet badge naming the pack grading policy a column declared. */
export function GradingBadge(props: { version: number | null | undefined }): JSX.Element {
  return (
    <Show when={props.version != null} fallback={<span class="gen-policy-none">—</span>}>
      <span
        class="gen-policy"
        title={"This pack's cases are graded under the bench v" + props.version + " grading policy"}
      >
        v{props.version}
      </span>
    </Show>
  );
}

/** Placeholder rows shaped like the table they stand in for. */
export function SkeletonRows(props: { rows: number; cols: number; label: string }): JSX.Element {
  return (
    <For each={Array.from({ length: props.rows }, (_, i) => i)}>
      {(row) => (
        <tr class="gen-skel-row" aria-hidden={row > 0 ? "true" : undefined}>
          <For each={Array.from({ length: props.cols }, (_, i) => i)}>
            {(col) => (
              <td>
                <Show when={row === 0 && col === 0} fallback={<span class="gen-skel" />}>
                  <span class="gen-skel" role="status" aria-label={props.label} />
                </Show>
              </td>
            )}
          </For>
        </tr>
      )}
    </For>
  );
}

/** One section's load failure, with a retry, instead of a blank space. When
 * `stale` is set the section still shows its last good data. */
export function SectionError(props: {
  what: string;
  onRetry: () => void;
  stale?: boolean;
}): JSX.Element {
  return (
    <div class="gen-error" role="alert">
      <span>
        {props.stale
          ? props.what + " could not be refreshed; showing the last loaded data."
          : props.what + " could not be loaded."}
      </span>
      <button type="button" class="btn" onClick={() => props.onRetry()}>
        Try again
      </button>
    </div>
  );
}

const SUBMISSION_CHIP: Record<GeneratorSubmissionStatus, readonly [string, string]> = {
  active: ["Active", "good"],
  superseded: ["Superseded", ""],
  rejected: ["Rejected", "bad"],
};

export function SubmissionStatusChip(props: {
  status: GeneratorSubmissionStatus | null | undefined;
  qualified?: boolean;
}): JSX.Element {
  const chip = () => (props.status ? (SUBMISSION_CHIP[props.status] ?? [props.status, ""]) : null);
  return (
    <Show when={chip()}>
      {(value) => (
        <StatusChip
          label={
            value()[0] +
            (props.status === "active" ? (props.qualified ? " · qualified" : " · qualifying") : "")
          }
          tone={props.status === "active" && !props.qualified ? "progress" : value()[1]}
        />
      )}
    </Show>
  );
}

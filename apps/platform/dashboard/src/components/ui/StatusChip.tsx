// The ubiquitous stage pill: `<span class="stage {tone}">{label}</span>`.
// Tones mirror the monolith's vocabulary (activityStage 6770–6788,
// fleetStatus 8305–8328, COMPONENT_HEALTH_CHIPS 8540–8546): "good", "warn",
// "bad", "progress", "paused", "unknown", or "" for the neutral pill.
import type { JSX } from "solid-js";

export type ChipTone = "good" | "warn" | "bad" | "progress" | "paused" | "unknown" | "";

/** A `[label, tone]` pair as the monolith's status helpers return them. */
export type ChipState = readonly [string, string];

export interface StatusChipProps {
  label: string;
  tone?: string | null;
  title?: string;
}

export function StatusChip(props: StatusChipProps): JSX.Element {
  return (
    <span class={"stage" + (props.tone ? " " + props.tone : "")} title={props.title}>
      {props.label}
    </span>
  );
}

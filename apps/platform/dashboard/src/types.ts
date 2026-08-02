// Wire shapes for every public API payload the dashboard touches, derived
// from field usage in the original single-file SPA. The API is the only data
// source; there is no shared package, so these interfaces mirror what the
// dashboard actually dereferences.
//
// Fields are optional/nullable by default — the UI is defensive about missing
// fields (older API generations omit newer ones) and the semantics are
// deliberately asymmetric where the original was: a missing `eligible` or
// `finalized` counts as true, while `registered` requires a strict === true.
// A field is required only where the original dereferenced it unconditionally.

export type * from "./types/bench";
export type * from "./types/fleet";
export type * from "./types/leaderboard";
export type * from "./types/pipeline";

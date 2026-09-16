// Package probe is the B5 native enforcement probe runner's library.
//
// In PR2 it measures no enforcement and writes no evidence record. It does two
// things:
//
//   - ResolveApprovedImage refuses a locally missing approved image instead of
//     letting any later command pull it, and pins the approved
//     repository@sha256 reference to its local content id.
//   - ObserveHostedGradingRequestedConfig builds hosted grading executors from
//     an approved grading profile through the production conversion
//     (GradingProfile.EnforcementProbeManifest, PhaseFactory.HostedGrading)
//     and reports the resource configuration Docker records for a created,
//     never-started executor container (Executor.InspectRequestedResourceConfig).
//
// The report uses its own schema, ObservationReportSchema, which the offline
// verifier refuses as a record. Requested configuration satisfies no catalog
// enforcement probe. Started-container cgroup reads, write attempts, limit
// events, pre-exec fixtures and cleanup scenarios are deferred
// to later PRs.
//
// NetAgent (B5 PR4) performs single network attempts for the root network
// collector and reports catalog outcome names; it never decides a match or
// writes a record. Positive checks are handshake-only.
//
// Nothing here runs from a host workflow, mints approval or reads a custody
// path.
package probe

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
// events, pre-exec fixtures, cleanup scenarios and network rules are deferred
// to later PRs.
//
// Nothing here runs from a host workflow, mints approval, reads a custody path
// or reaches anything but the Docker daemon selected by DOCKER_HOST.
package probe

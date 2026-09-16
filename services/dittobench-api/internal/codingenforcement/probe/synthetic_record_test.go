package probe

// Test-only synthetic record assembly. It proves the Go canonical record form
// equals the pinned golden evidence bytes the offline verifier hashes. It lives
// in a _test file on purpose: no production binary can assemble a record in
// PR2, because no evidence kind is measured end to end. A future collector must
// measure every required instance, preconditions and residue itself before any
// record-writing path exists.

import (
	"fmt"
	"slices"
	"strings"

	"github.com/ditto-assistant/dittobench-api/internal/codingenforcement/catalog"
)

// HostBinding is the record's host identity. The collector fills it from the
// post-collection preflight; the runner treats it as opaque.
type HostBinding struct {
	MachineIDSHA256      string
	BootID               string
	KernelRelease        string
	DaemonIdentitySHA256 string
	Subordinate          catalog.SubordinateIDs
	RouterNamespace      string
}

// ReleaseBinding is the record's release identity.
type ReleaseBinding struct {
	SourceRevision        string
	ReleaseManifestSHA256 string
	RuntimeArchiveSHA256  string
	ImageApprovalSHA256   map[string]string
}

// Env is the invariant context every record in one collection shares. It is
// supplied explicitly by the caller.
type Env struct {
	Host                         HostBinding
	Release                      ReleaseBinding
	Tools                        map[string]string
	ProfileInputs                map[string]string
	PreCollectionPreflightSHA256 string
	// Preconditions and Residue are supplied explicitly; nothing defaults them
	// to clear.
	Preconditions   map[string]any
	Residue         map[string]any
	StartedAtUnix   int64
	CompletedAtUnix int64
}

// Observation is one probe occurrence's measured result. Observed carries only
// int64, bool, string, and []string leaves, exactly the closed shapes the
// verifier accepts; matched is recomputed from the catalog, never stored here.
type Observation struct {
	ID             string
	Language       string
	EndpointSHA256 string
	Observed       map[string]any
}

// key identifies the required catalog instance an observation satisfies.
func (o Observation) key() instanceKey {
	return instanceKey{ID: o.ID, Language: o.Language, EndpointSHA256: o.EndpointSHA256}
}

type instanceKey struct {
	ID             string
	Language       string
	EndpointSHA256 string
}

// Phase is one catalog phase's observations with its measured wall-clock bounds.
type Phase struct {
	Name            string
	StartedAtUnix   int64
	CompletedAtUnix int64
	Observations    []Observation
}

// RecordResult is one assembled evidence record and its recomputed outcome.
type RecordResult struct {
	// Canonical is the record's canonical JSON bytes; its sha256 is its digest.
	Canonical []byte
	// Passed and Total are the recomputed matched counts. Test mode exposes
	// only these two integers, never a probe id or observed value.
	Passed int
	Total  int
	// Unmatched lists the probe ids whose recomputed matched was false. It is
	// used by the collector's own review, never placed in a record.
	Unmatched []string
}

// assembleSyntheticRecord builds one kind's record from a runner's phases. It fills each
// probe's expect from the embedded catalog, requires exactly the catalog's
// required instances, recomputes matched with catalog.Evaluate (the same rule
// the offline verifier reruns), and canonically encodes the result. The runner
// supplies observed values only; it can neither assert a matched nor omit a
// probe without AssembleRecord refusing.
func assembleSyntheticRecord(cat *catalog.Catalog, env Env, kind string, endpoints catalog.Endpoints, phases []Phase) (RecordResult, error) {
	if cat == nil || env.Preconditions == nil || env.Residue == nil {
		return RecordResult{}, fmt.Errorf("probe: catalog, preconditions and residue are required")
	}
	entry, ok := cat.Kinds[kind]
	if !ok {
		return RecordResult{}, fmt.Errorf("probe: unknown kind %q", kind)
	}
	instances, err := cat.RequiredInstances(kind, endpoints)
	if err != nil {
		return RecordResult{}, err
	}
	expects, err := catalogExpects(kind)
	if err != nil {
		return RecordResult{}, err
	}
	required := map[instanceKey]catalog.Instance{}
	for _, instance := range instances {
		required[instanceKey{instance.ID, instance.Language, instance.EndpointSHA256}] = instance
	}

	observed := map[instanceKey]map[string]any{}
	phaseByName := map[string][]Phase{}
	for _, phase := range phases {
		phaseByName[phase.Name] = append(phaseByName[phase.Name], phase)
		for _, observation := range phase.Observations {
			key := observation.key()
			instance, known := required[key]
			if !known {
				return RecordResult{}, fmt.Errorf("probe: %s produced an unexpected probe %q", kind, observation.ID)
			}
			if instance.Phase != phase.Name {
				return RecordResult{}, fmt.Errorf("probe: %s reported %q in phase %q not %q", kind, observation.ID, phase.Name, instance.Phase)
			}
			if _, seen := observed[key]; seen {
				return RecordResult{}, fmt.Errorf("probe: %s repeated probe %q", kind, observation.ID)
			}
			observed[key] = observation.Observed
		}
	}
	for key := range required {
		if _, ok := observed[key]; !ok {
			return RecordResult{}, fmt.Errorf("probe: %s is missing an observation for %q", kind, key.ID)
		}
	}

	outcomes := cat.OutcomeSet()
	result := RecordResult{Total: len(required)}
	phaseValues := make([]any, 0, len(entry.Phases))
	for _, phaseName := range entry.Phases {
		occurrences := phaseByName[phaseName]
		if len(occurrences) != 1 {
			return RecordResult{}, fmt.Errorf("probe: %s phase %q must appear exactly once", kind, phaseName)
		}
		phase := occurrences[0]
		probeValues := make([]map[string]any, 0)
		for _, instance := range instances {
			if instance.Phase != phaseName {
				continue
			}
			key := instanceKey{instance.ID, instance.Language, instance.EndpointSHA256}
			raw := observed[key]
			decoded, err := decodeObserved(raw)
			if err != nil {
				return RecordResult{}, fmt.Errorf("probe: %s observed for %q: %w", kind, instance.ID, err)
			}
			matched, err := catalog.Evaluate(instance.Expect, decoded, env.Host.Subordinate, outcomes, cat.Tolerances)
			if err != nil {
				return RecordResult{}, fmt.Errorf("probe: %s observed for %q is malformed: %w", kind, instance.ID, err)
			}
			if matched {
				result.Passed++
			} else {
				result.Unmatched = append(result.Unmatched, instance.ID)
			}
			probeValues = append(probeValues, map[string]any{
				"id":              instance.ID,
				"language":        nullable(instance.Language),
				"endpoint_sha256": nullable(instance.EndpointSHA256),
				"expect":          expects[instance.ID],
				"observed":        raw,
				"matched":         matched,
			})
		}
		slices.SortFunc(probeValues, func(left, right map[string]any) int {
			return strings.Compare(orderKey(left), orderKey(right))
		})
		anyProbes := make([]any, len(probeValues))
		for index, value := range probeValues {
			anyProbes[index] = value
		}
		phaseValues = append(phaseValues, map[string]any{
			"name":              phaseName,
			"started_at_unix":   phase.StartedAtUnix,
			"completed_at_unix": phase.CompletedAtUnix,
			"probes":            anyProbes,
		})
	}

	record := map[string]any{
		"schema":                          catalog.RecordSchema,
		"kind":                            kind,
		"coverage":                        catalog.Coverage,
		"not_covered":                     stringsToAny(catalog.NotCovered),
		"tolerances_version":              cat.Tolerances.Version,
		"host":                            hostValue(env.Host),
		"release":                         releaseValue(env.Release),
		"pre_collection_preflight_sha256": env.PreCollectionPreflightSHA256,
		"inputs":                          inputsValue(entry.Inputs, env.ProfileInputs),
		"endpoints":                       endpointValues(endpoints),
		"tools":                           toAnyMap(env.Tools),
		"preconditions":                   env.Preconditions,
		"phases":                          phaseValues,
		"residue":                         env.Residue,
		"started_at_unix":                 env.StartedAtUnix,
		"completed_at_unix":               env.CompletedAtUnix,
	}
	encoded, err := catalog.Canonical(record)
	if err != nil {
		return RecordResult{}, fmt.Errorf("probe: canonical encode: %w", err)
	}
	slices.Sort(result.Unmatched)
	result.Canonical = encoded
	return result, nil
}

// catalogExpects returns the exact expect object for every probe of a kind,
// read from the embedded catalog file so the bytes equal what the verifier
// compares against.
func catalogExpects(kind string) (map[string]any, error) {
	decoded, err := catalog.Decode(catalog.Bytes())
	if err != nil {
		return nil, err
	}
	top, ok := decoded.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("probe: catalog is malformed")
	}
	kinds, ok := top["kinds"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("probe: catalog kinds are malformed")
	}
	entry, ok := kinds[kind].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("probe: catalog kind %q is malformed", kind)
	}
	probes, ok := entry["probes"].([]any)
	if !ok {
		return nil, fmt.Errorf("probe: catalog probes are malformed")
	}
	expects := map[string]any{}
	for _, item := range probes {
		probe, ok := item.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("probe: catalog probe is malformed")
		}
		id, ok := probe["id"].(string)
		if !ok {
			return nil, fmt.Errorf("probe: catalog probe id is malformed")
		}
		expects[id] = probe["expect"]
	}
	return expects, nil
}

// decodeObserved round-trips an observed map to the canonical json.Number form
// catalog.Evaluate consumes, so matched is recomputed on the exact bytes the
// record carries.
func decodeObserved(observed map[string]any) (any, error) {
	raw, err := catalog.Canonical(observed)
	if err != nil {
		return nil, err
	}
	return catalog.Decode(raw)
}

func orderKey(probe map[string]any) string {
	id, _ := probe["id"].(string)
	language, _ := probe["language"].(string)
	endpoint, _ := probe["endpoint_sha256"].(string)
	return id + "\x00" + language + "\x00" + endpoint
}

func nullable(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func stringsToAny(values []string) []any {
	result := make([]any, len(values))
	for index, value := range values {
		result[index] = value
	}
	return result
}

func toAnyMap(values map[string]string) map[string]any {
	result := make(map[string]any, len(values))
	for key, value := range values {
		result[key] = value
	}
	return result
}

func hostValue(host HostBinding) map[string]any {
	return map[string]any{
		"machine_id_sha256":      host.MachineIDSHA256,
		"boot_id":                host.BootID,
		"kernel_release":         host.KernelRelease,
		"daemon_identity_sha256": host.DaemonIdentitySHA256,
		"router_namespace":       host.RouterNamespace,
		"subordinate_ids": map[string]any{
			"uid_start": host.Subordinate.UIDStart,
			"uid_count": host.Subordinate.UIDCount,
			"gid_start": host.Subordinate.GIDStart,
			"gid_count": host.Subordinate.GIDCount,
		},
	}
}

func releaseValue(release ReleaseBinding) map[string]any {
	return map[string]any{
		"source_revision":         release.SourceRevision,
		"release_manifest_sha256": release.ReleaseManifestSHA256,
		"runtime_archive_sha256":  release.RuntimeArchiveSHA256,
		"image_approval_sha256":   toAnyMap(release.ImageApprovalSHA256),
	}
}

func inputsValue(names []string, inputs map[string]string) map[string]any {
	result := make(map[string]any, len(names))
	for _, name := range names {
		result[name] = inputs[name]
	}
	return result
}

func endpointValues(endpoints catalog.Endpoints) []any {
	entries := make([]map[string]string, 0)
	if endpoints.Router != "" {
		entries = append(entries, map[string]string{"role": "router", "endpoint_sha256": endpoints.Router})
	}
	if endpoints.Proxy != "" {
		entries = append(entries, map[string]string{"role": "refusing_proxy", "endpoint_sha256": endpoints.Proxy})
	}
	for _, hash := range endpoints.Trusted {
		entries = append(entries, map[string]string{"role": "trusted", "endpoint_sha256": hash})
	}
	slices.SortFunc(entries, func(left, right map[string]string) int {
		if order := strings.Compare(left["role"], right["role"]); order != 0 {
			return order
		}
		return strings.Compare(left["endpoint_sha256"], right["endpoint_sha256"])
	})
	result := make([]any, len(entries))
	for index, entry := range entries {
		result[index] = map[string]any{"role": entry["role"], "endpoint_sha256": entry["endpoint_sha256"]}
	}
	return result
}

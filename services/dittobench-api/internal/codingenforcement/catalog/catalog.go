// Package catalog embeds the native enforcement probe catalog, its canonical
// JSON encoding and the expectation semantics that decide a probe's matched
// value.
//
// catalog-v1.json is the single source of truth. The offline Python evidence
// tool (infra/scripts/coding-native-evidence.py) reads the same file, and both
// sides pin the same golden vectors. Nothing here runs a probe, contacts a
// host or mints approval.
package catalog

import (
	"bytes"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"regexp"
	"slices"
	"strings"
)

//go:embed catalog-v1.json
var catalogBytes []byte

// Schemas and versioned constants. A change to any of these is a new version
// in the catalog file, this package and the Python verifier together.
const (
	CatalogSchema       = "dittobench-coding-native-enforcement-catalog-v1"
	RecordSchema        = "dittobench-coding-native-enforcement-evidence-v1"
	ReviewSchema        = "dittobench-coding-native-evidence-review-v1"
	Coverage            = "same_boot"
	FreshnessMaxSeconds = 21600
	// PreCollectionPreflightMaxAgeSeconds bounds a record's pre-collection
	// preflight age when collection starts.
	PreCollectionPreflightMaxAgeSeconds = 900
	// Rootless Docker maps container uid c >= 1 to subordinate start + c - 1.
	SubordinateMinStart = 100000
	SubordinateMinCount = 65536

	TolerancesVersion                   = "dittobench-coding-native-enforcement-tolerances-v1"
	CPUUsageMaxPermilleOfQuota          = 1150
	MemoryPeakMaxPermilleOfLimit        = 1000
	PidsMaxPermilleOfLimit              = 1000
	NofileMaxPermilleOfLimit            = 1000
	ScratchMaxPermilleOfLimit           = 1000
	LogMaxPermilleOfLimit               = 1000
	TimeoutElapsedMaxPermilleOfDeadline = 1100
	// Lower bounds: the limit event must happen at or near the limit, so an
	// idle or crashed burner never counts as enforcement.
	CPUUsageMinPermilleOfQuota   = 750
	MemoryPeakMinPermilleOfLimit = 900
	PidsMinPermilleOfLimit       = 1000
	NofileMinPermilleOfLimit     = 990
	ScratchMinPermilleOfLimit    = 950
	LogMinPermilleOfLimit        = 500
)

// Fixed catalog vocabularies.
var (
	Languages        = []string{"go", "node", "python", "rust"}
	RouterNamespaces = []string{"host", "rootless-netns"}
	NotCovered       = []string{"daemon_restart_recovery", "reboot_recovery"}
	// Kinds is also the fixed collection order; records never overlap.
	Kinds         = []string{"network_enforcement", "resource_enforcement", "preexec_confinement", "cleanup_recovery"}
	ProfileInputs = []string{"connectivity_profile_sha256", "execution_profile_sha256", "grading_profile_sha256"}
	BindSources   = []string{"memory_limit_bytes", "cpu_quota_millis", "pids_limit", "scratch_limit_bytes", "nofile_limit", "log_limit_bytes", "hidden_command_timeout_ms", "visible_command_timeout_ms"}
	// CommandTimeoutSources evidences every hosted grading test group timeout.
	CommandTimeoutSources = map[string]string{"hidden_command_timeout_ms": "hidden", "visible_command_timeout_ms": "visible"}
	// Network phases observed before the connectivity profile expires, and the
	// phase that reaches its expiry.
	NetworkPreExpiryPhases = []string{"active", "stop_rollback"}
	NetworkExpiryPhase     = "expiry"
)

// ResourceContainer names where a resource container's limits come from.
type ResourceContainer struct {
	Profile       string `json:"profile"`
	Scratch       string `json:"scratch"`
	NofileLimit   int64  `json:"nofile_limit"`
	LogLimitBytes int64  `json:"log_limit_bytes"`
}

// VersionedResourceContainers mirror the sandbox (nofile 1024, 8 MiB local
// log) and executor (24 KiB model-visible output, Rust /out carve-out) code.
var VersionedResourceContainers = map[string]ResourceContainer{
	"harness":            {Profile: "execution_profile_sha256", Scratch: "full", NofileLimit: 1024, LogLimitBytes: 8388608},
	"executor_authoring": {Profile: "execution_profile_sha256", Scratch: "executor", NofileLimit: 1024, LogLimitBytes: 24576},
	"executor_grading":   {Profile: "grading_profile_sha256", Scratch: "executor", NofileLimit: 1024, LogLimitBytes: 24576},
}

var bindFields = map[string]string{ExpectProfileEqual: "profile", ExpectBounded: "limit", ExpectSupervisorTimeout: "deadline_ms", ExpectZeroRetained: "limit"}

// ZeroRetainedProbe is the one probe whose policy retains no candidate
// output: hosted grading keeps 0 bytes, so no per-mille floor applies.
const ZeroRetainedProbe = "executor_grading.log_bound"

// Probe scopes: once per record, once per approved language image, or once
// per trusted endpoint listed in the record.
const (
	ScopeHost            = "host"
	ScopeLanguage        = "language"
	ScopeTrustedEndpoint = "trusted_endpoint"
	ScopeRouterEndpoint  = "router_endpoint"
	ScopeProxyEndpoint   = "proxy_endpoint"
)

var (
	probeID   = regexp.MustCompile(`^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}$`)
	name      = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
	sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// Tolerances are integer per-mille bounds, so no float enters a record.
type Tolerances struct {
	Version                             string `json:"version"`
	CPUUsageMaxPermilleOfQuota          int64  `json:"cpu_usage_max_permille_of_quota"`
	MemoryPeakMaxPermilleOfLimit        int64  `json:"memory_peak_max_permille_of_limit"`
	PidsMaxPermilleOfLimit              int64  `json:"pids_max_permille_of_limit"`
	NofileMaxPermilleOfLimit            int64  `json:"nofile_max_permille_of_limit"`
	ScratchMaxPermilleOfLimit           int64  `json:"scratch_max_permille_of_limit"`
	LogMaxPermilleOfLimit               int64  `json:"log_max_permille_of_limit"`
	TimeoutElapsedMaxPermilleOfDeadline int64  `json:"timeout_elapsed_max_permille_of_deadline"`
	CPUUsageMinPermilleOfQuota          int64  `json:"cpu_usage_min_permille_of_quota"`
	MemoryPeakMinPermilleOfLimit        int64  `json:"memory_peak_min_permille_of_limit"`
	PidsMinPermilleOfLimit              int64  `json:"pids_min_permille_of_limit"`
	NofileMinPermilleOfLimit            int64  `json:"nofile_min_permille_of_limit"`
	ScratchMinPermilleOfLimit           int64  `json:"scratch_min_permille_of_limit"`
	LogMinPermilleOfLimit               int64  `json:"log_min_permille_of_limit"`
}

// Permille returns the named tolerance.
func (t Tolerances) Permille(tolerance string) (int64, bool) {
	switch tolerance {
	case "cpu_usage_max_permille_of_quota":
		return t.CPUUsageMaxPermilleOfQuota, true
	case "memory_peak_max_permille_of_limit":
		return t.MemoryPeakMaxPermilleOfLimit, true
	case "pids_max_permille_of_limit":
		return t.PidsMaxPermilleOfLimit, true
	case "nofile_max_permille_of_limit":
		return t.NofileMaxPermilleOfLimit, true
	case "scratch_max_permille_of_limit":
		return t.ScratchMaxPermilleOfLimit, true
	case "log_max_permille_of_limit":
		return t.LogMaxPermilleOfLimit, true
	case "timeout_elapsed_max_permille_of_deadline":
		return t.TimeoutElapsedMaxPermilleOfDeadline, true
	case "cpu_usage_min_permille_of_quota":
		return t.CPUUsageMinPermilleOfQuota, true
	case "memory_peak_min_permille_of_limit":
		return t.MemoryPeakMinPermilleOfLimit, true
	case "pids_min_permille_of_limit":
		return t.PidsMinPermilleOfLimit, true
	case "nofile_min_permille_of_limit":
		return t.NofileMinPermilleOfLimit, true
	case "scratch_min_permille_of_limit":
		return t.ScratchMinPermilleOfLimit, true
	case "log_min_permille_of_limit":
		return t.LogMinPermilleOfLimit, true
	}
	return 0, false
}

// VersionedTolerances are the constants every catalog must carry.
var VersionedTolerances = Tolerances{
	Version:                             TolerancesVersion,
	CPUUsageMaxPermilleOfQuota:          CPUUsageMaxPermilleOfQuota,
	MemoryPeakMaxPermilleOfLimit:        MemoryPeakMaxPermilleOfLimit,
	PidsMaxPermilleOfLimit:              PidsMaxPermilleOfLimit,
	NofileMaxPermilleOfLimit:            NofileMaxPermilleOfLimit,
	ScratchMaxPermilleOfLimit:           ScratchMaxPermilleOfLimit,
	LogMaxPermilleOfLimit:               LogMaxPermilleOfLimit,
	TimeoutElapsedMaxPermilleOfDeadline: TimeoutElapsedMaxPermilleOfDeadline,
	CPUUsageMinPermilleOfQuota:          CPUUsageMinPermilleOfQuota,
	MemoryPeakMinPermilleOfLimit:        MemoryPeakMinPermilleOfLimit,
	PidsMinPermilleOfLimit:              PidsMinPermilleOfLimit,
	NofileMinPermilleOfLimit:            NofileMinPermilleOfLimit,
	ScratchMinPermilleOfLimit:           ScratchMinPermilleOfLimit,
	LogMinPermilleOfLimit:               LogMinPermilleOfLimit,
}

// scopeRoles names the endpoint role each endpoint-bound scope expands over.
var scopeRoles = map[string]string{ScopeTrustedEndpoint: "trusted", ScopeRouterEndpoint: "router", ScopeProxyEndpoint: "refusing_proxy"}

// Endpoints are a network record's endpoint hashes by role.
type Endpoints struct {
	Trusted []string
	Router  string
	Proxy   string
}

// Bounds limits how many endpoints of one role a record lists.
type Bounds struct {
	Min int `json:"min"`
	Max int `json:"max"`
}

// Probe is one catalog entry.
type Probe struct {
	ID     string            `json:"id"`
	Phase  string            `json:"phase"`
	Scope  string            `json:"scope"`
	Expect Expectation       `json:"expect"`
	Bind   map[string]string `json:"bind"`
}

// Kind is the probe set of one evidence kind.
type Kind struct {
	Inputs        []string          `json:"inputs"`
	EndpointRoles map[string]Bounds `json:"endpoint_roles"`
	Phases        []string          `json:"phases"`
	Probes        []Probe           `json:"probes"`
}

// Catalog is the decoded catalog-v1.json.
type Catalog struct {
	Schema              string                       `json:"schema"`
	RecordSchema        string                       `json:"record_schema"`
	ReviewSchema        string                       `json:"review_schema"`
	Languages           []string                     `json:"languages"`
	RouterNamespaces    []string                     `json:"router_namespaces"`
	Coverage            string                       `json:"coverage"`
	NotCovered          []string                     `json:"not_covered"`
	FreshnessMaxSeconds int64                        `json:"freshness_max_seconds"`
	PreflightMaxAge     int64                        `json:"pre_collection_preflight_max_age_seconds"`
	ResourceContainers  map[string]ResourceContainer `json:"resource_containers"`
	Tolerances          Tolerances                   `json:"tolerances"`
	Outcomes            []string                     `json:"outcomes"`
	Kinds               map[string]Kind              `json:"kinds"`
}

// Instance is one required probe occurrence in a record. Language and
// EndpointSHA256 are empty when the record carries null.
type Instance struct {
	ID             string
	Language       string
	EndpointSHA256 string
	Phase          string
	Expect         Expectation
}

// Bytes returns a copy of the embedded catalog file.
func Bytes() []byte { return bytes.Clone(catalogBytes) }

// SHA256 is the hex digest of the embedded catalog file bytes, the value a
// record binds as tools.catalog_sha256.
func SHA256() string {
	sum := sha256.Sum256(catalogBytes)
	return hex.EncodeToString(sum[:])
}

// Load decodes and validates the embedded catalog.
func Load() (*Catalog, error) { return Parse(catalogBytes) }

// Parse decodes and validates catalog bytes with closed keys.
func Parse(raw []byte) (*Catalog, error) {
	// The strict decoder refuses duplicate keys and non-integer numbers first,
	// and exact-case closed keys are checked before encoding/json, whose struct
	// decoding would otherwise accept COVERAGE for coverage.
	decoded, err := Decode(raw)
	if err != nil {
		return nil, fmt.Errorf("catalog: %w", err)
	}
	if err := closedKeys(decoded); err != nil {
		return nil, fmt.Errorf("catalog: %w", err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var value Catalog
	if err := decoder.Decode(&value); err != nil {
		return nil, fmt.Errorf("catalog: decode: %w", err)
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("catalog: trailing data")
	}
	if err := value.validate(); err != nil {
		return nil, fmt.Errorf("catalog: %w", err)
	}
	return &value, nil
}

func (c *Catalog) validate() error {
	switch {
	case c.Schema != CatalogSchema, c.RecordSchema != RecordSchema, c.ReviewSchema != ReviewSchema:
		return errors.New("schema differs")
	case !slices.Equal(c.Languages, Languages):
		return errors.New("languages differ")
	case !slices.Equal(c.RouterNamespaces, RouterNamespaces):
		return errors.New("router namespaces differ")
	case c.Coverage != Coverage, !slices.Equal(c.NotCovered, NotCovered):
		return errors.New("coverage differs")
	case c.FreshnessMaxSeconds != FreshnessMaxSeconds:
		return errors.New("freshness differs")
	case c.PreflightMaxAge != PreCollectionPreflightMaxAgeSeconds:
		return errors.New("preflight age differs")
	case !maps.Equal(c.ResourceContainers, VersionedResourceContainers):
		return errors.New("resource containers differ")
	case c.Tolerances != VersionedTolerances:
		return errors.New("tolerances differ")
	}
	if len(c.Outcomes) == 0 || !slices.IsSorted(c.Outcomes) || len(slices.Compact(slices.Clone(c.Outcomes))) != len(c.Outcomes) {
		return errors.New("outcomes are malformed")
	}
	for _, outcome := range c.Outcomes {
		if !name.MatchString(outcome) {
			return errors.New("outcomes are malformed")
		}
	}
	if len(c.Kinds) != len(Kinds) {
		return errors.New("kinds differ")
	}
	outcomes := c.OutcomeSet()
	for _, kindName := range Kinds {
		kind, ok := c.Kinds[kindName]
		if !ok {
			return errors.New("kinds differ")
		}
		if err := kind.validate(kindName, outcomes); err != nil {
			return fmt.Errorf("%s: %w", kindName, err)
		}
	}
	network := c.Kinds["network_enforcement"].Phases
	for _, phase := range append(slices.Clone(NetworkPreExpiryPhases), NetworkExpiryPhase) {
		if !slices.Contains(network, phase) {
			return errors.New("network phases lack the expiry window")
		}
	}
	timeouts := map[string]bool{}
	zeroRetained := false
	for _, probe := range c.Kinds["resource_enforcement"].Probes {
		if probe.Expect.Type == ExpectSupervisorTimeout {
			timeouts[probe.Bind["deadline_ms"]] = true
		}
		zeroRetained = zeroRetained || (probe.ID == ZeroRetainedProbe && probe.Expect.Type == ExpectZeroRetained)
	}
	if !zeroRetained {
		return errors.New("grading output retention is not an exact zero-byte assertion")
	}
	for source := range CommandTimeoutSources {
		if !timeouts[source] {
			return errors.New("not every grading test group timeout is evidenced")
		}
	}
	return nil
}

func (k Kind) validate(kindName string, outcomes map[string]bool) error {
	if k.Inputs == nil || !slices.IsSorted(k.Inputs) || len(slices.Compact(slices.Clone(k.Inputs))) != len(k.Inputs) {
		return errors.New("inputs are malformed")
	}
	for _, input := range k.Inputs {
		if !slices.Contains(ProfileInputs, input) {
			return errors.New("inputs are malformed")
		}
	}
	if k.EndpointRoles == nil {
		return errors.New("endpoint roles are malformed")
	}
	for role, bounds := range k.EndpointRoles {
		if !name.MatchString(role) || bounds.Min < 0 || bounds.Max < max(1, bounds.Min) {
			return errors.New("endpoint role is malformed")
		}
	}
	if len(k.Phases) == 0 {
		return errors.New("phases are malformed")
	}
	phases := map[string]bool{} // phase -> used by a probe
	for _, phase := range k.Phases {
		if _, repeated := phases[phase]; repeated || !name.MatchString(phase) {
			return errors.New("phases are malformed")
		}
		phases[phase] = false
	}
	if len(k.Probes) == 0 {
		return errors.New("probes are empty")
	}
	seen := map[string]bool{}
	for _, probe := range k.Probes {
		if !probeID.MatchString(probe.ID) || seen[probe.ID] {
			return errors.New("probe id is malformed or repeated")
		}
		seen[probe.ID] = true
		if _, known := phases[probe.Phase]; !known {
			return fmt.Errorf("%s: phase is unknown", probe.ID)
		}
		phases[probe.Phase] = true
		switch probe.Scope {
		case ScopeHost, ScopeLanguage:
		case ScopeTrustedEndpoint, ScopeRouterEndpoint, ScopeProxyEndpoint:
			if _, ok := k.EndpointRoles[scopeRoles[probe.Scope]]; !ok {
				return fmt.Errorf("%s: needs %s endpoints", probe.ID, scopeRoles[probe.Scope])
			}
		default:
			return fmt.Errorf("%s: scope is unknown", probe.ID)
		}
		if err := probe.Expect.validate(outcomes); err != nil {
			return fmt.Errorf("%s: %w", probe.ID, err)
		}
		if err := probe.validateBind(kindName); err != nil {
			return fmt.Errorf("%s: %w", probe.ID, err)
		}
	}
	for _, used := range phases {
		if !used {
			return errors.New("a phase has no probes")
		}
	}
	return nil
}

// validateBind requires every limit a resource probe reports to name its
// approved source, so no record can declare its own limit.
func (probe Probe) validateBind(kindName string) error {
	if probe.Bind == nil {
		return errors.New("bind is malformed")
	}
	zeroRetained := probe.Expect.Type == ExpectZeroRetained
	if zeroRetained != (probe.ID == ZeroRetainedProbe) || (zeroRetained && kindName != "resource_enforcement") {
		return errors.New("zero-byte retention fits only grading output")
	}
	field, bindable := bindFields[probe.Expect.Type]
	if kindName != "resource_enforcement" || !bindable {
		if len(probe.Bind) != 0 {
			return errors.New("must not bind limits")
		}
		return nil
	}
	container, _, _ := strings.Cut(probe.ID, ".")
	spec, ok := VersionedResourceContainers[container]
	if !ok {
		return errors.New("container is unknown")
	}
	source, ok := probe.Bind[field]
	if len(probe.Bind) != 1 || !ok || !slices.Contains(BindSources, source) {
		return errors.New("bind is malformed")
	}
	group, timeout := CommandTimeoutSources[source]
	if timeout != (probe.Expect.Type == ExpectSupervisorTimeout) {
		return errors.New("bind source does not fit its type")
	}
	if timeout && spec.Profile != "grading_profile_sha256" {
		return errors.New("no approved command timeout")
	}
	if timeout && probe.ID != container+".supervisor_timeout."+group {
		return errors.New("names another test group")
	}
	if probe.Expect.Type == ExpectZeroRetained && source != "log_limit_bytes" {
		return errors.New("zero-byte retention must bind the output limit")
	}
	return nil
}

// OutcomeSet is the observed outcome vocabulary.
func (c *Catalog) OutcomeSet() map[string]bool {
	result := make(map[string]bool, len(c.Outcomes))
	for _, outcome := range c.Outcomes {
		result[outcome] = true
	}
	return result
}

// RequiredInstances expands one kind's probes over every language image and
// every trusted endpoint hash, in record order: catalog phase order, then id,
// language and endpoint hash within a phase.
func (c *Catalog) RequiredInstances(kindName string, endpoints Endpoints) ([]Instance, error) {
	kind, ok := c.Kinds[kindName]
	if !ok {
		return nil, errors.New("catalog: kind is unknown")
	}
	if bounds, trusted := kind.EndpointRoles["trusted"]; trusted {
		if len(endpoints.Trusted) < max(1, bounds.Min) || len(endpoints.Trusted) > bounds.Max {
			return nil, errors.New("catalog: trusted endpoint count is outside its bounds")
		}
	} else if len(endpoints.Trusted) != 0 {
		return nil, errors.New("catalog: kind has no trusted endpoints")
	}
	_, router := kind.EndpointRoles["router"]
	_, proxy := kind.EndpointRoles["refusing_proxy"]
	if router != (endpoints.Router != "") || proxy != (endpoints.Proxy != "") {
		return nil, errors.New("catalog: router or proxy endpoint does not fit the kind")
	}
	seen := map[string]bool{}
	for _, endpoint := range append(slices.Clone(endpoints.Trusted), endpoints.Router, endpoints.Proxy) {
		if endpoint == "" {
			continue
		}
		if !sha256Hex.MatchString(endpoint) || seen[endpoint] {
			return nil, errors.New("catalog: endpoint hash is malformed or repeated")
		}
		seen[endpoint] = true
	}
	byRole := map[string][]string{"trusted": endpoints.Trusted, "router": {endpoints.Router}, "refusing_proxy": {endpoints.Proxy}}
	var result []Instance
	for _, probe := range kind.Probes {
		base := Instance{ID: probe.ID, Phase: probe.Phase, Expect: probe.Expect}
		switch probe.Scope {
		case ScopeHost:
			result = append(result, base)
		case ScopeLanguage:
			for _, language := range Languages {
				instance := base
				instance.Language = language
				result = append(result, instance)
			}
		case ScopeTrustedEndpoint, ScopeRouterEndpoint, ScopeProxyEndpoint:
			for _, endpoint := range byRole[scopeRoles[probe.Scope]] {
				instance := base
				instance.EndpointSHA256 = endpoint
				result = append(result, instance)
			}
		}
	}
	slices.SortFunc(result, func(left, right Instance) int {
		if order := slices.Index(kind.Phases, left.Phase) - slices.Index(kind.Phases, right.Phase); order != 0 {
			return order
		}
		return strings.Compare(left.ID+"\x00"+left.Language+"\x00"+left.EndpointSHA256, right.ID+"\x00"+right.Language+"\x00"+right.EndpointSHA256)
	})
	return result, nil
}

var (
	catalogKeys   = []string{"coverage", "freshness_max_seconds", "kinds", "languages", "not_covered", "outcomes", "pre_collection_preflight_max_age_seconds", "record_schema", "resource_containers", "review_schema", "router_namespaces", "schema", "tolerances"}
	toleranceKeys = []string{"cpu_usage_max_permille_of_quota", "cpu_usage_min_permille_of_quota", "log_max_permille_of_limit", "log_min_permille_of_limit", "memory_peak_max_permille_of_limit", "memory_peak_min_permille_of_limit", "nofile_max_permille_of_limit", "nofile_min_permille_of_limit", "pids_max_permille_of_limit", "pids_min_permille_of_limit", "scratch_max_permille_of_limit", "scratch_min_permille_of_limit", "timeout_elapsed_max_permille_of_deadline", "version"}
	containerKeys = []string{"log_limit_bytes", "nofile_limit", "profile", "scratch"}
	kindKeys      = []string{"endpoint_roles", "inputs", "phases", "probes"}
	boundsKeys    = []string{"max", "min"}
	probeKeys     = []string{"bind", "expect", "id", "phase", "scope"}
)

// exactObject requires an object whose keys are exactly keys (sorted), by case.
func exactObject(value any, keys []string) (map[string]any, error) {
	object, ok := value.(map[string]any)
	if !ok {
		return nil, errors.New("expected an object")
	}
	present := slices.Sorted(maps.Keys(object))
	if !slices.Equal(present, keys) {
		return nil, errors.New("keys are not the exact closed set")
	}
	return object, nil
}

func closedKeys(decoded any) error {
	top, err := exactObject(decoded, catalogKeys)
	if err != nil {
		return err
	}
	if _, err := exactObject(top["tolerances"], toleranceKeys); err != nil {
		return fmt.Errorf("tolerances: %w", err)
	}
	containers, ok := top["resource_containers"].(map[string]any)
	if !ok {
		return errors.New("resource containers are malformed")
	}
	for _, container := range containers {
		if _, err := exactObject(container, containerKeys); err != nil {
			return fmt.Errorf("resource container: %w", err)
		}
	}
	kinds, ok := top["kinds"].(map[string]any)
	if !ok {
		return errors.New("kinds are malformed")
	}
	for kindName, value := range kinds {
		kind, err := exactObject(value, kindKeys)
		if err != nil {
			return fmt.Errorf("%s: %w", kindName, err)
		}
		roles, ok := kind["endpoint_roles"].(map[string]any)
		if !ok {
			return fmt.Errorf("%s: endpoint roles are malformed", kindName)
		}
		for _, bounds := range roles {
			if _, err := exactObject(bounds, boundsKeys); err != nil {
				return fmt.Errorf("%s endpoint role: %w", kindName, err)
			}
		}
		probes, ok := kind["probes"].([]any)
		if !ok {
			return fmt.Errorf("%s: probes are malformed", kindName)
		}
		for _, probe := range probes {
			if _, err := exactObject(probe, probeKeys); err != nil {
				return fmt.Errorf("%s probe: %w", kindName, err)
			}
		}
	}
	return nil
}

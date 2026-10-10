package codinghostedruntime

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"testing"
)

// The native host prerequisites role renders this record for its fixed host from
// its port constants. .github/workflows/dittobench.yml watches both files.
const (
	prerequisitesTemplate = "../../../../infra/ansible/roles/coding_hosted_prerequisites/templates/host-prerequisites.json.j2"
	prerequisitesVars     = "../../../../infra/ansible/roles/coding_hosted_prerequisites/vars/main.yml"
)

type prerequisitesPorts struct{ router, proxy string }

func rolePorts(t *testing.T) prerequisitesPorts {
	t.Helper()
	vars, err := os.ReadFile(prerequisitesVars)
	if err != nil {
		t.Fatal(err)
	}
	value := func(name string) string {
		matches := regexp.MustCompile(`(?m)^`+name+`: ([1-9][0-9]{3,4})$`).FindAllSubmatch(vars, -1)
		if len(matches) != 1 {
			t.Fatalf("role constant %s is not one canonical port", name)
		}
		return string(matches[0][1])
	}
	return prerequisitesPorts{
		router: value("coding_hosted_prerequisites_router_port"),
		proxy:  value("coding_hosted_prerequisites_proxy_port"),
	}
}

type prerequisitesRecord struct {
	Schema         string `json:"schema"`
	ShadowOnly     *bool  `json:"shadow_only"`
	WeightEligible *bool  `json:"weight_eligible"`
	RouterListen   string `json:"router_listen"`
	EgressNetwork  string `json:"egress_network"`
	EgressProxy    string `json:"egress_proxy"`
	CandidateUID   uint32 `json:"candidate_uid"`
	CandidateGID   uint32 `json:"candidate_gid"`
}

func renderPrerequisites(t *testing.T, address string) prerequisitesRecord {
	t.Helper()
	template, err := os.ReadFile(prerequisitesTemplate)
	if err != nil {
		t.Fatal(err)
	}
	ports := rolePorts(t)
	substitutions := []struct{ placeholder, value string }{
		{"{{ coding_hosted_prerequisites_host_address }}", address},
		{"{{ coding_hosted_prerequisites_router_port }}", ports.router},
		{"{{ coding_hosted_prerequisites_proxy_port }}", ports.proxy},
	}
	if bytes.Count(template, []byte("{{")) != 4 {
		t.Fatal("prerequisites template has unexpected substitutions")
	}
	for _, substitution := range substitutions {
		want := 1
		if substitution.value == address {
			want = 2
		}
		if bytes.Count(template, []byte(substitution.placeholder)) != want {
			t.Fatal("prerequisites template has unexpected substitutions")
		}
		template = bytes.ReplaceAll(template, []byte(substitution.placeholder), []byte(substitution.value))
	}
	decoder := json.NewDecoder(bytes.NewReader(template))
	decoder.DisallowUnknownFields()
	var record prerequisitesRecord
	if err := decoder.Decode(&record); err != nil {
		t.Fatal(err)
	}
	if record.Schema != "dittobench-coding-hosted-host-prerequisites-v2" || record.ShadowOnly == nil || !*record.ShadowOnly || record.WeightEligible == nil || *record.WeightEligible {
		t.Fatal("prerequisites record gates drifted")
	}
	return record
}

func loadWithPrerequisites(t *testing.T, record prerequisitesRecord) (*runtimeConfig, error) {
	t.Helper()
	wire, path := fixture(t)
	wire.RouterListen, wire.EgressNetwork, wire.EgressProxy = record.RouterListen, record.EgressNetwork, record.EgressProxy
	wire.CandidateUID, wire.CandidateGID = record.CandidateUID, record.CandidateGID
	writeConfig(t, path, wire)
	config, err := loadConfigChecked(path, testExecutable)
	if _, statErr := os.Stat(filepath.Join(wire.StateRoot, "consumed")); !os.IsNotExist(statErr) {
		t.Fatal("prerequisites validation consumed attempt")
	}
	return config, err
}

func TestPrerequisitesRoleRecordPassesRuntimeLoader(t *testing.T) {
	// The role guard admits 10.33.0.2 through 10.33.0.253; check both bounds.
	ports := rolePorts(t)
	for _, address := range []string{"10.33.0.2", "10.33.0.253"} {
		record := renderPrerequisites(t, address)
		// Every language runtime uses 10001; the Rust executor requires it.
		if record.CandidateUID != 10001 || record.CandidateGID != 10001 {
			t.Fatal("candidate identity drifted from the fixed executor identity")
		}
		config, err := loadWithPrerequisites(t, record)
		if err != nil {
			t.Fatalf("runtime rejected the role record for %s", address)
		}
		if config.docker.HostGatewayIP != address || config.docker.EgressNetwork != "ditto-coding-restricted" ||
			config.docker.EgressProxy != "http://"+address+":"+ports.proxy || config.publicBase != "http://host.docker.internal:"+ports.router {
			t.Fatal("runtime did not bind the role record")
		}
	}
}

func TestPrerequisitesRecordShapeFailsClosedOutsidePrivateIPv4(t *testing.T) {
	// The role guard refuses these before rendering; the runtime refuses them too.
	for _, address := range []string{"127.0.0.1", "0.0.0.0", "8.8.8.8", "169.254.169.254", "100.64.0.1"} {
		if _, err := loadWithPrerequisites(t, renderPrerequisites(t, address)); err != ErrConfig {
			t.Fatalf("runtime accepted the role record for %s", address)
		}
	}
}

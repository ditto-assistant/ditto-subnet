package codingcertservice

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const (
	testUID    = 61001
	testGID    = 61002
	testDigest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
	testPack   = "cb608113db0cc31001fe0a7294854453061f9e85d1471520100ce99eca97a903"
)

func validEnvironment() map[string]string {
	return map[string]string{
		EnabledEnvironment:           "true",
		ServiceUIDEnvironment:        "61001",
		ControlGIDEnvironment:        "61002",
		RouterListenEnvironment:      "10.203.0.1:11438",
		EgressNetworkEnvironment:     "ditto-coding-certification-egress",
		EgressProxyEnvironment:       "http://10.203.0.1:3128",
		RuntimeRepositoryEnvironment: "registry.invalid/ditto/coding-runtime",
		RuntimeDigestEnvironment:     testDigest,
		PackManifestEnvironment:      testPack,
		RouterHelperEnvironment:      strings.Repeat("3", 64),
		"CREDENTIALS_DIRECTORY":      "/run/credentials/ditto-coding-certification.service",
	}
}

func lookup(values map[string]string) func(string) string {
	return func(name string) string { return values[name] }
}

func TestServiceIsDisabledByDefault(t *testing.T) {
	for _, value := range []string{"", "false", "TRUE", "True", "1", "yes", "on", " true", "true\n"} {
		values := validEnvironment()
		values[EnabledEnvironment] = value
		if _, err := ConfigFromEnvironment(lookup(values), testUID); !errors.Is(err, ErrDisabled) {
			t.Errorf("enable flag %q: err=%v", value, err)
		}
	}
	if _, err := ConfigFromEnvironment(lookup(map[string]string{}), testUID); !errors.Is(err, ErrDisabled) {
		t.Fatalf("empty environment: err=%v", err)
	}
	if _, err := ConfigFromEnvironment(nil, testUID); !errors.Is(err, ErrDisabled) {
		t.Fatalf("nil environment: err=%v", err)
	}
	if err := Run(t.Context(), lookup(map[string]string{}), os.Geteuid()); !errors.Is(err, ErrDisabled) {
		t.Fatalf("default Run err=%v", err)
	}
}

func TestConfigUsesFixedPathsAndItsOwnSettings(t *testing.T) {
	// Shared scorer settings must never be read.
	values := validEnvironment()
	values["DITTOBENCH_SANDBOX_EGRESS_PROXY"] = "http://172.30.0.2:3128"
	values["DITTOBENCH_OPENROUTER_SHIM_CA_BUNDLE_PATH"] = "/var/lib/dittobench-openrouter-shim/ca-bundle.pem"
	values["DOCKER_HOST"] = "tcp://127.0.0.1:2375"
	config, err := ConfigFromEnvironment(lookup(values), testUID)
	if err != nil {
		t.Fatal(err)
	}
	if config.ControlSocketPath != "/run/ditto-coding-certification/control.sock" ||
		config.DockerSocketPath != "/run/ditto-coding-certification-docker/docker.sock" ||
		config.Runtime.DockerHost != "unix:///run/ditto-coding-certification-docker/docker.sock" ||
		config.Runtime.HostGatewayIP != "10.203.0.1" || config.Runtime.EgressProxy != "http://10.203.0.1:3128" ||
		config.CertificationRoot != CertificationRoot || config.RouterHelperPath != RouterHelperPath ||
		config.ServiceUID != testUID || config.ControlGID != testGID || config.RuntimeImageDigest != testDigest ||
		config.Admission != (AdmissionLimits{MaxLifetime: 2 * time.Hour, IdleTimeout: 15 * time.Minute}) {
		t.Fatalf("config=%+v", config)
	}
}

func TestConfigBoundsTheAdmissionWindow(t *testing.T) {
	for _, test := range []struct {
		lifetime, idle string
		want           AdmissionLimits
	}{
		{"", "", AdmissionLimits{MaxLifetime: 2 * time.Hour, IdleTimeout: 15 * time.Minute}},
		{"3600", "600", AdmissionLimits{MaxLifetime: time.Hour, IdleTimeout: 10 * time.Minute}},
		{"43200", "43200", AdmissionLimits{MaxLifetime: 12 * time.Hour, IdleTimeout: 12 * time.Hour}},
		{"14400", "", AdmissionLimits{MaxLifetime: 4 * time.Hour, IdleTimeout: 15 * time.Minute}},
	} {
		values := validEnvironment()
		values[MaxLifetimeEnvironment] = test.lifetime
		values[IdleTimeoutEnvironment] = test.idle
		config, err := ConfigFromEnvironment(lookup(values), testUID)
		if err != nil || config.Admission != test.want {
			t.Fatalf("lifetime=%q idle=%q admission=%+v err=%v", test.lifetime, test.idle, config.Admission, err)
		}
	}
	for name, values := range map[string][2]string{
		"lifetime below one hour":      {"3599", ""},
		"lifetime above twelve hours":  {"43201", ""},
		"zero lifetime":                {"0", ""},
		"negative lifetime":            {"-3600", ""},
		"lifetime with a unit":         {"2h", ""},
		"lifetime with a leading zero": {"07200", ""},
		"lifetime with whitespace":     {" 7200", ""},
		"idle below ten minutes":       {"", "599"},
		"idle above the lifetime":      {"3600", "3601"},
		"idle default above lifetime":  {"", "7201"},
		"idle with a unit":             {"", "15m"},
		"idle overflow":                {"", "99999999999999999999"},
	} {
		t.Run(name, func(t *testing.T) {
			environment := validEnvironment()
			environment[MaxLifetimeEnvironment] = values[0]
			environment[IdleTimeoutEnvironment] = values[1]
			if _, err := ConfigFromEnvironment(lookup(environment), testUID); !errors.Is(err, ErrConfig) {
				t.Fatalf("err=%v", err)
			}
		})
	}
}

func TestConfigRefusesEveryInvalidValueWithoutEchoingIt(t *testing.T) {
	mutations := map[string]func(map[string]string){
		"root service uid":       func(v map[string]string) { v[ServiceUIDEnvironment] = "0" },
		"missing service uid":    func(v map[string]string) { delete(v, ServiceUIDEnvironment) },
		"other service uid":      func(v map[string]string) { v[ServiceUIDEnvironment] = "61003" },
		"non-canonical uid":      func(v map[string]string) { v[ServiceUIDEnvironment] = "061001" },
		"root control gid":       func(v map[string]string) { v[ControlGIDEnvironment] = "0" },
		"negative control gid":   func(v map[string]string) { v[ControlGIDEnvironment] = "-1" },
		"missing control gid":    func(v map[string]string) { delete(v, ControlGIDEnvironment) },
		"router loopback":        func(v map[string]string) { v[RouterListenEnvironment] = "127.0.0.1:11438" },
		"router public":          func(v map[string]string) { v[RouterListenEnvironment] = "8.8.8.8:11438" },
		"router ipv6":            func(v map[string]string) { v[RouterListenEnvironment] = "[fd00::1]:11438" },
		"router privileged port": func(v map[string]string) { v[RouterListenEnvironment] = "10.203.0.1:443" },
		"router all addresses":   func(v map[string]string) { v[RouterListenEnvironment] = "0.0.0.0:11438" },
		"router hostname":        func(v map[string]string) { v[RouterListenEnvironment] = "gateway:11438" },
		"router leading zero":    func(v map[string]string) { v[RouterListenEnvironment] = "10.203.0.01:11438" },
		"missing network":        func(v map[string]string) { delete(v, EgressNetworkEnvironment) },
		"missing proxy":          func(v map[string]string) { delete(v, EgressProxyEnvironment) },
		"public proxy":           func(v map[string]string) { v[EgressProxyEnvironment] = "http://8.8.8.8:3128" },
		"unconfined seccomp":     func(v map[string]string) { v[SeccompEnvironment] = "unconfined" },
		"unconfined apparmor":    func(v map[string]string) { v[AppArmorEnvironment] = "unconfined" },
		"repository tag":         func(v map[string]string) { v[RuntimeRepositoryEnvironment] = "registry.invalid/runtime:latest" },
		"missing repository":     func(v map[string]string) { delete(v, RuntimeRepositoryEnvironment) },
		"image tag":              func(v map[string]string) { v[RuntimeDigestEnvironment] = "latest" },
		"uppercase image digest": func(v map[string]string) { v[RuntimeDigestEnvironment] = strings.ToUpper(testDigest) },
		"short image digest":     func(v map[string]string) { v[RuntimeDigestEnvironment] = testDigest[:70] },
		"missing pack digest":    func(v map[string]string) { delete(v, PackManifestEnvironment) },
		"missing helper digest":  func(v map[string]string) { delete(v, RouterHelperEnvironment) },
		"short helper digest":    func(v map[string]string) { v[RouterHelperEnvironment] = "3" },
		"uppercase pack digest":  func(v map[string]string) { v[PackManifestEnvironment] = strings.ToUpper(testPack) },
		"no credentials":         func(v map[string]string) { delete(v, "CREDENTIALS_DIRECTORY") },
		"credentials elsewhere":  func(v map[string]string) { v["CREDENTIALS_DIRECTORY"] = "/var/lib/secrets" },
		"credentials traversal":  func(v map[string]string) { v["CREDENTIALS_DIRECTORY"] = "/run/credentials/../x" },
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			values := validEnvironment()
			mutate(values)
			_, err := ConfigFromEnvironment(lookup(values), testUID)
			if !errors.Is(err, ErrConfig) {
				t.Fatalf("err=%v", err)
			}
			for _, value := range values {
				if len(value) > 4 && strings.Contains(err.Error(), value) {
					t.Fatalf("refusal echoes %q: %v", value, err)
				}
			}
		})
	}
	if _, err := ConfigFromEnvironment(lookup(validEnvironment()), 0); !errors.Is(err, ErrConfig) {
		t.Fatalf("root euid err=%v", err)
	}
	if _, err := ConfigFromEnvironment(lookup(validEnvironment()), testUID+1); !errors.Is(err, ErrConfig) {
		t.Fatalf("other euid err=%v", err)
	}
}

// The service reads its environment only through the getenv it is given, and
// never builds a runtime from the ordinary scorer's defaults or settings.
func TestServiceSourceReadsNoAmbientOrSharedSettings(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		body, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		for _, forbidden := range []string{
			"os.Getenv", "os.LookupEnv", "os.Environ", "NewLocalDocker(", "DITTOBENCH_SANDBOX_",
			"OPENROUTER_SHIM", "SSL_CERT_FILE", "HTTPS_PROXY", "\"tcp\"", "\"tcp4\"", "net.Listen(",
		} {
			if strings.Contains(string(body), forbidden) {
				t.Errorf("%s contains %q", name, forbidden)
			}
		}
	}
}

package catalog

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
)

func TestEmbeddedCatalogIsTheCheckedInFile(t *testing.T) {
	raw, err := os.ReadFile("catalog-v1.json")
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(raw, Bytes()) || SHA256() != digestHex(raw) {
		t.Fatal("embedded catalog differs from catalog-v1.json")
	}
}

func TestCatalogDefinesEveryRequiredProbeSet(t *testing.T) {
	loaded, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	network := loaded.Kinds["network_enforcement"]
	first := network.Probes[0]
	if first.ID != "candidate.router.source" || first.Phase != "active" || first.Scope != ScopeRouterEndpoint ||
		first.Expect.Type != ExpectOutcomeIn || strings.Join(first.Expect.Accept, ",") != "container_address" {
		t.Fatalf("first network probe = %+v", first)
	}
	if bounds := network.EndpointRoles["refusing_proxy"]; bounds != (Bounds{Min: 1, Max: 1}) {
		t.Fatalf("refusing proxy bounds = %+v", bounds)
	}
	trusted := []string{strings.Repeat("a", 64), strings.Repeat("b", 64)}
	counts := map[string]int{}
	router, proxy := strings.Repeat("c", 64), strings.Repeat("d", 64)
	for _, kind := range Kinds {
		endpoints := Endpoints{}
		if kind == "network_enforcement" {
			endpoints = Endpoints{Trusted: trusted, Router: router, Proxy: proxy}
		}
		instances, err := loaded.RequiredInstances(kind, endpoints)
		if err != nil {
			t.Fatal(err)
		}
		counts[kind] = len(instances)
	}
	// 13 host, 5 router and 2 proxy probes plus 7 trusted-endpoint probes for two
	// endpoints; 35 and 22 probes for each of the four language images; 10
	// cleanup probes.
	want := map[string]int{
		"network_enforcement":  13 + 5 + 2 + 7*2,
		"resource_enforcement": 35 * 4,
		"preexec_confinement":  22 * 4,
		"cleanup_recovery":     10,
	}
	for kind, count := range want {
		if counts[kind] != count {
			t.Errorf("%s instances = %d, want %d", kind, counts[kind], count)
		}
	}
	for name, endpoints := range map[string]Endpoints{
		"raw endpoint":      {Trusted: []string{"10.20.0.7:5432"}, Router: router, Proxy: proxy},
		"no trusted":        {Router: router, Proxy: proxy},
		"empty trusted":     {Trusted: []string{}, Router: router, Proxy: proxy},
		"duplicate trusted": {Trusted: []string{trusted[0], trusted[0]}, Router: router, Proxy: proxy},
		"too many trusted":  {Trusted: manyHashes(33), Router: router, Proxy: proxy},
		"no router":         {Trusted: trusted, Proxy: proxy},
		"no proxy":          {Trusted: trusted, Router: router},
		"router is proxy":   {Trusted: trusted, Router: router, Proxy: router},
		"router is trusted": {Trusted: trusted, Router: trusted[0], Proxy: proxy},
		"raw router":        {Trusted: trusted, Router: "10.30.0.4:18080", Proxy: proxy},
	} {
		if _, err := loaded.RequiredInstances("network_enforcement", endpoints); err == nil {
			t.Errorf("network %s accepted", name)
		}
	}
	for name, endpoints := range map[string]Endpoints{
		"trusted": {Trusted: trusted},
		"router":  {Router: router},
		"proxy":   {Proxy: proxy},
	} {
		if _, err := loaded.RequiredInstances("cleanup_recovery", endpoints); err == nil {
			t.Errorf("cleanup accepted a %s endpoint", name)
		}
	}
	instances, err := loaded.RequiredInstances("network_enforcement", Endpoints{Trusted: trusted, Router: router, Proxy: proxy})
	if err != nil {
		t.Fatal(err)
	}
	for _, instance := range instances {
		switch {
		case strings.HasPrefix(instance.ID, "candidate.router."):
			if instance.EndpointSHA256 != router {
				t.Errorf("%s is not bound to the router", instance.ID)
			}
		case strings.HasPrefix(instance.ID, "candidate.proxy."):
			if instance.EndpointSHA256 != proxy {
				t.Errorf("%s is not bound to the proxy", instance.ID)
			}
		}
	}
	for _, probe := range loaded.Kinds["resource_enforcement"].Probes {
		field, bindable := bindFields[probe.Expect.Type]
		if bindable && probe.Bind[field] == "" {
			t.Errorf("%s reports an unbound %s", probe.ID, field)
		}
	}
	for _, kind := range loaded.Kinds {
		for _, probe := range kind.Probes {
			if strings.Contains(probe.ID, "reboot") || strings.Contains(probe.ID, "restart") {
				t.Errorf("%s claims coverage beyond the same boot", probe.ID)
			}
		}
	}
}

func TestCatalogRefusals(t *testing.T) {
	mutate := func(change func(map[string]any)) []byte {
		decoded, err := Decode(Bytes())
		if err != nil {
			t.Fatal(err)
		}
		value := decoded.(map[string]any)
		change(value)
		raw, err := json.Marshal(value)
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	kind := func(value map[string]any, name string) map[string]any {
		return value["kinds"].(map[string]any)[name].(map[string]any)
	}
	for name, change := range map[string]func(map[string]any){
		"cpu tolerance": func(v map[string]any) {
			v["tolerances"].(map[string]any)["cpu_usage_max_permille_of_quota"] = 1200
		},
		"not covered": func(v map[string]any) { v["not_covered"] = []any{"daemon_restart_recovery"} },
		"coverage":    func(v map[string]any) { v["coverage"] = "cross_boot" },
		"unknown key": func(v map[string]any) { v["approved"] = true },
		"repeated probe": func(v map[string]any) {
			cleanup := kind(v, "cleanup_recovery")
			probes := cleanup["probes"].([]any)
			cleanup["probes"] = append(probes, probes[0])
		},
		"unknown expect": func(v map[string]any) {
			probe := kind(v, "cleanup_recovery")["probes"].([]any)[0].(map[string]any)
			probe["expect"] = map[string]any{"type": "anything"}
		},
		"unknown outcome": func(v map[string]any) {
			probe := kind(v, "network_enforcement")["probes"].([]any)[0].(map[string]any)
			probe["expect"] = map[string]any{"type": "outcome_in", "accept": []any{"teleported"}}
		},
		"extra expect key": func(v map[string]any) {
			probe := kind(v, "network_enforcement")["probes"].([]any)[0].(map[string]any)
			probe["expect"].(map[string]any)["tolerance"] = "cpu_usage_max_permille_of_quota"
		},
		"unused phase": func(v map[string]any) {
			cleanup := kind(v, "cleanup_recovery")
			cleanup["phases"] = append(cleanup["phases"].([]any), "reboot")
		},
		"case variant key": func(v map[string]any) { v["COVERAGE"] = "same_boot" },
		"case variant tolerance": func(v map[string]any) {
			v["tolerances"].(map[string]any)["CPU_usage_max_permille_of_quota"] = 1150
		},
		"case variant probe key": func(v map[string]any) {
			probe := kind(v, "cleanup_recovery")["probes"].([]any)[0].(map[string]any)
			probe["Phase"] = probe["phase"]
			delete(probe, "phase")
		},
		"case variant kind key": func(v map[string]any) {
			cleanup := kind(v, "cleanup_recovery")
			cleanup["Inputs"] = cleanup["inputs"]
			delete(cleanup, "inputs")
		},
		"resource limit unbound": func(v map[string]any) {
			probe := kind(v, "resource_enforcement")["probes"].([]any)[0].(map[string]any)
			probe["bind"] = map[string]any{}
		},
		"network probe binds a limit": func(v map[string]any) {
			probe := kind(v, "network_enforcement")["probes"].([]any)[0].(map[string]any)
			probe["bind"] = map[string]any{"limit": "pids_limit"}
		},
		"authoring timeout bound": func(v map[string]any) {
			resource := kind(v, "resource_enforcement")
			for _, item := range resource["probes"].([]any) {
				probe := item.(map[string]any)
				if probe["id"] == "executor_grading.supervisor_timeout.hidden" {
					probe["id"] = "executor_authoring.supervisor_timeout.hidden"
				}
			}
		},
		"visible timeout unevidenced": func(v map[string]any) {
			resource := kind(v, "resource_enforcement")
			var kept []any
			for _, item := range resource["probes"].([]any) {
				if item.(map[string]any)["id"] != "executor_grading.supervisor_timeout.visible" {
					kept = append(kept, item)
				}
			}
			resource["probes"] = kept
		},
		"timeout group mismatch": func(v map[string]any) {
			resource := kind(v, "resource_enforcement")
			for _, item := range resource["probes"].([]any) {
				probe := item.(map[string]any)
				if probe["id"] == "executor_grading.supervisor_timeout.visible" {
					probe["bind"] = map[string]any{"deadline_ms": "hidden_command_timeout_ms"}
				}
			}
		},
		"floor from another resource": func(v map[string]any) {
			for _, item := range kind(v, "resource_enforcement")["probes"].([]any) {
				probe := item.(map[string]any)
				if probe["id"] == "harness.memory_oom" {
					probe["expect"].(map[string]any)["floor"] = "cpu_usage_min_permille_of_quota"
				}
			}
		},
		"bounded without floor": func(v map[string]any) {
			for _, item := range kind(v, "resource_enforcement")["probes"].([]any) {
				probe := item.(map[string]any)
				if probe["id"] == "harness.pids_cap" {
					delete(probe["expect"].(map[string]any), "floor")
				}
			}
		},
		"router scope without router role": func(v map[string]any) {
			probe := kind(v, "cleanup_recovery")["probes"].([]any)[0].(map[string]any)
			probe["scope"] = "router_endpoint"
		},
		"expiry phase renamed": func(v map[string]any) {
			network := kind(v, "network_enforcement")
			network["phases"] = []any{"active", "stop_rollback", "lapse", "failed_start"}
			for _, item := range network["probes"].([]any) {
				probe := item.(map[string]any)
				if probe["phase"] == "expiry" {
					probe["phase"] = "lapse"
				}
			}
		},
		"container limit drift": func(v map[string]any) {
			v["resource_containers"].(map[string]any)["harness"].(map[string]any)["nofile_limit"] = 4096
		},
		"grading log back on a per-mille floor": func(v map[string]any) {
			withProbe(kind(v, "resource_enforcement"), "executor_grading.log_bound", func(probe map[string]any) {
				probe["expect"] = map[string]any{"type": "bounded", "floor": "log_min_permille_of_limit", "tolerance": "log_max_permille_of_limit"}
			})
		},
		"grading log as a loose exact zero": func(v map[string]any) {
			withProbe(kind(v, "resource_enforcement"), "executor_grading.log_bound", func(probe map[string]any) {
				probe["expect"] = map[string]any{"type": "exact", "value": map[string]any{"retained_bytes": 0}}
				probe["bind"] = map[string]any{}
			})
		},
		"zero retention on the harness log": func(v map[string]any) {
			withProbe(kind(v, "resource_enforcement"), "harness.log_bound", func(probe map[string]any) {
				probe["expect"] = map[string]any{"type": "zero_retained"}
			})
		},
		"zero retention unbound": func(v map[string]any) {
			withProbe(kind(v, "resource_enforcement"), "executor_grading.log_bound", func(probe map[string]any) {
				probe["bind"] = map[string]any{}
			})
		},
		"zero retention bound to another limit": func(v map[string]any) {
			withProbe(kind(v, "resource_enforcement"), "executor_grading.log_bound", func(probe map[string]any) {
				probe["bind"] = map[string]any{"limit": "pids_limit"}
			})
		},
		"zero retention with a floor key": func(v map[string]any) {
			withProbe(kind(v, "resource_enforcement"), "executor_grading.log_bound", func(probe map[string]any) {
				probe["expect"].(map[string]any)["floor"] = "log_min_permille_of_limit"
			})
		},
		"repeated phase": func(v map[string]any) {
			cleanup := kind(v, "cleanup_recovery")
			phases := cleanup["phases"].([]any)
			cleanup["phases"] = append(phases, phases[0])
		},
	} {
		if _, err := Parse(mutate(change)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func manyHashes(count int) []string {
	result := make([]string, count)
	for index := range result {
		result[index] = fmt.Sprintf("%064x", index+1)
	}
	return result
}

// withProbe edits one probe of a decoded kind by id.
func withProbe(kind map[string]any, id string, edit func(map[string]any)) {
	for _, item := range kind["probes"].([]any) {
		if probe := item.(map[string]any); probe["id"] == id {
			edit(probe)
		}
	}
}

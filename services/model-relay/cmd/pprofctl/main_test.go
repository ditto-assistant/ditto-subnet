package main

import "testing"

func TestTargetPortMapping(t *testing.T) {
	target := target{project: "ditto-app-dev", zone: "us-central1-a", instance: "ditto-platform-prod", service: "platform-relay-2"}
	if err := target.validate(); err != nil {
		t.Fatal(err)
	}
	if got, err := target.pprofPort(); err != nil || got != 11011 {
		t.Fatalf("pprof port = %d, %v; want 11011", got, err)
	}
	target.mainPort = 9000
	if got, err := target.pprofPort(); err != nil || got != 12000 {
		t.Fatalf("override pprof port = %d, %v; want 12000", got, err)
	}
}

func TestProfileURLAndValidation(t *testing.T) {
	if got, err := profileURL("profile", 15); err != nil || got != "/debug/pprof/profile?seconds=15" {
		t.Fatalf("profile URL = %q, %v", got, err)
	}
	if _, err := profileURL("profile", 0); err == nil {
		t.Fatal("zero duration accepted")
	}
	if _, err := profileURL("unknown", 15); err == nil {
		t.Fatal("unknown profile accepted")
	}
	bad := target{project: "ditto-app-dev;id", zone: "us-central1-a", instance: "prod", service: "platform-relay-1"}
	if err := bad.validate(); err == nil {
		t.Fatal("unsafe gcloud target accepted")
	}
	badContainer := target{
		project: "ditto-app-dev", zone: "us-central1-a", instance: "prod",
		service: "custom", mainPort: 8000, containerService: "sandbox-docker;id",
	}
	if err := badContainer.validate(); err == nil {
		t.Fatal("unsafe container service accepted")
	}
	for _, path := range []string{
		"/debug/pprof/'; id; echo '",
		"/debug/pprof/`id`",
		"/debug/pprof/$(id)",
		"/debug/pprof/heap|cat",
		"/debug/pprof/heap\n",
	} {
		if safePath.MatchString(path) {
			t.Errorf("unsafe path accepted: %q", path)
		}
	}
}

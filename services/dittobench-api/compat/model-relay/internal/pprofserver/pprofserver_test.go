package pprofserver

import "testing"

func TestResolvePort(t *testing.T) {
	if got, err := resolvePort(11434, ""); err != nil || got != 14434 {
		t.Fatalf("derived port = %d, %v; want 14434", got, err)
	}
	if _, err := resolvePort(64000, ""); err == nil {
		t.Fatal("out-of-range derived port accepted")
	}
}

func TestListenAddressCannotPublishProfiler(t *testing.T) {
	if got := listenAddress(14434); got != "127.0.0.1:14434" {
		t.Fatalf("listenAddress = %q; want loopback-only address", got)
	}
}

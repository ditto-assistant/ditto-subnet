package catalog

import (
	"bytes"
	"encoding/json"
	"os"
	"testing"
)

// screenerProbeContract is the screener's copy of the catalog shape its
// full-size tool-catalog probe offers a harness (ditto_screener
// tool_catalog_probe). The screener sizes its synthetic catalog from it, so it
// must move with this package: a contract that grows the menu, or changes the
// schema toolkit tools render, has to update that file in the same change.
const screenerProbeContract = "../../../workers/screener/ditto_screener/data/tool-catalog-probe-v18.json"

func TestScreenerToolCatalogProbeContractMatchesTheCatalog(t *testing.T) {
	raw, err := os.ReadFile(screenerProbeContract)
	if err != nil {
		t.Fatalf("read screener probe contract: %v", err)
	}
	var contract struct {
		BenchVersion          int             `json:"bench_version"`
		MaxCatalogTools       int             `json:"max_catalog_tools"`
		ToolkitToolParameters json.RawMessage `json:"toolkit_tool_parameters"`
	}
	if err := json.Unmarshal(raw, &contract); err != nil {
		t.Fatalf("decode screener probe contract: %v", err)
	}
	newest := catalogSizes[len(catalogSizes)-1]
	if contract.BenchVersion != newest.BenchVersion {
		t.Errorf("screener probe contract bench_version = %d, want the newest catalog contract %d", contract.BenchVersion, newest.BenchVersion)
	}
	if contract.MaxCatalogTools != MaxCatalogTools() {
		t.Errorf("screener probe contract max_catalog_tools = %d, want MaxCatalogTools() = %d", contract.MaxCatalogTools, MaxCatalogTools())
	}
	if got, want := canonicalJSON(t, contract.ToolkitToolParameters), canonicalJSON(t, v18ToolkitSchema); !bytes.Equal(got, want) {
		t.Errorf("screener probe toolkit_tool_parameters = %s, want v18ToolkitSchema %s", got, want)
	}
}

func canonicalJSON(t *testing.T, raw []byte) []byte {
	t.Helper()
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		t.Fatalf("decode %s: %v", raw, err)
	}
	out, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	return out
}

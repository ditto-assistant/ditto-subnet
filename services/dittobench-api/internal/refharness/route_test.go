package refharness

import (
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

func tools() []protocol.ToolDefinition {
	return []protocol.ToolDefinition{
		{Name: "search_web", Description: "Search the public web for current information."},
		{Name: "search_memories", Description: "Search the user's long-term memories by semantic query."},
		{Name: "set_theme", Description: "Change the app's color theme."},
	}
}

func TestRouteDeterministic(t *testing.T) {
	p := "search the web for the latest quantum computing news"
	a := Route(p, tools())
	b := Route(p, tools())
	if len(a) != len(b) {
		t.Fatalf("non-deterministic length: %d vs %d", len(a), len(b))
	}
	if len(a) == 1 && a[0].Name != b[0].Name {
		t.Fatalf("non-deterministic choice: %s vs %s", a[0].Name, b[0].Name)
	}
}

func TestRouteKeywordMatch(t *testing.T) {
	cases := []struct {
		prompt string
		want   string // "" means expect no call
	}{
		{"search the public web for current news", "search_web"},
		{"change the color theme to dark", "set_theme"},
		{"hiya, just saying hello", ""}, // no tool keyword → call nothing
	}
	for _, c := range cases {
		got := Route(c.prompt, tools())
		if c.want == "" {
			if len(got) != 0 {
				t.Fatalf("%q: expected no call, got %v", c.prompt, got)
			}
			continue
		}
		if len(got) != 1 || got[0].Name != c.want {
			t.Fatalf("%q: expected %s, got %v", c.prompt, c.want, got)
		}
	}
}

func TestRouteEmitsObjectArgs(t *testing.T) {
	got := Route("search the public web", tools())
	if len(got) != 1 {
		t.Fatalf("expected one call, got %d", len(got))
	}
	if string(got[0].Args) != "{}" {
		t.Fatalf("expected {} args, got %s", got[0].Args)
	}
}

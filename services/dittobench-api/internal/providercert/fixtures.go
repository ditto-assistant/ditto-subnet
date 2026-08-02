package providercert

import "encoding/json"

type scenario struct {
	Name       string
	Streaming  bool
	Messages   []map[string]any
	Tools      []map[string]any
	ToolChoice any
	Parallel   *bool
}

func boolPtr(v bool) *bool { return &v }

func scenarios() []scenario {
	lookup := map[string]any{
		"type": "function",
		"function": map[string]any{
			"name":        "lookup_record",
			"description": "Look up one certification fixture record by id.",
			"parameters": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"record_id": map[string]any{"type": "string", "enum": []string{"record-17"}},
				},
				"required":             []string{"record_id"},
				"additionalProperties": false,
			},
		},
	}
	weather := func(name string) map[string]any {
		return map[string]any{
			"type": "function",
			"function": map[string]any{
				"name":        name,
				"description": "Return deterministic weather for one city.",
				"parameters": map[string]any{
					"type": "object",
					"properties": map[string]any{
						"city": map[string]any{"type": "string"},
					},
					"required":             []string{"city"},
					"additionalProperties": false,
				},
			},
		}
	}
	base := []map[string]any{{
		"role":    "system",
		"content": "You are a deterministic API certification fixture. Follow tool instructions exactly and do not add prose.",
	}}
	return []scenario{
		{
			Name:       "text",
			Messages:   appendCopy(base, map[string]any{"role": "user", "content": "Reply with exactly CERT_TEXT_OK"}),
			ToolChoice: "none",
		},
		{
			Name:       "forced_tool",
			Messages:   appendCopy(base, map[string]any{"role": "user", "content": "Use lookup_record for record-17. Do not answer directly."}),
			Tools:      []map[string]any{lookup},
			ToolChoice: map[string]any{"type": "function", "function": map[string]any{"name": "lookup_record"}},
			Parallel:   boolPtr(false),
		},
		{
			Name:       "auto_tool",
			Messages:   appendCopy(base, map[string]any{"role": "user", "content": "Use lookup_record for record-17. Do not answer directly."}),
			Tools:      []map[string]any{lookup},
			ToolChoice: "auto",
			Parallel:   boolPtr(false),
		},
		{
			Name:       "parallel_tools",
			Messages:   appendCopy(base, map[string]any{"role": "user", "content": "Call weather_paris for Paris and weather_tokyo for Tokyo in parallel. Make both calls and do not answer directly."}),
			Tools:      []map[string]any{weather("weather_paris"), weather("weather_tokyo")},
			ToolChoice: "auto",
			Parallel:   boolPtr(true),
		},
		{
			Name:       "streaming_tool",
			Streaming:  true,
			Messages:   appendCopy(base, map[string]any{"role": "user", "content": "Use lookup_record for record-17. Do not answer directly."}),
			Tools:      []map[string]any{lookup},
			ToolChoice: "auto",
			Parallel:   boolPtr(false),
		},
		{
			Name:       "continuation",
			Messages:   appendCopy(base, map[string]any{"role": "user", "content": "Use lookup_record for record-17, then report its status."}),
			Tools:      []map[string]any{lookup},
			ToolChoice: "auto",
			Parallel:   boolPtr(false),
		},
		{
			Name:       "tool_error_continuation",
			Messages:   appendCopy(base, map[string]any{"role": "user", "content": "Use lookup_record for record-17. If the tool errors, reply exactly CERT_TOOL_ERROR_HANDLED."}),
			Tools:      []map[string]any{lookup},
			ToolChoice: "auto",
			Parallel:   boolPtr(false),
		},
	}
}

func appendCopy(in []map[string]any, value map[string]any) []map[string]any {
	out := append([]map[string]any(nil), in...)
	return append(out, value)
}

func rawObject(s string) json.RawMessage { return json.RawMessage(s) }

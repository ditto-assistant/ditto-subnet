export class ProtocolError extends Error {}

function object(value, field) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ProtocolError(`${field} must be an object`);
  }
  return value;
}

function string(value, field, required = false) {
  if (value === undefined || value === null) {
    if (required) throw new ProtocolError(`${field} must be a non-empty string`);
    return "";
  }
  if (typeof value !== "string" || (required && value.length === 0)) {
    throw new ProtocolError(`${field} must be a${required ? " non-empty" : ""} string`);
  }
  return value;
}

function array(value, field) {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw new ProtocolError(`${field} must be an array`);
  return value;
}

export function parseSeed(value) {
  const input = object(value, "seed request");
  const wave = input.wave ?? 0;
  if (!Number.isInteger(wave) || wave < 0) throw new ProtocolError("wave must be a non-negative integer");
  return {
    userId: string(input.user_id || "miner", "user_id", true),
    wave,
    pairs: array(input.pairs, "pairs").map((raw) => {
      const pair = object(raw, "pairs entry");
      return {
        pairId: string(pair.pair_id, "pair_id", true),
        sessionId: string(pair.session_id, "session_id"),
        timestamp: string(pair.timestamp, "timestamp"),
        prompt: string(pair.prompt, "prompt"),
        response: string(pair.response, "response"),
      };
    }),
    subjects: array(input.subjects, "subjects").map((item) => object(item, "subjects entry")),
    links: array(input.links, "links").map((item) => object(item, "links entry")),
  };
}

export function parseRun(value) {
  const input = object(value, "run request");
  return {
    caseId: string(input.case_id, "case_id", true),
    systemPrompt: string(input.system_prompt, "system_prompt"),
    userInput: string(input.user_input, "user_input"),
    userId: string(input.user_id || "miner", "user_id", true),
    toolEndpoint: string(input.tool_endpoint, "tool_endpoint"),
    tools: array(input.tools, "tools").map((raw) => {
      const tool = object(raw, "tools entry");
      const parameters = tool.parameters ?? { type: "object", properties: {} };
      object(parameters, "tool parameters");
      return {
        name: string(tool.name, "tool name", true),
        description: string(tool.description, "tool description"),
        parameters,
      };
    }),
  };
}

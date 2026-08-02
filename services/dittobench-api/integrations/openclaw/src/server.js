import http from "node:http";
import path from "node:path";

import { parseRun, parseSeed, ProtocolError } from "./protocol.js";
import { OpenClawRunner } from "./runtime.js";
import { OpenClawState } from "./state.js";

const MAX_REQUEST_BYTES = 256 * 1024 * 1024;

export class AdapterService {
  constructor(state, runner) {
    const configured = process.env.DITTOBENCH_DB || "/tmp/dittobench.db";
    this.state = state ?? new OpenClawState(path.join(path.dirname(configured), "dittobench-openclaw"));
    this.runner = runner ?? new OpenClawRunner(this.state);
  }

  async seed(value) {
    const request = parseSeed(value);
    const result = await this.state.seed(request);
    await this.runner.sync(request);
    return result;
  }

  async run(value) {
    return this.runner.run(parseRun(value));
  }
}

async function readJson(request) {
  const declared = Number(request.headers["content-length"] || 0);
  if (!Number.isInteger(declared) || declared <= 0 || declared > MAX_REQUEST_BYTES) {
    throw new ProtocolError("request body is empty or too large");
  }
  const chunks = [];
  let received = 0;
  for await (const chunk of request) {
    received += chunk.length;
    if (received > MAX_REQUEST_BYTES) throw new ProtocolError("request body is too large");
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new ProtocolError("request body is not valid JSON");
  }
}

function writeJson(response, status, value) {
  const body = JSON.stringify(value);
  response.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

export function createServer(service = new AdapterService()) {
  return http.createServer(async (request, response) => {
    try {
      if (request.method === "GET" && request.url === "/health") {
        writeJson(response, 200, { status: "ok", adapter: "openclaw", version: "0.1.0" });
        return;
      }
      if (request.method === "POST" && request.url === "/seed") {
        writeJson(response, 200, await service.seed(await readJson(request)));
        return;
      }
      if (request.method === "POST" && request.url === "/run") {
        writeJson(response, 200, await service.run(await readJson(request)));
        return;
      }
      writeJson(response, 404, { error: "not found" });
    } catch (error) {
      const status = error instanceof ProtocolError ? 400 : 500;
      writeJson(response, status, { error: `${error.name}: ${error.message}` });
    }
  });
}

if (process.argv[1] === new URL(import.meta.url).pathname) {
  const port = Number(process.env.PORT || 8080);
  createServer().listen(port, "0.0.0.0");
}

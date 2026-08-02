import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

function digest(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function fenced(value) {
  return String(value ?? "").replaceAll("\r\n", "\n");
}

export class OpenClawState {
  constructor(root) {
    this.root = root;
    this.locks = new Map();
  }

  userKey(userId) {
    return digest(userId);
  }

  agentId(userId) {
    return `bench-${this.userKey(userId).slice(0, 24)}`;
  }

  userRoot(userId) {
    return path.join(this.root, "users", this.userKey(userId));
  }

  workspace(userId) {
    return path.join(this.userRoot(userId), "workspace");
  }

  agentDir(userId) {
    return path.join(this.userRoot(userId), "agent");
  }

  sessionFile(userId, caseId) {
    return path.join(this.userRoot(userId), "eval", `${digest(caseId)}.jsonl`);
  }

  memoryFile(userId, pairId) {
    return path.join(this.workspace(userId), "memory", `pair-${digest(pairId)}.md`);
  }

  runtimeMemoryFile(userId, memoryId) {
    return path.join(this.workspace(userId), "memory", `runtime-${digest(memoryId)}.md`);
  }

  async withUserLock(userId, operation) {
    const previous = this.locks.get(userId) ?? Promise.resolve();
    let release;
    const current = new Promise((resolve) => { release = resolve; });
    const queued = previous.then(() => current);
    this.locks.set(userId, queued);
    await previous;
    try {
      return await operation();
    } finally {
      release();
      if (this.locks.get(userId) === queued) this.locks.delete(userId);
    }
  }

  async ensureUser(userId) {
    await fs.mkdir(path.join(this.workspace(userId), "memory"), { recursive: true });
    await fs.mkdir(this.agentDir(userId), { recursive: true });
    await fs.mkdir(path.dirname(this.sessionFile(userId, "bootstrap")), { recursive: true });
  }

  async seed(request) {
    return this.withUserLock(request.userId, async () => {
      await this.ensureUser(request.userId);
      const memoryDir = path.join(this.workspace(request.userId), "memory");
      if (request.wave === 0) {
        await fs.rm(memoryDir, { recursive: true, force: true });
        await fs.mkdir(memoryDir, { recursive: true });
      }

      for (const pair of request.pairs) {
        const markdown = [
          "# Imported conversation",
          "",
          `- Pair ID: ${pair.pairId}`,
          `- Session ID: ${pair.sessionId || "unspecified"}`,
          `- Timestamp: ${pair.timestamp || "unspecified"}`,
          "",
          "## User",
          "",
          fenced(pair.prompt),
          "",
          "## Assistant",
          "",
          fenced(pair.response),
          "",
        ].join("\n");
        await fs.writeFile(this.memoryFile(request.userId, pair.pairId), markdown, "utf8");
      }

      const metadata = [
        `# Imported DittoBench metadata wave ${request.wave}`,
        "",
        "The records below are verbatim seed metadata, stored as native Markdown for OpenClaw indexing.",
        "",
        "## Subjects",
        "",
        "```json",
        JSON.stringify(request.subjects, null, 2),
        "```",
        "",
        "## Links",
        "",
        "```json",
        JSON.stringify(request.links, null, 2),
        "```",
        "",
      ].join("\n");
      await fs.writeFile(path.join(memoryDir, `metadata-wave-${request.wave}.md`), metadata, "utf8");

      return {
        pairs: request.pairs.length,
        subjects: request.subjects.length,
        links: request.links.length,
      };
    });
  }

  async saveMemory(userId, content) {
    await this.ensureUser(userId);
    const memoryId = `openclaw-${crypto.randomUUID()}`;
    const markdown = `# Remembered fact\n\n- Memory ID: ${memoryId}\n\n${fenced(content)}\n`;
    await fs.writeFile(this.runtimeMemoryFile(userId, memoryId), markdown, "utf8");
    return memoryId;
  }

  async updateMemory(userId, pairId, content) {
    await this.ensureUser(userId);
    const candidates = [this.memoryFile(userId, pairId), this.runtimeMemoryFile(userId, pairId)];
    let target = candidates[0];
    for (const candidate of candidates) {
      try {
        await fs.access(candidate);
        target = candidate;
        break;
      } catch {}
    }
    const markdown = `# Updated memory\n\n- Memory ID: ${pairId}\n\n${fenced(content)}\n`;
    await fs.writeFile(target, markdown, "utf8");
    return pairId;
  }

  async deleteMemory(userId, pairId) {
    const candidates = [this.memoryFile(userId, pairId), this.runtimeMemoryFile(userId, pairId)];
    let deleted = false;
    for (const candidate of candidates) {
      try {
        await fs.unlink(candidate);
        deleted = true;
      } catch (error) {
        if (error?.code !== "ENOENT") throw error;
      }
    }
    return deleted;
  }
}

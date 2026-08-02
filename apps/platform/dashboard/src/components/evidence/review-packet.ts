// The one-click "review details" packet miners paste when asking for a
// review (monolith reviewPacketLine 6066–6068, reviewPacket 6069–6084):
// agent id, name/version, hotkey, status, artifact SHA, canonical URL.
import { canonicalEntityUrl } from "../../lib/entity-links";
import { agentVersionLabel } from "../../lib/format";

export interface ReviewPacketEntry {
  agent_id?: string | null;
  name?: string | null;
  agent_name?: string | null;
  version?: number | null;
  agent_version?: number | null;
  miner_hotkey?: string | null;
  status?: string | null;
  artifact_sha256?: string | null;
}

function reviewPacketLine(value: unknown): string {
  return String(value == null ? "" : value)
    .replace(/[\r\n\t]+/g, " ")
    .trim();
}

export function reviewPacket(entry: ReviewPacketEntry): string {
  const agentId = reviewPacketLine(entry.agent_id);
  const name = reviewPacketLine(entry.name || entry.agent_name || "Unnamed agent");
  const version = entry.version != null ? entry.version : entry.agent_version;
  const lines = [
    "Please review agent " + agentId,
    "Name: " + name + " (" + agentVersionLabel(version) + ")",
    "Miner hotkey: " + reviewPacketLine(entry.miner_hotkey),
  ];
  if (entry.status) lines.push("Status: " + reviewPacketLine(entry.status));
  if (entry.artifact_sha256) {
    lines.push("Artifact SHA-256: " + reviewPacketLine(entry.artifact_sha256));
  }
  lines.push("URL: " + canonicalEntityUrl("agent", agentId));
  return lines.join("\n");
}

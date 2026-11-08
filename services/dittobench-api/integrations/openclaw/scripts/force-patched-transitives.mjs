#!/usr/bin/env node
/**
 * openclaw@2026.7.33 publishes an npm-shrinkwrap.json. Parent package.json
 * overrides are recorded by `npm explain` but do not replace the nested
 * shrinkwrapped copies under node_modules/openclaw/node_modules, so Dependabot
 * HIGH floors must be forced onto disk after install.
 *
 * Adopting Dependabot #3398 (openclaw 2026.10.10) removes the shrinkwrap and
 * clears most floors, but also removes openclaw/extension-api and changes
 * session storage — that needs a separate adapter migration, not a silent
 * security bump here.
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const openclawDir = path.join(root, "node_modules", "openclaw");

const floors = {
  "@modelcontextprotocol/sdk": "1.32.0",
  "brace-expansion": "5.0.11",
  "body-parser": "2.4.0",
  undici: "8.12.2",
  zod: "4.6.6",
};

if (!fs.existsSync(openclawDir)) {
  console.log("force-patched-transitives: openclaw not installed; skipping");
  process.exit(0);
}

const specs = Object.entries(floors).map(([name, version]) => `${name}@${version}`);
const result = spawnSync(
  "npm",
  [
    "install",
    ...specs,
    "--prefix",
    openclawDir,
    "--no-save",
    "--no-package-lock",
    "--no-fund",
    "--no-audit",
    "--ignore-scripts",
  ],
  { cwd: root, encoding: "utf8" },
);

if (result.status !== 0) {
  process.stderr.write(result.stdout || "");
  process.stderr.write(result.stderr || "");
  process.exit(result.status ?? 1);
}

for (const [name, version] of Object.entries(floors)) {
  const pkgPath = path.join(openclawDir, "node_modules", ...name.split("/"), "package.json");
  if (!fs.existsSync(pkgPath)) {
    console.error(`force-patched-transitives: missing ${pkgPath}`);
    process.exit(1);
  }
  const actual = JSON.parse(fs.readFileSync(pkgPath, "utf8")).version;
  if (actual !== version) {
    console.error(
      `force-patched-transitives: ${name} expected ${version}, found ${actual}`,
    );
    process.exit(1);
  }
  console.log(`force-patched-transitives: ${name}@${actual}`);
}

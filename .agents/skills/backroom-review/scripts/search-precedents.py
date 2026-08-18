#!/usr/bin/env python3
"""Search local ATH board-review precedent files.

Live production holdings belong to Backroom ``search_ath_precedents``. This
CLI only reads the canned reporter next to this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PRECEDENTS_DIR = Path(__file__).resolve().parent.parent / "references" / "precedents"


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    rest = text[4:]
    end = rest.find("\n---\n")
    if end < 0:
        return {}, text
    raw, body = rest[:end], rest[end + 5 :]
    meta: dict[str, object] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [
                item.strip() for item in value[1:-1].split(",") if item.strip()
            ]
        elif value.lower() in {"true", "false"}:
            meta[key] = value.lower() == "true"
        else:
            meta[key] = value
    return meta, body


def load_precedents(directory: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.md")):
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        if not meta:
            continue
        tags = meta.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)]
        rows.append(
            {
                "id": str(meta.get("id") or path.stem),
                "agent_id": meta.get("agent_id"),
                "agent_name": meta.get("agent_name"),
                "agent_version": meta.get("agent_version"),
                "resolution": str(meta.get("resolution") or "").lower(),
                "holding": meta.get("holding"),
                "tags": [str(tag) for tag in tags],
                "path": str(path.relative_to(directory.parent.parent)),
                "body": body,
            }
        )
    return rows


def matches(
    row: dict[str, object],
    query: str | None,
    resolution: str | None,
    tag: str | None,
) -> bool:
    if resolution and row["resolution"] != resolution:
        return False
    if tag:
        tags = [item.lower() for item in row["tags"]]  # type: ignore[union-attr]
        if tag.lower() not in tags:
            return False
    if not query:
        return True
    needle = query.casefold()
    haystacks = [
        str(row.get("id") or ""),
        str(row.get("agent_id") or ""),
        str(row.get("agent_name") or ""),
        str(row.get("agent_version") or ""),
        str(row.get("holding") or ""),
        str(row.get("body") or ""),
        " ".join(row["tags"]),  # type: ignore[arg-type]
    ]
    return any(needle in item.casefold() for item in haystacks)


def render_text(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "No local precedents matched."
    lines: list[str] = []
    for row in rows:
        version = row.get("agent_version") or ""
        name = row.get("agent_name") or row["id"]
        heading = (
            f"{row['id']}  {str(row['resolution']).upper()}  {name} {version}".rstrip()
        )
        lines.append(heading)
        if row.get("agent_id"):
            lines.append(f"  agent_id: {row['agent_id']}")
        if row.get("holding"):
            lines.append(f"  {row['holding']}")
        if row.get("tags"):
            lines.append(f"  tags: {', '.join(row['tags'])}")  # type: ignore[arg-type]
        lines.append(f"  file: {row['path']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search canned ATH board-review precedents.",
    )
    parser.add_argument("query", nargs="?", help="Case-insensitive substring")
    parser.add_argument(
        "--resolution",
        choices=("clear", "reject", "leave"),
        help="Restrict to one holding",
    )
    parser.add_argument("--tag", help="Restrict to one frontmatter tag")
    parser.add_argument("--list", action="store_true", help="Print every precedent")
    parser.add_argument("--json", action="store_true", help="JSON instead of text")
    parser.add_argument(
        "--dir",
        type=Path,
        default=PRECEDENTS_DIR,
        help="Override precedents directory",
    )
    args = parser.parse_args(argv)
    if not args.dir.is_dir():
        print(f"precedents directory not found: {args.dir}", file=sys.stderr)
        return 2
    rows = [
        row
        for row in load_precedents(args.dir)
        if matches(row, None if args.list else args.query, args.resolution, args.tag)
    ]
    if args.json:
        payload = [{k: v for k, v in row.items() if k != "body"} for row in rows]
        print(json.dumps({"count": len(payload), "items": payload}, indent=2))
        return 0
    sys.stdout.write(render_text(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

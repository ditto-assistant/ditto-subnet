#!/usr/bin/env python3
"""Read-only independent snapshot matching frozen backend 1b575560.

Run only after all preparation/ID-rewrite writers exit. A snapshot detects
changes; it does not prevent later writers. No credentials or raw rows output.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SOURCE_SHA = "1b5755609b3ea95660fdba289e6a747adb8c5dae"
VERSION = "postgres-jsonb-row-multiset-sha256-v1"
TABLES = ("users", "memory_pairs", "subjects", "subject_memory_pair_links",
          "subject_artifact_links", "subject_edges", "subject_graph_state",
          "subject_clusters", "subject_cluster_members", "subject_cluster_edges")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON object key")
        result[key] = value
    return result


def read_json(path):
    return json.loads(path.read_text(), object_pairs_hook=unique_object)


def manifest_users(manifest, expected_count=500):
    cases = manifest.get("cases")
    require(isinstance(cases, list) and len(cases) == expected_count, "manifest case count differs")
    users = []
    for case in cases:
        require(isinstance(case, dict), "invalid manifest case")
        qid, user = case.get("question_id"), case.get("fixture_user")
        require(isinstance(qid, str) and re.fullmatch(r"[A-Za-z0-9_-]+", qid), "invalid question ID")
        require(user == "lme_s_" + qid, "fixture user does not match question ID")
        users.append(user)
    require(len(set(users)) == expected_count, "duplicate fixture user/question")
    return sorted(users)


def snapshot_sql(users):
    require(users and users == sorted(set(users)), "users must be nonempty sorted unique")
    require(all(re.fullmatch(r"lme_s_[A-Za-z0-9_-]+", u) for u in users), "invalid fixture scope")
    # Validated ASCII identifiers contain no SQL or psql metacharacters.
    scope = "ARRAY[" + ",".join("'" + u + "'" for u in users) + "]::text[]"
    statements = ["BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;",
                  "SET LOCAL TIME ZONE 'UTC';"]
    for table in TABLES:
        column = "uid" if table == "users" else "user_id"
        statements.append(f"""WITH rows AS MATERIALIZED (
 SELECT encode(digest(to_jsonb(t)::text, 'sha256'), 'hex') AS hash
 FROM {table} t WHERE {column} = ANY({scope}))
 SELECT json_build_object('table', '{table}', 'sha256',
 encode(digest(coalesce(string_agg(hash, '' ORDER BY hash), ''), 'sha256'), 'hex'),
 'rows', count(*)) FROM rows;""")
    statements.append("COMMIT;")
    return "\n".join(statements) + "\n"


def build_snapshot(users, entries):
    require(len(entries) == len(TABLES), "snapshot table count differs")
    tables = []
    for name, entry in zip(TABLES, entries):
        require(isinstance(entry, dict) and entry.get("table") == name, "snapshot table order differs")
        require(isinstance(entry.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), "invalid table hash")
        require(type(entry.get("rows")) is int and entry["rows"] >= 0, "invalid row count")
        tables.append({"table": name, "sha256": entry["sha256"], "rows": entry["rows"]})
    # encoding/json marshals the Go struct in this exact field order, including
    # its empty SHA256 field on the first marshal. Do not sort JSON keys.
    result = {"version": VERSION, "sha256": "", "users": users, "tables": tables}
    encoded = json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode()
    result["sha256"] = hashlib.sha256(encoded).hexdigest()
    return result


def capture(container, database, users):
    require(re.fullmatch(r"ditto-postgres-lme-[A-Za-z0-9_-]+", container), "only a task-private LME container is allowed")
    require(re.fullmatch(r"ditto_lme[A-Za-z0-9_]*", database), "only an LME database name is allowed")
    command = ["docker", "exec", "-i", container, "psql", "-X", "-q", "-A", "-t",
               "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database]
    completed = subprocess.run(command, input=snapshot_sql(users), capture_output=True, text=True, check=True)
    entries = [json.loads(line, object_pairs_hook=unique_object)
               for line in completed.stdout.splitlines() if line.strip()]
    return build_snapshot(users, entries)


def compare_snapshot(actual, evidence):
    if "meta" in evidence:
        evidence = json.loads(evidence["meta"]["lme_prepared_snapshot"], object_pairs_hook=unique_object)
    require(actual == evidence, "prepared snapshot differs from comparison evidence")


def write_new(path, snapshot):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(snapshot, stream, indent=2)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--container", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", type=Path, help="new 0600 JSON file; otherwise stdout")
    parser.add_argument("--compare", type=Path, help="prior snapshot or completed backend run JSON")
    args = parser.parse_args()
    require(args.output is None or not args.output.exists(), "output already exists")
    users = manifest_users(read_json(args.manifest))
    snapshot = capture(args.container, args.database, users)
    if args.compare:
        compare_snapshot(snapshot, read_json(args.compare))
    if args.output:
        write_new(args.output, snapshot)
    else:
        print(json.dumps(snapshot, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        print(f"snapshot capture refused: {exc}", file=sys.stderr)
        sys.exit(1)

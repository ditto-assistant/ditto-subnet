#!/usr/bin/env python3
"""Online encrypted PostgreSQL backups. Never print provider errors or secrets."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

BUCKET = "ditto-platform-pg-backups"
ENDPOINT = "https://s3.hippius.com"
DATABASE = "ditto_platform_prod"
TABLES = ("agents", "screening_attempts", "scores")
KEY_RE = re.compile(
    r"^(daily|monthly)/(\d{4})/(\d{2})/(\d{2})/"
    r"(ditto_platform_prod|globals|manifest)-(\d{8}T\d{6}Z)\.(dump\.age|sql\.age|json)$"
)


def utcnow():
    return datetime.now(timezone.utc)


def sha256(file):
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_key(key):
    match = KEY_RE.fullmatch(key)
    if not match:
        return None
    prefix, year, month, day, kind, stamp, extension = match.groups()
    expected = {"ditto_platform_prod": "dump.age", "globals": "sql.age", "manifest": "json"}
    if expected[kind] != extension:
        return None
    try:
        instant = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if instant.strftime("%Y/%m/%d") != f"{year}/{month}/{day}":
        return None
    if prefix == "monthly" and instant.day != 1:
        return None
    return prefix, instant, kind


def retention_keys(keys):
    """Keep 30 newest daily runs and 12 newest first-of-month copies.

    Only complete manifest-committed groups may be reclaimed. Unknown keys and
    incomplete uploads remain untouched for explicit operator investigation.
    """
    groups = {}
    for key in keys:
        parsed = parse_key(key)
        if parsed:
            prefix, instant, kind = parsed
            groups.setdefault((prefix, instant), {})[kind] = key
    victims = []
    for prefix, keep in (("daily", 30), ("monthly", 12)):
        complete = sorted(
            (instant for (group_prefix, instant), objects in groups.items()
             if group_prefix == prefix and set(objects) == {"ditto_platform_prod", "globals", "manifest"}),
            reverse=True,
        )
        if prefix == "monthly":
            selected = {}
            for instant in complete:
                selected.setdefault(instant.strftime("%Y-%m"), instant)
            retained = set(list(selected.values())[:keep])
        else:
            retained = set(complete[:keep])
        for instant in complete:
            if instant in retained:
                continue
            objects = groups[(prefix, instant)]
            # Remove the commit marker first so no drill selects a partial set.
            victims.extend(objects[kind] for kind in ("manifest", "globals", "ditto_platform_prod"))
    return victims


def guard_space(database_bytes, staging):
    # Same reserve as preview/cloud/export-snapshot.sh, tested with a stub df.
    output = subprocess.check_output(["df", "-Pk", str(staging)], text=True)
    fields = output.splitlines()[1].split()
    total, available = int(fields[1]) * 1024, int(fields[3]) * 1024
    reserve = max(10 * 1024**3, total // 5)
    if database_bytes < 0 or available < database_bytes + reserve:
        raise RuntimeError("insufficient backup staging space")


def protected_file(directory, name):
    file = directory / name
    stat = file.lstat()
    if not file.is_file() or file.is_symlink() or stat.st_mode & 0o777 != 0o600:
        raise RuntimeError("backup credential file is not protected")
    if stat.st_uid != os.geteuid() or stat.st_nlink != 1:
        raise RuntimeError("backup credential ownership is invalid")
    return file


class S3:
    """Presign every operation; never emit the URL, response body, or credentials."""
    def __init__(self, directory):
        import boto3
        import requests
        from botocore.config import Config

        self.client = boto3.client(
            "s3", endpoint_url=ENDPOINT, region_name="decentralized",
            aws_access_key_id=protected_file(directory, "access-key-id").read_text().strip(),
            aws_secret_access_key=protected_file(directory, "secret-access-key").read_text().strip(),
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )
        self.http = requests.Session()
        self.http.trust_env = False

    def request(self, op, method, params, **kwargs):
        url = self.client.generate_presigned_url(
            op, Params={"Bucket": BUCKET, **params}, ExpiresIn=300,
        )
        response = self.http.request(method, url, timeout=(30, 300), allow_redirects=False, **kwargs)
        if response.status_code >= 300:
            response.close()
            raise RuntimeError("Hippius operation failed")
        return response

    def list(self, prefix):
        objects, seen, token = [], set(), None
        for _ in range(100):
            params = {"Prefix": prefix, "MaxKeys": 1000}
            if token:
                params["ContinuationToken"] = token
            with self.request("list_objects_v2", "GET", params) as response:
                root = ET.fromstring(response.content)
            fields = {child.tag.rsplit("}", 1)[-1]: child.text for child in root}
            for child in root:
                if child.tag.rsplit("}", 1)[-1] == "Contents":
                    item = {field.tag.rsplit("}", 1)[-1]: field.text for field in child}
                    objects.append({"key": item["Key"], "size": int(item["Size"]),
                                    "last_modified": item["LastModified"]})
            if fields.get("IsTruncated") != "true":
                return objects
            token = fields.get("NextContinuationToken")
            if not token or token in seen:
                raise RuntimeError("incomplete Hippius inventory")
            seen.add(token)
        raise RuntimeError("Hippius inventory exceeded bound")

    def upload(self, key, file):
        size = file.stat().st_size
        if size < 16:
            raise RuntimeError("empty backup object")
        if key.endswith(".age"):
            with file.open("rb") as stream:
                if stream.read(22) != b"age-encryption.org/v1\n":
                    raise RuntimeError("refusing non-age backup")
        upload_id = None
        try:
            with self.request("create_multipart_upload", "POST", {"Key": key}) as response:
                root = ET.fromstring(response.content)
            upload_id = next(child.text for child in root if child.tag.rsplit("}", 1)[-1] == "UploadId")
            parts = []
            with file.open("rb") as stream:
                for number in range(1, 10001):
                    chunk = stream.read(64 * 1024**2)
                    if not chunk:
                        break
                    with self.request("upload_part", "PUT", {
                        "Key": key, "UploadId": upload_id, "PartNumber": number,
                    }, data=chunk) as response:
                        parts.append((number, response.headers["ETag"]))
                else:
                    raise RuntimeError("backup exceeds multipart limit")
            document = ET.Element("CompleteMultipartUpload")
            for number, etag in parts:
                part = ET.SubElement(document, "Part")
                ET.SubElement(part, "PartNumber").text = str(number)
                ET.SubElement(part, "ETag").text = etag
            with self.request("complete_multipart_upload", "POST", {"Key": key, "UploadId": upload_id},
                              data=ET.tostring(document), headers={"Content-Type": "application/xml"}) as response:
                if ET.fromstring(response.content).tag.rsplit("}", 1)[-1] == "Error":
                    raise RuntimeError("multipart completion failed")
            upload_id = None
            with self.request("head_object", "HEAD", {"Key": key}) as response:
                if int(response.headers["Content-Length"]) != size:
                    raise RuntimeError("remote backup size mismatch")
        finally:
            if upload_id:
                with self.request("abort_multipart_upload", "DELETE", {"Key": key, "UploadId": upload_id}):
                    pass

    def download(self, key, destination, max_bytes=None):
        with self.request("get_object", "GET", {"Key": key}, stream=True) as response:
            with destination.open("wb") as output:
                written = 0
                for chunk in response.iter_content(1024 * 1024):
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise RuntimeError("backup metadata exceeds bound")
                    output.write(chunk)

    def delete(self, key):
        if not parse_key(key):
            raise RuntimeError("unsafe retention key")
        with self.request("delete_object", "DELETE", {"Key": key}):
            pass


def encrypted_dump(command, recipient_file, destination):
    # The public recipient is read from a protected file by age, never argv.
    with destination.open("wb") as output:
        dump = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            encrypt = subprocess.Popen(["age", "-R", str(recipient_file)],
                                       stdin=dump.stdout, stdout=output, stderr=subprocess.DEVNULL)
            dump.stdout.close()
            encrypt_result = encrypt.wait()
            dump_result = dump.wait()
            if encrypt_result or dump_result:
                raise RuntimeError("encrypted PostgreSQL export failed")
        finally:
            if dump.poll() is None:
                dump.terminate()
                dump.wait()


def backup(directory=Path("/etc/ditto-pg-backup"), staging_base=Path("/var/tmp"),
           state=Path("/var/lib/ditto-pg-backup")):
    os.umask(0o077)
    recipient = protected_file(directory, "age-recipient")
    # tempfile is explicitly on the disk, never the VM's RAM-backed /tmp.
    started = utcnow()
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    transaction = subprocess.Popen(
        ["sudo", "-u", "postgres", "psql", "-XAtq", "-v", "ON_ERROR_STOP=1", "-d", DATABASE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        query = (
            "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; "
            "SET LOCAL statement_timeout = '60s'; "
            "SET LOCAL idle_in_transaction_session_timeout = '90min'; "
            "SELECT json_build_object("
            "'snapshot', pg_export_snapshot(), 'server_version', version(), "
            "'server_version_num', current_setting('server_version_num')::integer, "
            "'database_bytes', pg_database_size(current_database()), "
            "'alembic_version', (SELECT version_num FROM alembic_version), "
            "'row_counts', json_build_object("
            + ", ".join(f"'{table}', (SELECT count(*) FROM {table})" for table in TABLES)
            + "));\n"
        )
        transaction.stdin.write(query)
        transaction.stdin.flush()
        metadata = json.loads(transaction.stdout.readline())
        guard_space(metadata["database_bytes"], staging_base)
        s3 = S3(directory)
        with tempfile.TemporaryDirectory(prefix="ditto-pg-backup-", dir=staging_base) as scratch:
            staging = Path(scratch)
            dump = staging / f"ditto_platform_prod-{stamp}.dump.age"
            globals_file = staging / f"globals-{stamp}.sql.age"
            encrypted_dump(
                ["sudo", "-u", "postgres", "pg_dump", "-Fc", "--no-owner", "--no-privileges",
                 f"--snapshot={metadata.pop('snapshot')}", DATABASE], recipient, dump,
            )
            transaction.stdin.write("COMMIT;\n")
            transaction.stdin.close()
            if transaction.wait() != 0:
                raise RuntimeError("backup snapshot transaction failed")
            encrypted_dump(["sudo", "-u", "postgres", "pg_dumpall", "--globals-only"], recipient, globals_file)
            manifest = {
                "format_version": 1, "database": DATABASE, **metadata,
                "pg_dump_version": subprocess.check_output(
                    ["sudo", "-u", "postgres", "pg_dump", "--version"], text=True).strip(),
                "started_at": started.isoformat(), "completed_at": utcnow().isoformat(),
                "objects": [{"name": file.name, "sha256": sha256(file), "size": file.stat().st_size}
                            for file in (dump, globals_file)],
            }
            manifest_file = staging / f"manifest-{stamp}.json"
            manifest_file.write_text(json.dumps(manifest, sort_keys=True) + "\n")
            prefixes = ["daily/"] + (["monthly/"] if started.day == 1 else [])
            for prefix in prefixes:
                base = prefix + started.strftime("%Y/%m/%d/")
                # Manifest is the commit marker, always uploaded/verified last.
                for file in (dump, globals_file, manifest_file):
                    s3.upload(base + file.name, file)
            inventory = s3.list("daily/") + s3.list("monthly/")
            for key in retention_keys([item["key"] for item in inventory]):
                s3.delete(key)
            state.mkdir(mode=0o700, exist_ok=True)
            pending = state / "last_success.pending"
            pending.write_text(f"{int(utcnow().timestamp())} {sha256(manifest_file)}\n")
            pending.replace(state / "last_success")
    finally:
        if transaction.poll() is None:
            transaction.terminate()
            transaction.wait()


if __name__ == "__main__":
    def interrupted(_signum, _frame):
        raise RuntimeError("backup interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        backup()
    except Exception:
        # Provider exceptions can carry presigned URLs or globals password hashes.
        print("ditto-pg-backup: FAILED", file=__import__("sys").stderr)
        raise SystemExit(1)

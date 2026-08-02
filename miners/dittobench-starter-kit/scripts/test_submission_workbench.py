from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("submission-workbench.py")
SPEC = importlib.util.spec_from_file_location("submission_workbench", SCRIPT)
assert SPEC and SPEC.loader
WORKBENCH = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WORKBENCH
SPEC.loader.exec_module(WORKBENCH)


class FakeChild:
    pid = 12345

    def poll(self) -> None:
        return None


class SubmissionWorkbenchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = WORKBENCH.WorkbenchState(
            root=Path(self.temporary.name),
            token="test-token",
            port=0,
            trust_embedded_key=True,
        )
        self.server = WORKBENCH.ThreadingHTTPServer(
            ("127.0.0.1", 0), WORKBENCH.handler_for(self.state)
        )
        self.state.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.state.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        *,
        data: bytes | None = None,
        method: str = "GET",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        request_headers = {"x-ditto-lab-token": self.state.token}
        request_headers.update(headers or {})
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())

    def test_front_page_is_drop_to_chat_workbench(self) -> None:
        with urllib.request.urlopen(self.base + "/") as response:
            page = response.read().decode()
        self.assertIn("Drop a harness", page)
        self.assertIn("Agent submission", page)
        self.assertIn("Ditto Memory Passport", page)
        self.assertIn("Optional — start with blank memory", page)
        self.assertIn("Build, load & chat", page)
        self.assertNotIn("OPENROUTER_API_KEY=", page)

    def test_raw_uploads_are_private_and_make_session_launchable(self) -> None:
        _, uploaded = self.request(
            "/api/upload/submission",
            data=b"reviewed artifact",
            method="POST",
            headers={
                "content-type": "application/octet-stream",
                "x-file-name": "reviewed-agent.tar.gz",
            },
        )
        self.assertEqual(uploaded["submission"], "reviewed-agent.tar.gz")
        self.assertEqual(
            len(str(uploaded["submission_sha256"])),
            64,
        )
        self.assertTrue(uploaded["can_launch"])

        _, uploaded = self.request(
            "/api/upload/passport",
            data=b"signed export",
            method="POST",
            headers={
                "content-type": "application/octet-stream",
                "x-file-name": "ditto-passport.zip",
            },
        )
        self.assertTrue(uploaded["can_launch"])
        self.assertNotIn("counts", uploaded)
        self.assertNotIn("user_id", uploaded)
        self.assertEqual(self.state.submission_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.passport_path.stat().st_mode & 0o777, 0o600)

    def test_launch_requires_review_acknowledgement(self) -> None:
        self.state.session_dir.mkdir(parents=True)
        self.state.submission_path = self.state.session_dir / "submission.tar"
        self.state.submission_path.write_bytes(b"tar")
        request = urllib.request.Request(
            self.base + "/api/launch",
            data=b'{"provider":"ollama","acknowledged":false}',
            headers={
                "content-type": "application/json",
                "x-ditto-lab-token": self.state.token,
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.state.phase, "idle")

    def test_launch_builds_starts_seeds_then_becomes_ready(self) -> None:
        self.state.session_dir.mkdir(parents=True)
        submission = self.state.session_dir / "submission.tar"
        with tarfile.open(submission, "w") as archive:
            content = b"[package]\nname='reviewed'\nversion='0.1.0'\n"
            info = tarfile.TarInfo("Cargo.toml")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        passport_path = self.state.session_dir / "passport.zip"
        passport_path.write_bytes(b"fixture")
        self.state.submission_path = submission
        self.state.passport_path = passport_path
        passport = mock.Mock(user_id="isolated-user")
        built = mock.Mock(returncode=0)
        child = FakeChild()
        with (
            mock.patch.object(
                WORKBENCH.LAB,
                "load_verified_passport",
                return_value=(passport, "fixture"),
            ),
            mock.patch.object(WORKBENCH.subprocess, "run", return_value=built) as run,
            mock.patch.object(
                WORKBENCH.subprocess, "Popen", return_value=child
            ) as popen,
            mock.patch.object(WORKBENCH.LAB, "wait_for_agent") as wait,
            mock.patch.object(WORKBENCH.LAB, "seed_agent") as seed,
        ):
            WORKBENCH.launch_submission(
                self.state,
                provider="ollama",
                full_export=False,
            )
        self.assertEqual(self.state.phase, "ready")
        self.assertEqual(self.state.memory_scope, "quick_100")
        self.assertEqual(
            run.call_args.args[0], ["cargo", "build", "--release", "--locked"]
        )
        self.assertEqual(
            popen.call_args.args[0][:4], ["cargo", "run", "--release", "--locked"]
        )
        wait.assert_called_once()
        seed.assert_called_once()

    def test_launch_without_passport_seeds_blank_isolated_user(self) -> None:
        self.state.session_dir.mkdir(parents=True)
        submission = self.state.session_dir / "submission.tar"
        with tarfile.open(submission, "w") as archive:
            content = b"[package]\nname='reviewed'\nversion='0.1.0'\n"
            info = tarfile.TarInfo("Cargo.toml")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        self.state.submission_path = submission
        built = mock.Mock(returncode=0)
        child = FakeChild()
        with (
            mock.patch.object(WORKBENCH.LAB, "load_verified_passport") as load_passport,
            mock.patch.object(WORKBENCH.subprocess, "run", return_value=built),
            mock.patch.object(WORKBENCH.subprocess, "Popen", return_value=child),
            mock.patch.object(WORKBENCH.LAB, "wait_for_agent"),
            mock.patch.object(WORKBENCH.LAB, "seed_agent") as seed,
        ):
            WORKBENCH.launch_submission(
                self.state,
                provider="ollama",
                full_export=False,
            )
        load_passport.assert_not_called()
        self.assertEqual(self.state.phase, "ready")
        self.assertEqual(self.state.memory_scope, "blank")
        seeded = seed.call_args.args[1]
        self.assertTrue(seeded.user_id.startswith("workbench-"))
        self.assertEqual(seeded.seed_payload["pairs"], [])
        self.assertEqual(seeded.seed_payload["subjects"], [])
        self.assertEqual(seeded.seed_payload["links"], [])

    def test_provider_environment_passes_only_selected_secret(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "openrouter-secret",
                "UNRELATED_SECRET": "must-not-pass",
            },
            clear=False,
        ):
            env = WORKBENCH._provider_environment(
                "openrouter", Path(self.temporary.name) / "workbench.db"
            )
        self.assertEqual(env["OPENROUTER_API_KEY"], "openrouter-secret")
        self.assertNotIn("UNRELATED_SECRET", env)
        self.assertEqual(env["DITTOBENCH_PROVIDER"], "openrouter")

    def test_ollama_provider_selects_local_models(self) -> None:
        env = WORKBENCH._provider_environment(
            "ollama", Path(self.temporary.name) / "workbench.db"
        )
        self.assertEqual(env["DITTOBENCH_PROVIDER"], "ollama")
        self.assertEqual(env["DITTOBENCH_MODEL"], "gpt-oss:20b")
        self.assertNotIn("OPENROUTER_API_KEY", env)

    def test_reset_clears_review_artifacts(self) -> None:
        old_session = self.state.session_dir
        old_session.mkdir(parents=True)
        (old_session / "private").write_text("memory", encoding="utf-8")
        self.state.phase = "error"
        WORKBENCH.reset_session(self.state)
        self.assertFalse(old_session.exists())
        self.assertEqual(self.state.phase, "idle")
        self.assertNotEqual(self.state.session_dir, old_session)


if __name__ == "__main__":
    unittest.main()

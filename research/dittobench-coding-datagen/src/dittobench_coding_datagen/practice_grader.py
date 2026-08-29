"""Fresh deterministic grader for frozen public-practice submissions."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dittobench_coding_datagen.canonical import (
    canonical_json_bytes,
    safe_relative_path,
    sha256_hex,
)
from dittobench_coding_datagen.compiler import materialize
from dittobench_coding_datagen.model import CODING_CONTRACT_VERSION, CorpusError
from dittobench_coding_datagen.practice_case import (
    load_practice_agent_case,
    load_practice_grader_case,
)
from dittobench_coding_datagen.practice_runtime import (
    FrozenPracticeSubmission,
    _changed_paths,
    _snapshot,
    _tree_sha256,
    _unified_diff,
    run_practice_command,
)


@dataclass(frozen=True)
class PracticeTaskEvidence:
    """Binary, public-practice-only repair evidence."""

    coding_contract_version: int
    weight_eligible: bool
    task_id: str
    base_tree_sha256: str
    final_tree_sha256: str
    patch_sha256: str
    changed_path_root: str
    authoring_event_root: str
    build_returncode: int
    visible_tests_returncode: int
    grader_tests_returncode: int
    protected_paths_intact: bool
    harness_completed: bool
    terminal_domain: Literal["resolved", "repair_failure", "harness_failure"]
    repair_score_micros: int

    def as_json(self) -> dict[str, Any]:
        return {
            "authoring_event_root": self.authoring_event_root,
            "base_tree_sha256": self.base_tree_sha256,
            "build_returncode": self.build_returncode,
            "changed_path_root": self.changed_path_root,
            "coding_contract_version": self.coding_contract_version,
            "final_tree_sha256": self.final_tree_sha256,
            "grader_tests_returncode": self.grader_tests_returncode,
            "harness_completed": self.harness_completed,
            "patch_sha256": self.patch_sha256,
            "protected_paths_intact": self.protected_paths_intact,
            "repair_score_micros": self.repair_score_micros,
            "task_id": self.task_id,
            "terminal_domain": self.terminal_domain,
            "visible_tests_returncode": self.visible_tests_returncode,
            "weight_eligible": self.weight_eligible,
        }


def _atomic_write(path: Path, body: bytes, mode: int) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _apply_submission(
    workspace: Path,
    submission: FrozenPracticeSubmission,
    editable_paths: tuple[str, ...],
) -> None:
    if submission.changed_paths != tuple(sorted(submission.changed_paths)):
        raise CorpusError("frozen changed_paths must be sorted")
    if len(submission.changed_paths) != len(set(submission.changed_paths)):
        raise CorpusError("frozen changed_paths must be unique")
    changes_by_path = {change.path: change for change in submission.changes}
    if len(changes_by_path) != len(submission.changes):
        raise CorpusError("frozen changes contain duplicate paths")
    if tuple(changes_by_path) != submission.changed_paths:
        raise CorpusError("frozen changes disagree with changed_paths")
    for path in submission.changed_paths:
        relative = safe_relative_path(path)
        if relative not in editable_paths:
            raise CorpusError(f"frozen change targets protected path: {relative}")
        target = workspace / relative
        if not target.is_file() or target.is_symlink():
            raise CorpusError(f"frozen change target is not a regular file: {relative}")
        change = changes_by_path[path]
        before = target.read_bytes()
        if sha256_hex(before) != change.before_sha256:
            raise CorpusError(f"frozen before digest mismatch: {relative}")
        after = change.after_content.encode("utf-8")
        if sha256_hex(after) != change.after_sha256:
            raise CorpusError(f"frozen after digest mismatch: {relative}")
        mode = stat.S_IMODE(target.stat().st_mode)
        _atomic_write(target, after, mode)


def _inject_grader(grader_root: Path, workspace: Path) -> None:
    for path in sorted(grader_root.rglob("*")):
        relative = safe_relative_path(path.relative_to(grader_root).as_posix())
        if path.is_symlink():
            raise CorpusError(f"practice grader contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise CorpusError(f"practice grader contains a special file: {relative}")
        destination = workspace / relative
        if destination.exists():
            raise CorpusError(f"practice grader collides with visible file: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination, follow_symlinks=False)


def grade_frozen_practice_submission(
    pack: Path, submission: FrozenPracticeSubmission
) -> PracticeTaskEvidence:
    """Apply a frozen submission to a pristine base and grade it."""

    identities = {
        "authoring event root": submission.authoring_event_root,
        "base tree": submission.base_tree_sha256,
        "changed-path root": submission.changed_path_root,
        "final tree": submission.final_tree_sha256,
        "patch": submission.patch_sha256,
    }
    for label, identity in identities.items():
        if len(identity) != 64 or any(
            byte not in "0123456789abcdef" for byte in identity
        ):
            raise CorpusError(f"frozen {label} identity is not lowercase SHA-256")

    agent_case = load_practice_agent_case(pack, submission.task_id)
    grader_case = load_practice_grader_case(pack, submission.task_id)
    with tempfile.TemporaryDirectory(prefix="dittobench-practice-grader-") as raw:
        workspace = Path(raw) / "workspace"
        materialize(pack, submission.task_id, workspace)
        pristine = _snapshot(workspace)
        if _tree_sha256(pristine) != submission.base_tree_sha256:
            raise CorpusError("frozen submission base tree does not match the capsule")
        _apply_submission(
            workspace, submission, agent_case.runtime_policy.editable_paths
        )
        candidate = _snapshot(workspace)
        changed = _changed_paths(pristine, candidate)
        patch = _unified_diff(pristine, candidate, changed)
        if changed != submission.changed_paths:
            raise CorpusError("reapplied changed paths do not match frozen submission")
        if _tree_sha256(candidate) != submission.final_tree_sha256:
            raise CorpusError("reapplied final tree does not match frozen submission")
        if patch != submission.patch:
            raise CorpusError("reapplied patch bytes do not match frozen submission")
        if sha256_hex(patch.encode("utf-8")) != submission.patch_sha256:
            raise CorpusError("reapplied patch digest does not match frozen submission")
        if (
            sha256_hex(canonical_json_bytes(list(changed)))
            != submission.changed_path_root
        ):
            raise CorpusError("reapplied changed-path root does not match submission")

        _inject_grader(grader_case.grader_root, workspace)
        protected_before = _snapshot(workspace)
        build = run_practice_command(workspace, "python-compile")
        visible_returncode = 126
        grader_returncode = 126
        if build.returncode == 0:
            visible_returncode = run_practice_command(
                workspace, "visible-unit"
            ).returncode
            grader_returncode = run_practice_command(
                workspace, "grader-unit"
            ).returncode
        protected_intact = protected_before == _snapshot(workspace)
        resolved = (
            build.returncode == 0
            and visible_returncode == 0
            and grader_returncode == 0
            and protected_intact
        )
        return PracticeTaskEvidence(
            coding_contract_version=CODING_CONTRACT_VERSION,
            weight_eligible=False,
            task_id=submission.task_id,
            base_tree_sha256=submission.base_tree_sha256,
            final_tree_sha256=submission.final_tree_sha256,
            patch_sha256=submission.patch_sha256,
            changed_path_root=submission.changed_path_root,
            authoring_event_root=submission.authoring_event_root,
            build_returncode=build.returncode,
            visible_tests_returncode=visible_returncode,
            grader_tests_returncode=grader_returncode,
            protected_paths_intact=protected_intact,
            harness_completed=True,
            terminal_domain="resolved" if resolved else "repair_failure",
            repair_score_micros=1_000_000 if resolved else 0,
        )

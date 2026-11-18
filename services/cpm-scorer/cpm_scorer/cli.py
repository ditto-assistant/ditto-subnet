"""`cpm-scorer score` (offline, practice) and `cpm-scorer serve` (Platform)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from cpm_scorer import metrics
from cpm_scorer.bases import BASES, Base, get_base
from cpm_scorer.generate import build_generate, default_cache_dir, find_datagen_dir
from cpm_scorer.screening import ScreeningError, scan_repo_files

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
EXIT_NONDETERMINISTIC = 3
EXIT_SCREENING = 4


def parse_seeds(text: str) -> list[int]:
    seeds = [int(s) for s in text.split(",") if s.strip()]
    if not seeds:
        raise argparse.ArgumentTypeError("at least one seed")
    return seeds


def fetch_hf(repo: str, revision: str, cache_dir: str | None = None) -> Path:
    """Download json + safetensors at a pinned revision after checking the
    repo's full file list for code and pickle files."""
    from huggingface_hub import HfApi, snapshot_download

    files = HfApi().list_repo_files(repo, revision=revision)
    scan_repo_files(files)
    return Path(
        snapshot_download(
            repo,
            revision=revision,
            allow_patterns=["*.json", "*.safetensors"],
            cache_dir=cache_dir,
        )
    )


def resolve_source(spec: str, base: Base, cache_dir: str | None = None):
    """`--model`: `base`, a local directory, or `HF_REPO@REVISION`."""
    from cpm_scorer.score import ModelSource, screen_dir

    if spec == "base":
        return ModelSource(base, "base", None, f"{base.repo}@{base.revision}")
    p = Path(spec).expanduser()
    if p.is_dir():
        kind, _ = screen_dir(p, base.base_id)
        return ModelSource(base, kind, p, str(p))
    if "@" not in spec:
        raise SystemExit(f"--model {spec!r}: not a directory and not REPO@REVISION")
    repo, rev = spec.rsplit("@", 1)
    if repo == base.repo and rev == base.revision:
        return ModelSource(base, "base", None, spec)
    matched = next((b for b in BASES.values() if repo == b.repo), None)
    if matched is not None:
        if matched.base_id != base.base_id:
            raise SystemExit(
                f"{repo} is a base; select it with --base {matched.base_id} "
                "and --model base"
            )
        raise SystemExit(f"{repo} is a base; use revision {base.revision} or 'base'")
    if not _FULL_SHA.match(rev):
        print(
            f"warning: {rev!r} is not a full commit SHA; submissions require one",
            file=sys.stderr,
        )
    root = fetch_hf(repo, rev, cache_dir)
    kind, _ = screen_dir(root, base.base_id)
    return ModelSource(base, kind, root, spec)


def _generate_bin(args: argparse.Namespace, cache: Path) -> Path:
    if args.generate_bin:
        return Path(args.generate_bin)
    return build_generate(find_datagen_dir(), cache)


def cmd_score(args: argparse.Namespace) -> int:
    from cpm_scorer import score

    base = get_base(args.base)
    cache = Path(args.cache_dir) if args.cache_dir else default_cache_dir()
    try:
        source = resolve_source(args.model, base, args.hf_cache_dir)
    except ScreeningError as e:
        print(json.dumps({"screening": e.reason, "detail": e.detail}))
        return EXIT_SCREENING
    gen = _generate_bin(args, cache)
    try:
        loaded = score.load_scorer(source, args.device, args.hf_cache_dir)
    except ScreeningError as e:
        print(json.dumps({"screening": e.reason, "detail": e.detail}))
        return EXIT_SCREENING
    result = score.run(
        loaded,
        args.seeds,
        args.bench_version,
        gen,
        cache,
        limit_cases=args.limit_cases,
        budget=args.budget_tokens,
        tool_k=args.tool_k,
        rerun=not args.skip_determinism,
    )
    json.dump(result, sys.stdout, indent=2)
    print()
    det = result["determinism"]
    return EXIT_NONDETERMINISTIC if det and not det["identical"] else 0


def make_score_fn(args: argparse.Namespace):
    from cpm_scorer import score
    from cpm_scorer.serve import Work, WorkFault

    cache = Path(args.cache_dir) if args.cache_dir else default_cache_dir()
    gen = _generate_bin(args, cache)

    def score_fn(work: Work) -> dict:
        sub = work.submission
        try:
            base = get_base(sub["base_id"])
        except ValueError as e:
            raise WorkFault("screening:base_not_allowed", str(e)) from None
        if sub.get("kind") == "base":
            source = score.ModelSource(base, "base", None, "reference")
        else:
            repo, rev = sub.get("hf_repo"), sub.get("revision")
            if not repo or not isinstance(rev, str) or not _FULL_SHA.match(rev):
                raise WorkFault("bad_work", "hf_repo and a 40-hex revision")
            try:
                root = fetch_hf(repo, rev, args.hf_cache_dir)
                kind, _ = score.screen_dir(
                    root, base.base_id, expected_manifest_sha=sub.get("manifest_sha256")
                )
            except ScreeningError as e:
                raise WorkFault(f"screening:{e.reason}", e.detail) from None
            except Exception as e:
                raise WorkFault("download_failed", type(e).__name__) from None
            if sub.get("kind") and sub["kind"] != kind:
                raise WorkFault("screening:kind_mismatch", f"{sub['kind']} != {kind}")
            source = score.ModelSource(base, kind, root, f"{repo}@{rev}")
        try:
            loaded = score.load_scorer(source, args.device, args.hf_cache_dir)
        except ScreeningError as e:
            raise WorkFault(f"screening:{e.reason}", e.detail) from None
        want = sub.get("weights_sha256")
        if want and loaded.weights_sha256 and want != loaded.weights_sha256:
            raise WorkFault("weights_mismatch", "weights_sha256 differs")
        return score.run(
            loaded,
            work.seeds,
            work.bench_version,
            gen,
            cache,
            budget=work.budget_tokens,
            tool_k=work.tool_k,
        )

    return score_fn


def cmd_serve(args: argparse.Namespace) -> int:
    from cpm_scorer.serve import PlatformClient, default_scorer_id, load_token, serve

    client = PlatformClient(args.platform, load_token())
    serve(
        client,
        make_score_fn(args),
        scorer_id=args.scorer_id or default_scorer_id(),
        poll_seconds=args.poll_seconds,
        once=args.once,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cpm-scorer", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--device", default="cpu", help="cpu | cuda | cuda:N | mps")
        p.add_argument("--generate-bin", help="pinned cmd/generate binary")
        p.add_argument("--cache-dir", help="generator and artifact cache")
        p.add_argument("--hf-cache-dir", help="Hugging Face cache directory")

    s = sub.add_parser("score", help="score a model offline (practice path)")
    s.add_argument(
        "--model", required=True, help="'base', a directory, or HF_REPO@REVISION"
    )
    s.add_argument("--base", default="qwen3.5-0.8b", choices=sorted(BASES))
    s.add_argument("--seeds", type=parse_seeds, default=[42, 7, 1234])
    s.add_argument("--bench-version", type=int, default=18)
    s.add_argument("--limit-cases", type=int, help="first N memory and N tool cases")
    s.add_argument("--budget-tokens", type=int, default=metrics.BUDGET_TOKENS)
    s.add_argument("--tool-k", type=int, default=metrics.TOOL_K)
    s.add_argument(
        "--skip-determinism", action="store_true", help="skip the re-run check"
    )
    common(s)
    s.set_defaults(func=cmd_score)

    v = sub.add_parser("serve", help="lease, score and post runs for Platform")
    v.add_argument("--platform", required=True, help="Platform base URL")
    v.add_argument("--poll-seconds", type=float, default=30.0)
    v.add_argument("--once", action="store_true", help="one poll, then exit")
    v.add_argument("--scorer-id")
    common(v)
    v.set_defaults(func=cmd_serve)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

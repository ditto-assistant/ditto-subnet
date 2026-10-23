from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from conftest import build_model, bundle_bytes, vocab_text, write_artifact

from dittobench_reranker.artifact import ArtifactError, load_artifact, pack
from dittobench_reranker.cli import main
from dittobench_reranker.contract import CONTRACT
from dittobench_reranker.pools import PoolFormatError, parse_bundle
from dittobench_reranker.scorer import compare, measure_latency, score

# --- artifact validation (SN-206) --------------------------------------------


def test_valid_artifact_loads(artifact_dir: Path) -> None:
    art = load_artifact(artifact_dir)
    assert len(art.sha256) == 64
    assert art.manifest["base_model_license"] == "mit"


def test_tarball_round_trip_is_deterministic(
    artifact_dir: Path, tmp_path: Path
) -> None:
    first = pack(artifact_dir)
    assert first == pack(artifact_dir)
    tarball = tmp_path / "a.tar.gz"
    tarball.write_bytes(first)
    assert load_artifact(tarball).sha256 == load_artifact(artifact_dir).sha256


def test_wrong_output_name_rejected(tmp_path: Path) -> None:
    d = write_artifact(tmp_path / "a", model=build_model(output_name="scores"))
    with pytest.raises(ArtifactError, match="logits"):
        load_artifact(d)


def test_custom_operator_domain_rejected(tmp_path: Path) -> None:
    d = write_artifact(tmp_path / "a", model=build_model(extra_domain="evil.ops"))
    with pytest.raises(ArtifactError):
        load_artifact(d)


def test_garbage_model_rejected(tmp_path: Path) -> None:
    d = write_artifact(tmp_path / "a", model=b"not onnx")
    with pytest.raises(ArtifactError):
        load_artifact(d)


@pytest.mark.parametrize(
    "manifest",
    [
        {"contract": "other", "base_model": "x", "base_model_license": "mit"},
        {
            "contract": "reranker-season-v1",
            "base_model": "x",
            "base_model_license": "cc-by-nc-4.0",
        },
        {
            "contract": "reranker-season-v1",
            "base_model": "",
            "base_model_license": "mit",
        },
    ],
)
def test_bad_manifest_rejected(tmp_path: Path, manifest: dict) -> None:
    d = write_artifact(tmp_path / "a", manifest=manifest)
    with pytest.raises(ArtifactError):
        load_artifact(d)


def test_special_tokens_must_sit_at_product_ids(tmp_path: Path) -> None:
    lines = vocab_text().split("\n")
    lines[101], lines[102] = lines[102], lines[101]  # swap [CLS] and [SEP]
    d = write_artifact(tmp_path / "a", vocab="\n".join(lines))
    with pytest.raises(ArtifactError, match="CLS"):
        load_artifact(d)


def test_extra_file_rejected(artifact_dir: Path) -> None:
    (artifact_dir / "notes.txt").write_text("hi")
    with pytest.raises(ArtifactError, match="unexpected"):
        load_artifact(artifact_dir)


def test_tarball_path_traversal_rejected(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"x"
        info = tarfile.TarInfo("../model.onnx")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(buf.getvalue())
    with pytest.raises(ArtifactError):
        load_artifact(bad)


def test_oversized_model_rejected(tmp_path: Path) -> None:
    from dataclasses import replace

    from dittobench_reranker.artifact import read_files
    from dittobench_reranker.contract import ArtifactLimits

    d = write_artifact(tmp_path / "a")
    tiny = replace(CONTRACT, artifact=ArtifactLimits(max_model_bytes=10))
    with pytest.raises(ArtifactError, match="exceeds"):
        read_files(d, tiny)


# --- pools --------------------------------------------------------------------


def test_bundle_rejects_gold_outside_candidates() -> None:
    doc = json.loads(bundle_bytes(1))
    doc["sets"][0]["pools"][0]["gold"] = ["c99"]
    with pytest.raises(PoolFormatError, match="subset"):
        parse_bundle(json.dumps(doc).encode(), 20)


def test_bundle_rejects_oversized_pool() -> None:
    with pytest.raises(PoolFormatError):
        parse_bundle(bundle_bytes(1), 10)


def test_bundle_rejects_wrong_schema() -> None:
    with pytest.raises(PoolFormatError, match="schema"):
        parse_bundle(b'{"schema": "x", "sets": []}', 20)


# --- scoring (SN-205) ---------------------------------------------------------


def test_needle_model_beats_base_order(artifact_dir: Path, pools_path: Path) -> None:
    from dittobench_reranker.pools import load_bundle

    bundle = load_bundle(pools_path, 20)
    report = score(load_artifact(artifact_dir), bundle)
    assert report.pools == 6
    assert report.base_ndcg_at_10 == 0.0  # gold sits at c18/c19
    # RRF k=60 with ce_weight 0.7 lifts the two needles to ranks 4 and 5.
    assert report.score > 0.4
    assert report.lift == pytest.approx(report.score)
    assert report.contract_sha256 == CONTRACT.digest()
    assert report.pools_sha256 == bundle.sha256


def test_scoring_is_deterministic(artifact_dir: Path, pools_path: Path) -> None:
    from dittobench_reranker.pools import load_bundle

    bundle = load_bundle(pools_path, 20)
    art = load_artifact(artifact_dir)
    assert score(art, bundle).to_dict() == score(art, bundle).to_dict()


def test_paired_compare_dethrones_only_on_real_gain(
    tmp_path: Path, pools_path: Path
) -> None:
    from dittobench_reranker.pools import load_bundle

    bundle = load_bundle(pools_path, 20)
    champion = score(
        load_artifact(write_artifact(tmp_path / "c", build_model(0.0))), bundle
    )
    challenger = score(
        load_artifact(write_artifact(tmp_path / "x", build_model(1.0))), bundle
    )
    result = compare(champion, challenger)
    assert result.dethrones
    assert not compare(challenger, challenger).dethrones


def test_compare_refuses_different_pools(
    artifact_dir: Path, pools_path: Path, tmp_path: Path
) -> None:
    from dittobench_reranker.pools import load_bundle

    art = load_artifact(artifact_dir)
    a = score(art, load_bundle(pools_path, 20))
    other = tmp_path / "other.json"
    other.write_bytes(bundle_bytes(3))
    b = score(art, load_bundle(other, 20))
    with pytest.raises(ValueError, match="different pools"):
        compare(a, b)


def test_latency_report_shape(artifact_dir: Path, pools_path: Path) -> None:
    from dittobench_reranker.pools import load_bundle

    report = measure_latency(load_artifact(artifact_dir), load_bundle(pools_path, 20))
    assert report.measured_pools == CONTRACT.latency.measured_pools
    assert report.budget_ms == 50.0
    assert report.ratio_to_incumbent is None
    assert report.passed  # the toy model is far below the absolute budget


def test_latency_gate_is_relative_to_incumbent(
    artifact_dir: Path, pools_path: Path
) -> None:
    from dittobench_reranker.pools import load_bundle

    art = load_artifact(artifact_dir)
    report = measure_latency(art, load_bundle(pools_path, 20), incumbent=art)
    assert report.incumbent_p50_ms is not None
    assert report.ratio_to_incumbent is not None
    # Same model timed twice: the ratio is near 1, well inside 3.5.
    assert 0.2 < report.ratio_to_incumbent < 3.5
    assert report.passed


def test_non_finite_logits_fail(tmp_path: Path, pools_path: Path) -> None:
    from dittobench_reranker.pools import load_bundle
    from dittobench_reranker.scorer import ScoringError

    d = write_artifact(tmp_path / "nan", build_model(float("nan")))
    with pytest.raises(ScoringError, match="non-finite"):
        score(load_artifact(d), load_bundle(pools_path, 20))


# --- CLI ----------------------------------------------------------------------


def test_cli_validate_score_compare(
    artifact_dir: Path, pools_path: Path, tmp_path: Path, capsys
) -> None:
    assert main(["validate", str(artifact_dir)]) == 0
    out = tmp_path / "r.json"
    assert (
        main(
            [
                "score",
                "--artifact",
                str(artifact_dir),
                "--pools",
                str(pools_path),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert json.loads(out.read_text())["pools"] == 6
    assert main(["compare", str(out), str(out)]) == 0
    tarball = tmp_path / "a.tar.gz"
    assert main(["pack", str(artifact_dir), str(tarball)]) == 0
    assert tarball.exists()


def test_cli_reports_invalid_artifact(tmp_path: Path, capsys) -> None:
    d = write_artifact(tmp_path / "a", model=b"junk")
    assert main(["validate", str(d)]) == 2
    assert "does not parse" in capsys.readouterr().err

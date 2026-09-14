"""Discovery is location-only exploration, including benign lookalikes."""

import json

import pytest

from ditto_screener.fanout_discovery import semantic_discovery
from ditto_screener.source_review import TarSourceRepository

from .test_source_review import _archive_files


def test_later_source_and_rule_diversity(tmp_path):
    files = {
        "src/a.rs": ("let x = cache.get(question);\n" * 200).encode(),
        "src/z.rs": b"fn compiler_prompt(query: str) {\nreturn compiled_result;\n}\n",
        "src/zz.rs": b"table.question_to_recipe.get(question)\n",
    }
    archive = _archive_files(tmp_path, files)
    report = semantic_discovery(str(archive))
    paths = {x["locations"][0]["path"] for x in report["leads"]}
    assert "src/z.rs" in paths and "src/zz.rs" in paths
    assert len(report["leads"]) <= 8
    assert report["coverage"]["files_scanned"] == 3
    assert {x["kind"] for x in report["leads"]} == {
        "request-key-lookup",
        "program-or-compiler-interface",
        "delegated-result-return",
    }


def test_comments_docs_and_strings_are_not_executable_leads(tmp_path):
    source = b'''# table.get(question)
"""class Program:
return program_result
"""
message = "fn compiler_prompt(question) { cache.get(query) }"
'''
    archive = _archive_files(
        tmp_path,
        {
            "src/main.py": source,
            "docs/a.py": b"return program_result\n",
            "README.md": b"class Program\n",
            "src/lib.rs": b"// return program_result\n/* table.get(question) */\n",
        },
    )
    assert semantic_discovery(str(archive))["leads"] == []


def test_safe_harbor_lookalikes_remain_unadjudicated(tmp_path):
    archive = _archive_files(
        tmp_path,
        {
            "runtime.py": b"""
def compiler_prompt(question):
    return question
class Expression:
    pass
def lookup(query):
    return response_cache.get(query)
"""
        },
    )
    report = semantic_discovery(TarSourceRepository(str(archive)))
    assert report["leads"]
    assert "legitimate" in report["guidance"]
    assert "not a finding or a clearance requirement" in report["guidance"]
    assert "exhaustive" in report["coverage"] and not report["coverage"]["exhaustive"]
    for lead in report["leads"]:
        assert set(lead) == {"kind", "locations"}
        assert set(lead["locations"][0]) == {"path", "line"}
    assert "response_cache" not in json.dumps(report)


def test_bounded_scan_and_output_report_omissions(tmp_path):
    archive = _archive_files(
        tmp_path, {f"src/{i}.py": b"return program_result\n" for i in range(5)}
    )
    report = semantic_discovery(str(archive), max_files=2, max_hints=1)
    assert len(report["leads"]) == 1
    assert report["coverage"]["files_omitted"] == 3
    assert report["coverage"]["hints_omitted"] == 1
    assert report["coverage"]["truncated"]
    report = semantic_discovery(str(archive), max_bytes=1)
    assert not report["leads"] and report["coverage"]["files_omitted"] == 5


@pytest.mark.parametrize(
    "limits", [{"max_hints": 0}, {"max_files": 999999}, {"max_bytes": 0}]
)
def test_invalid_limits_rejected(tmp_path, limits):
    archive = _archive_files(tmp_path, {"main.py": b"pass\n"})
    with pytest.raises(ValueError):
        semantic_discovery(str(archive), **limits)


def test_semantic_validation_feedback_is_hint_not_verdict(tmp_path):
    archive = _archive_files(
        tmp_path,
        {
            "later.py": b"""# The model's draft remains data.
def validate_request(question, expression):
    if expression is None:
        raise ValueError("invalid syntax")
def dispatch(question, expression):
    repair = []
    return validate_request(question, expression)
"""
        },
    )
    report = semantic_discovery(str(archive))
    assert any(x["kind"] == "semantic-validation-feedback" for x in report["leads"])
    assert "syntax/type/resource validation" in report["guidance"]
    assert report["coverage"]["unreadable_files"] == 0


def test_high_priority_later_compiler_survives_generic_early_interfaces(tmp_path):
    source = (
        "def should_compile(question):\n    return False\n\n"
        + "\n" * 40
        + "def compiler_prompt(question):\n    return question\n"
    )
    archive = _archive_files(tmp_path, {"x.py": source.encode()})
    report = semantic_discovery(str(archive), max_hints=1)
    assert report["leads"][0]["locations"][0]["line"] > 40

"""The protected infra workflow plans and applies the AWS roots keylessly."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = (REPO / ".github" / "workflows" / "infra-plan-apply.yml").read_text()
AWS_ROOTS = ("aws-generator-scorer", "aws-screener")


def _search(pattern: str, text: str) -> re.Match[str]:
    match = re.search(pattern, text)
    assert match is not None, pattern
    return match


def test_both_aws_roots_are_selectable_and_bootstrap_is_not() -> None:
    options = _search(r"options: \[(gcp-platform[^\]]*)\]", WORKFLOW).group(1)
    roots = options.split(", ")
    for root in AWS_ROOTS:
        assert root in roots
    # The bootstrap creates the workflow's own identity; it is never a root.
    assert "aws-bootstrap" not in roots
    # Plan and apply map both roots to their exact paths.
    assert WORKFLOW.count("aws-generator-scorer|aws-screener)") == 2


def test_aws_roots_assume_oidc_roles_and_get_no_app_credentials() -> None:
    for name in ("Create exact private plan", "Apply the reviewed binary plan"):
        step = _search(rf"- name: {name}\n\s+if: ([^\n]+)", WORKFLOW).group(1)
        assert "!startsWith(inputs.root, 'aws-')" in step, name
    assert WORKFLOW.count("if: startsWith(inputs.root, 'aws-')") >= 4
    assert "arn:aws:iam::339713011188:role/ditto-subnet-infra-plan" in WORKFLOW
    assert "arn:aws:iam::339713011188:role/ditto-subnet-infra-apply" in WORKFLOW
    assert "aws-access-key-id" not in WORKFLOW
    assert "AWS_SECRET_ACCESS_KEY" not in WORKFLOW

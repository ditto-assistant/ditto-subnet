"""Proof-oriented build/runtime reachability regressions for static preflight."""

from __future__ import annotations

import pytest

from ditto_screener.source_reachability import (
    ReachabilityState,
    analyze_reachability,
)


def _states(files: dict[str, str]) -> dict[str, ReachabilityState]:
    return {path: item.state for path, item in analyze_reachability(files).items()}


def test_missing_dockerfile_keeps_everything_unresolved() -> None:
    assert _states({"src/main.rs": "fn main() {}"}) == {
        "src/main.rs": ReachabilityState.UNRESOLVED
    }


def test_dockerfile_itself_is_always_reachable() -> None:
    result = analyze_reachability({"Dockerfile": "FROM scratch\n"})
    assert result["Dockerfile"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["Dockerfile"].bases == ("docker-build-definition",)


def test_explicit_copy_excludes_local_python_helper() -> None:
    files = {
        "Dockerfile": (
            "FROM python:3.12\nCOPY app/main.py /app/main.py\n"
            'ENTRYPOINT ["python", "/app/main.py"]\n'
        ),
        "app/main.py": "print('ready')",
        "tools/local_diagnostic.py": "post_debug()",
    }
    result = analyze_reachability(files)
    assert result["app/main.py"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["tools/local_diagnostic.py"].state == ReachabilityState.PROVEN_INERT
    assert result["tools/local_diagnostic.py"].bases == (
        "excluded-from-effective-build",
    )


def test_broad_copy_without_execution_is_unresolved_not_reachable() -> None:
    files = {
        "Dockerfile": 'FROM python:3.12\nCOPY . /app\nCMD ["python", "/app/main.py"]\n',
        "main.py": "print('ready')",
        "local_debug.py": "post_debug()",
    }
    result = analyze_reachability(files)
    assert result["main.py"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["local_debug.py"].state == ReachabilityState.UNRESOLVED
    assert result["local_debug.py"].bases == ("copied-without-proven-execution",)


def test_same_basename_at_distinct_destinations_does_not_alias_entrypoint() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM python:3.12\n"
                "COPY service/main.py /srv/main.py\n"
                "COPY tools/main.py /tools/main.py\n"
                'ENTRYPOINT ["python", "/srv/main.py"]\n'
            ),
            "service/main.py": "print('service')",
            "tools/main.py": "print('local tool')",
        }
    )

    assert result["service/main.py"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["tools/main.py"].state == ReachabilityState.UNRESOLVED


def test_later_copy_overwrites_earlier_container_destination() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM python:3.12\n"
                "COPY service/main.py /app/main.py\n"
                "COPY tools/main.py /app/main.py\n"
                'ENTRYPOINT ["python", "/app/main.py"]\n'
            ),
            "service/main.py": "print('service')",
            "tools/main.py": "print('local tool')",
        }
    )

    assert result["service/main.py"].state == ReachabilityState.UNRESOLVED
    assert result["tools/main.py"].state == ReachabilityState.PROVEN_REACHABLE


def test_workdir_resolves_relative_json_entrypoint_without_basename_guessing() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM python:3.12\nWORKDIR /srv\n"
                "COPY service/main.py ./main.py\n"
                "COPY tools/main.py /tools/main.py\n"
                'ENTRYPOINT ["python", "./main.py"]\n'
            ),
            "service/main.py": "print('service')",
            "tools/main.py": "print('local tool')",
        }
    )

    assert result["service/main.py"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["tools/main.py"].state == ReachabilityState.UNRESOLVED


def test_json_copy_and_json_entrypoint_preserve_exact_container_path() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                'FROM python:3.12\nCOPY ["src/main.py", "/app/main.py"]\n'
                'ENTRYPOINT ["python", "/app/main.py"]\n'
            ),
            "src/main.py": "print('service')",
        }
    )

    assert result["src/main.py"].state == ReachabilityState.PROVEN_REACHABLE


def test_runtime_command_resolves_against_final_filesystem_not_declaration_order() -> (
    None
):
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM python:3.12\nCOPY first/main.py /app/main.py\n"
                'ENTRYPOINT ["python", "/app/main.py"]\n'
                "COPY second/main.py /app/main.py\n"
            ),
            "first/main.py": "print('first')",
            "second/main.py": "print('second')",
        }
    )

    assert result["first/main.py"].state == ReachabilityState.UNRESOLVED
    assert result["second/main.py"].state == ReachabilityState.PROVEN_REACHABLE


@pytest.mark.parametrize(
    "instruction",
    [
        'CMD ["python", "/app/server.py"]',
        'ENTRYPOINT ["python", "/app/server.py"]',
        "CMD python /app/server.py",
        "ENTRYPOINT python /app/server.py",
    ],
)
def test_final_runtime_instruction_proves_literal_entrypoint(
    instruction: str,
) -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                f"FROM python:3.12\nCOPY server.py /app/server.py\n{instruction}\n"
            ),
            "server.py": "print('ready')",
        }
    )
    assert result["server.py"].state == ReachabilityState.PROVEN_REACHABLE
    assert any("final-stage" in basis for basis in result["server.py"].bases)


@pytest.mark.parametrize(
    "instruction",
    [
        "RUN ./scripts/build.sh",
        "RUN sh scripts/build.sh",
        "RUN bash ./scripts/build.sh",
    ],
)
def test_docker_run_proves_invoked_script(instruction: str) -> None:
    result = analyze_reachability(
        {
            "Dockerfile": f'FROM alpine\nCOPY . .\n{instruction}\nCMD ["/bin/true"]\n',
            "scripts/build.sh": "echo built",
        }
    )
    assert result["scripts/build.sh"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["scripts/build.sh"].bases == ("docker-run:stage-0",)


def test_chmod_does_not_prove_script_execution() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM alpine\nCOPY . .\nRUN chmod +x scripts/local.sh\n"
                'CMD ["/bin/true"]\n'
            ),
            "scripts/local.sh": "echo local",
        }
    )
    assert result["scripts/local.sh"].state == ReachabilityState.UNRESOLVED


def test_unused_multistage_builder_is_not_effective_build() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM alpine AS unused\nCOPY danger.sh /danger.sh\nRUN /danger.sh\n"
                'FROM scratch\nCMD ["/safe"]\n'
            ),
            "danger.sh": "echo unused",
        }
    )
    assert result["danger.sh"].state == ReachabilityState.PROVEN_INERT


def test_copy_from_makes_builder_ancestor_effective() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM alpine AS build\nCOPY build.sh /build.sh\nRUN /build.sh\n"
                "FROM scratch\nCOPY --from=build /out/server /server\n"
                'ENTRYPOINT ["/server"]\n'
            ),
            "build.sh": "echo built",
        }
    )
    assert result["build.sh"].state == ReachabilityState.PROVEN_REACHABLE


def test_copy_from_tracks_source_mapping_into_final_stage() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM python:3.12 AS build\n"
                "COPY src/main.py /workspace/main.py\n"
                "FROM python:3.12\n"
                "COPY --from=build /workspace/main.py /app/main.py\n"
                'ENTRYPOINT ["python", "/app/main.py"]\n'
            ),
            "src/main.py": "print('service')",
        }
    )

    assert result["src/main.py"].state == ReachabilityState.PROVEN_REACHABLE


def test_from_stage_inherits_workdir_and_exact_source_mapping() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM python:3.12 AS base\nWORKDIR /srv\n"
                "COPY src/main.py ./main.py\n"
                "FROM base AS final\n"
                'ENTRYPOINT ["python", "./main.py"]\n'
            ),
            "src/main.py": "print('service')",
        }
    )

    assert result["src/main.py"].state == ReachabilityState.PROVEN_REACHABLE


def test_from_named_stage_makes_parent_effective() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM alpine AS base\nCOPY prepare.sh /prepare.sh\nRUN /prepare.sh\n"
                'FROM base AS final\nCMD ["/bin/true"]\n'
            ),
            "prepare.sh": "echo built",
        }
    )
    assert result["prepare.sh"].state == ReachabilityState.PROVEN_REACHABLE


@pytest.mark.parametrize(
    "copy",
    [
        "COPY $SOURCE /app",
        "COPY ${SOURCE} /app",
        "COPY ../outside /app",
    ],
)
def test_dynamic_or_outside_copy_never_proves_reachability(copy: str) -> None:
    result = analyze_reachability(
        {
            "Dockerfile": f'FROM alpine\n{copy}\nCMD ["/bin/true"]\n',
            "src/main.rs": "fn main() {}",
        }
    )
    assert result["src/main.rs"].state == ReachabilityState.UNRESOLVED


@pytest.mark.parametrize(
    "dockerfile",
    [
        (
            "FROM python:3.12\nCOPY main.py /app/main.py\n"
            'ENTRYPOINT ["python", "$APP"]\n'
        ),
        (
            "FROM python:3.12\nCOPY main.py /app/main.py\n"
            "RUN --mount=type=bind,target=/src python /app/main.py\n"
        ),
        (
            "FROM python:3.12\nADD https://example.invalid/main.py /app/main.py\n"
            'ENTRYPOINT ["python", "/app/main.py"]\n'
        ),
        (
            "FROM python:3.12\nWORKDIR $APP_DIR\nCOPY main.py ./main.py\n"
            'ENTRYPOINT ["python", "./main.py"]\n'
        ),
    ],
)
def test_dynamic_buildkit_forms_never_prove_candidate(dockerfile: str) -> None:
    result = analyze_reachability(
        {"Dockerfile": dockerfile, "main.py": "print('service')"}
    )

    assert result["main.py"].state != ReachabilityState.PROVEN_REACHABLE


def test_dynamic_from_never_proves_literal_entrypoint() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "ARG BASE=python:3.12\nFROM ${BASE}\n"
                "COPY main.py /app/main.py\n"
                'ENTRYPOINT ["python", "/app/main.py"]\n'
            ),
            "main.py": "print('service')",
        }
    )

    assert result["main.py"].state == ReachabilityState.UNRESOLVED


def test_literal_copy_glob_is_resolved() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM python:3.12\nCOPY *.py /app/\n"
                'ENTRYPOINT ["python", "/app/main.py"]\n'
            ),
            "main.py": "print('ready')",
            "notes.txt": "not source",
        }
    )
    assert result["main.py"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["notes.txt"].state == ReachabilityState.PROVEN_INERT


def test_cargo_build_proves_default_binary_and_build_script() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM rust:bookworm AS build\nCOPY . .\nRUN cargo build --release\n"
                "FROM scratch\nCOPY --from=build /target/release/app /app\n"
                'ENTRYPOINT ["/app"]\n'
            ),
            "Cargo.toml": '[package]\nname="app"\nversion="0.1.0"\n',
            "build.rs": "fn main() {}",
            "src/main.rs": "fn main() {}",
            "tests/security.rs": "#[test] fn probe() {}",
        }
    )
    assert result["Cargo.toml"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["build.rs"].state == ReachabilityState.PROVEN_REACHABLE
    assert "cargo-build-script" in result["build.rs"].bases
    assert result["src/main.rs"].state == ReachabilityState.PROVEN_REACHABLE
    assert "cargo-target" in result["src/main.rs"].bases
    assert result["tests/security.rs"].state == ReachabilityState.UNRESOLVED


def test_cargo_custom_binary_and_build_paths_are_reachable() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": "FROM rust:bookworm\nCOPY . .\nRUN cargo build --release\n",
            "Cargo.toml": (
                '[package]\nname="app"\nversion="0.1.0"\nbuild="ops/generate.rs"\n'
                '[[bin]]\nname="app"\npath="service/entry.rs"\n'
            ),
            "ops/generate.rs": "fn main() {}",
            "service/entry.rs": "fn main() {}",
        }
    )
    assert result["ops/generate.rs"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["service/entry.rs"].state == ReachabilityState.PROVEN_REACHABLE


def test_cargo_workdir_selects_one_manifest_without_basename_aliasing() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM rust:bookworm\n"
                "COPY service /workspace/service\n"
                "COPY tools /workspace/tools\n"
                "WORKDIR /workspace/service\nRUN cargo build --release\n"
            ),
            "service/Cargo.toml": '[package]\nname="service"\nversion="0.1.0"\n',
            "service/src/main.rs": "fn main() {}",
            "tools/Cargo.toml": '[package]\nname="tools"\nversion="0.1.0"\n',
            "tools/src/main.rs": "fn main() {}",
        }
    )

    assert result["service/src/main.rs"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["tools/src/main.rs"].state == ReachabilityState.UNRESOLVED


def test_dynamic_cargo_command_never_proves_targets() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM rust:bookworm\nCOPY . /workspace\nWORKDIR /workspace\n"
                "RUN cargo build $CARGO_FLAGS\n"
            ),
            "Cargo.toml": '[package]\nname="app"\nversion="0.1.0"\n',
            "src/main.rs": "fn main() {}",
        }
    )

    assert result["src/main.rs"].state == ReachabilityState.UNRESOLVED


def test_cargo_path_dependency_target_is_reachable() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": "FROM rust:bookworm\nCOPY . .\nRUN cargo build --release\n",
            "Cargo.toml": (
                '[package]\nname="app"\nversion="0.1.0"\n'
                '[dependencies]\nshared={path="crates/shared"}\n'
            ),
            "src/main.rs": "fn main() {}",
            "crates/shared/Cargo.toml": ('[package]\nname="shared"\nversion="0.1.0"\n'),
            "crates/shared/src/lib.rs": "pub fn value() {}",
        }
    )
    assert (
        result["crates/shared/src/lib.rs"].state == ReachabilityState.PROVEN_REACHABLE
    )


def test_rust_mod_graph_is_followed_from_cargo_root() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": "FROM rust:bookworm\nCOPY . .\nRUN cargo build --release\n",
            "Cargo.toml": '[package]\nname="app"\nversion="0.1.0"\n',
            "src/main.rs": "mod service; fn main() { service::run(); }",
            "src/service.rs": "pub fn run() {}",
            "src/unreferenced.rs": "pub fn local() {}",
        }
    )
    assert result["src/service.rs"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["src/unreferenced.rs"].state == ReachabilityState.UNRESOLVED


@pytest.mark.parametrize(
    "source",
    [
        'include!("../vendor/runtime.rs");',
        '#[path = "../vendor/runtime.rs"]\nmod runtime;',
    ],
)
def test_literal_rust_source_indirection_is_followed(source: str) -> None:
    result = analyze_reachability(
        {
            "Dockerfile": "FROM rust:bookworm\nCOPY . .\nRUN cargo build --release\n",
            "Cargo.toml": '[package]\nname="app"\nversion="0.1.0"\n',
            "src/main.rs": source,
            "vendor/runtime.rs": "pub fn run() {}",
        }
    )
    assert result["vendor/runtime.rs"].state == ReachabilityState.PROVEN_REACHABLE


@pytest.mark.parametrize(
    "source",
    [
        '#[cfg(test)]\ninclude!("../tests/payload.rs");\nfn main() {}',
        '#[cfg(test)]\n#[path = "../tests/payload.rs"]\nmod payload;\nfn main() {}',
    ],
)
def test_test_gated_rust_indirection_is_not_reachable(source: str) -> None:
    result = analyze_reachability(
        {
            "Dockerfile": "FROM rust:bookworm\nCOPY . .\nRUN cargo build --release\n",
            "Cargo.toml": '[package]\nname="app"\nversion="0.1.0"\n',
            "src/main.rs": source,
            "tests/payload.rs": "fn payload() {}",
        }
    )
    assert result["tests/payload.rs"].state == ReachabilityState.UNRESOLVED


def test_not_test_gated_rust_indirection_is_production_reachable() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": "FROM rust:bookworm\nCOPY . .\nRUN cargo build --release\n",
            "Cargo.toml": '[package]\nname="app"\nversion="0.1.0"\n',
            "src/main.rs": (
                '#[cfg(not(test))]\ninclude!("../vendor/runtime.rs");\nfn main() {}'
            ),
            "vendor/runtime.rs": "pub fn run() {}",
        }
    )

    assert result["vendor/runtime.rs"].state == ReachabilityState.PROVEN_REACHABLE


def test_dynamic_rust_include_marks_remaining_rust_unresolved() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": "FROM rust:bookworm\nCOPY . .\nRUN cargo build --release\n",
            "Cargo.toml": '[package]\nname="app"\nversion="0.1.0"\n',
            "src/main.rs": 'include!(env!("GENERATED_SOURCE"));',
            "generated/unknown.rs": "fn generated() {}",
        }
    )
    assert result["generated/unknown.rs"].state == ReachabilityState.UNRESOLVED


def test_python_absolute_import_graph_is_followed() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM python:3.12\nCOPY . /app\n"
                'ENTRYPOINT ["python", "/app/app/main.py"]\n'
            ),
            "app/main.py": "from app import service\nservice.run()",
            "app/service.py": "def run(): pass",
            "app/local.py": "def preview(): pass",
        }
    )
    assert result["app/service.py"].state == ReachabilityState.PROVEN_REACHABLE
    assert result["app/local.py"].state == ReachabilityState.UNRESOLVED


def test_python_relative_import_graph_is_followed() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                "FROM python:3.12\nCOPY . /app\n"
                'ENTRYPOINT ["python", "/app/pkg/main.py"]\n'
            ),
            "pkg/main.py": "from . import service\nservice.run()",
            "pkg/service.py": "def run(): pass",
        }
    )
    assert result["pkg/service.py"].state == ReachabilityState.PROVEN_REACHABLE


@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nimportlib.import_module(name)",
        "__import__(module_name)",
    ],
)
def test_dynamic_python_import_keeps_other_sources_unresolved(source: str) -> None:
    result = analyze_reachability(
        {
            "Dockerfile": (
                'FROM python:3.12\nCOPY . /app\nENTRYPOINT ["python", "/app/main.py"]\n'
            ),
            "main.py": source,
            "plugins/unknown.py": "def run(): pass",
        }
    )
    assert result["plugins/unknown.py"].state == ReachabilityState.UNRESOLVED


@pytest.mark.parametrize(
    ("entry_source", "dependency"),
    [
        ('import "./service.js";', "service.js"),
        ('const service = require("./service");', "service.js"),
        ('source "./helpers/start.sh"', "helpers/start.sh"),
        ('. "./helpers/start.sh"', "helpers/start.sh"),
    ],
)
def test_literal_script_imports_are_followed(
    entry_source: str, dependency: str
) -> None:
    entry = "main.js" if dependency.endswith(".js") else "main.sh"
    result = analyze_reachability(
        {
            "Dockerfile": f'FROM alpine\nCOPY . /app\nENTRYPOINT ["/app/{entry}"]\n',
            entry: entry_source,
            dependency: "export VALUE=1",
        }
    )
    assert result[dependency].state == ReachabilityState.PROVEN_REACHABLE


def test_go_test_file_is_not_proven_by_release_entrypoint() -> None:
    result = analyze_reachability(
        {
            "Dockerfile": "FROM golang\nCOPY . .\nRUN go build ./cmd/server\n",
            "cmd/server/main.go": "package main\nfunc main() {}",
            "cmd/server/main_test.go": "package main\nfunc TestProbe() {}",
        }
    )
    assert result["cmd/server/main_test.go"].state != ReachabilityState.PROVEN_REACHABLE


def test_output_is_deterministic_across_input_order() -> None:
    files = {
        "Dockerfile": 'FROM python:3.12\nCOPY . /app\nCMD ["python", "/app/main.py"]\n',
        "main.py": "import helper",
        "helper.py": "VALUE = 1",
    }
    forward = analyze_reachability(files)
    reverse = analyze_reachability(dict(reversed(list(files.items()))))
    assert forward == reverse

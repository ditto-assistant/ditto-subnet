"""Family-diff attribution: the released-base copy-hold rule, without a model.

Each scenario mirrors the shape of a settled precedent with anonymized agents:
a released base, an unreleased matched agent built on it, and a held agent
whose overlap with the matched agent either is or is not explained by the base.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ditto.api_server.copy_court_family import (
    STARTER_KIT_BASE,
    ArtifactView,
    DisclosureProof,
    FamilyContext,
    PublicBase,
    ScreeningEvidence,
    attribute_family,
    declared_references,
    family_verdict,
)

_T0 = datetime(2026, 10, 20, tzinfo=UTC)
_BASE_UUID = "11111111-2222-4333-8444-555555555555"

_CONFIG = "MAX_TOKENS = 4096\nTIMEOUT_SECONDS = 30\nRETRIES = 3\nMODEL = 'x'\n"
_STORE = (
    "def save_note(store, note):\n"
    "    store.append({'text': note.text, 'saved_at': note.saved_at})\n"
    "    return len(store)\n"
    "def load_notes(store, user_id):\n"
    "    return [row for row in store if row['user_id'] == user_id]\n"
)
_ACTION_BASE = (
    "SYSTEM = 'Answer from the records you were given and cite them.'\n"
    "def plan(request, catalog):\n"
    "    return call_model(SYSTEM, request, tools=catalog)\n"
    "def finish(answer):\n"
    "    return answer.strip()\n"
)
# The matched agent's own five sentences on top of the base.
_MATCHED_SENTENCES = [
    "HINT_A = 'Prefer the newest dated record when two notes disagree on value.'",
    "HINT_B = 'Quote the amount exactly as the note writes it, with its unit.'",
    "HINT_C = 'When a ticket number appears, carry it into the final reply line.'",
    "HINT_D = 'Never merge two people who share a first name without a surname.'",
    "HINT_E = 'Report the currency symbol before the number, never after it.'",
]
_ACTION_MATCHED = _ACTION_BASE + "\n".join(_MATCHED_SENTENCES) + "\n"
# The held agent's own, unrelated delta on the same file.
_ACTION_HELD = _ACTION_BASE + (
    "LANE_NOTE = 'Pick the advisory brief only when every capability stays offered.'\n"
    "def closure(refs):\n"
    "    return sorted({ref.number for ref in refs if ref.number is not None})\n"
)


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def _view(agent_id: str, label: str, files: dict[str, str], hours: int) -> ArtifactView:
    return ArtifactView(
        agent_id=agent_id,
        label=label,
        sha256=_digest(agent_id),
        created_at=_T0 + timedelta(hours=hours),
        digests={path: _digest(text) for path, text in files.items()},
        texts=dict(files),
    )


_BASE_FILES = {
    "app/__init__.py": "",
    "app/config.py": _CONFIG,
    "app/store.py": _STORE,
    "app/action.py": _ACTION_BASE,
}
_BASE = _view("b" * 32, "base v25", _BASE_FILES, 0)
_MATCHED = _view(
    "m" * 32,
    "matched v4",
    {**_BASE_FILES, "app/action.py": _ACTION_MATCHED},
    24,
)
_HELD = _view(
    "h" * 32,
    "held v1",
    {
        **_BASE_FILES,
        "app/action.py": _ACTION_HELD,
        "BASE.md": f"Built on base v25, sha {_BASE.sha256}\n",
    },
    48,
)
_SCREENED = ScreeningEvidence(
    attempt_id="attempt-1",
    policy_version=13,
    agent_policy_version=13,
    lineage_scope="full",
    open_quarantines=0,
)


def _proof(view: ArtifactView, *, proven: bool, relation: str) -> DisclosureProof:
    return DisclosureProof(
        agent_id=view.agent_id,
        label=view.label,
        sha256=view.sha256,
        relation=relation,
        publication_at=_T0 + timedelta(hours=1) if proven else None,
        handoff_count=12 if proven else 0,
        proven=proven,
    )


def _context(
    held: ArtifactView,
    matched: ArtifactView,
    disclosures: list[DisclosureProof],
    *,
    same_owner: bool = False,
    matched_public: bool = False,
    screening: ScreeningEvidence = _SCREENED,
) -> FamilyContext:
    return FamilyContext(
        held=held,
        matched=matched,
        matched_public=matched_public,
        same_owner=same_owner,
        matched_after_held=False,
        substituted_from=None,
        disclosures=disclosures,
        screening=screening,
        clear_precedents=[{"kind": "resolved_ath_review", "agent_id": "p-clear"}],
        reject_precedents=[{"kind": "resolved_ath_review", "agent_id": "p-reject"}],
    )


def test_declared_references_reads_only_declaration_documents() -> None:
    shas, ids = declared_references(
        {
            "BASE.md": f"base {'a' * 64} agent {_BASE_UUID}",
            "app/hash.py": f"KNOWN = '{'c' * 64}'",
        }
    )
    assert shas == {"a" * 64}
    assert ids == {_BASE_UUID}


def test_released_base_explains_overlap_and_clears() -> None:
    """Shared files are base bytes; the matched agent's sentences are absent."""
    facts = attribute_family(
        held=_HELD,
        matched=_MATCHED,
        bases=[PublicBase(_BASE, "declared_by_held", _T0)],
    )
    assert facts.shared_count == 3
    assert facts.unexplained == []
    assert facts.base_explains == {_BASE.agent_id: 3}
    assert facts.distinctive_total == len(_MATCHED_SENTENCES)
    assert facts.distinctive_present_count == 0
    assert facts.primary_base == _BASE.agent_id
    assert [row["path"] for row in facts.held_delta["modified"]] == ["app/action.py"]
    verdict = family_verdict(
        _context(
            _HELD,
            _MATCHED,
            [
                _proof(_MATCHED, proven=False, relation="matched"),
                _proof(_BASE, proven=True, relation="declared_by_held"),
            ],
        ),
        facts,
    )
    assert verdict.verdict == "clear"
    assert verdict.basis == "released_base_explains_overlap"
    assert verdict.auto_clear_eligible
    assert verdict.citations[0]["agent_id"] == "p-clear"


def test_matched_distinctive_lines_present_recommends_reject() -> None:
    """The held agent carries code that exists only in the unreleased match."""
    copied = _view(
        "c" * 32,
        "copier v24",
        {
            **_BASE_FILES,
            "app/action.py": _ACTION_MATCHED + "EXTRA = 'own line here ok'\n",
        },
        48,
    )
    facts = attribute_family(
        held=copied,
        matched=_MATCHED,
        bases=[PublicBase(_BASE, "similar_public", _T0)],
    )
    assert facts.distinctive_present_count == len(_MATCHED_SENTENCES)
    verdict = family_verdict(
        _context(
            copied,
            _MATCHED,
            [
                _proof(_MATCHED, proven=False, relation="matched"),
                _proof(_BASE, proven=True, relation="similar_public"),
            ],
        ),
        facts,
    )
    assert verdict.verdict == "reject"
    assert not verdict.auto_clear_eligible
    assert verdict.citations[0]["agent_id"] == "p-reject"


def test_matched_file_copied_whole_is_unexplained() -> None:
    copied = _view("c" * 32, "copier", dict(_MATCHED.texts), 48)
    facts = attribute_family(
        held=copied, matched=_MATCHED, bases=[PublicBase(_BASE, "similar_public", _T0)]
    )
    assert facts.unexplained == ["app/action.py"]


def test_no_proven_base_escalates() -> None:
    facts = attribute_family(held=_HELD, matched=_MATCHED, bases=[])
    verdict = family_verdict(
        _context(
            _HELD,
            _MATCHED,
            [
                _proof(_MATCHED, proven=False, relation="matched"),
                _proof(_BASE, proven=False, relation="declared_by_held"),
            ],
        ),
        facts,
    )
    assert verdict.verdict == "escalate"
    assert any(
        "missing handoff does not prove privacy" in u for u in verdict.unresolved
    )


def test_older_public_ancestor_in_matched_lineage_explains_overlap() -> None:
    """The hold names a later unreleased version; the held agent inherits an
    older public version of the same lineage (discriminating files match it)."""
    old_world = "WORLD = 'v12 world model with the original routing table'\n" * 3
    new_world = "WORLD = 'v25 world model with the rebuilt routing table'\n" * 3
    v12 = _view("1" * 32, "lineage v12", {**_BASE_FILES, "src/world.rs": old_world}, 0)
    v25 = _view("2" * 32, "lineage v25", {**_BASE_FILES, "src/world.rs": new_world}, 30)
    held = _view(
        "h" * 32,
        "held v1",
        {**_BASE_FILES, "src/world.rs": old_world, "src/protocol.rs": "fn v10() {}\n"},
        40,
    )
    facts = attribute_family(
        held=held, matched=v25, bases=[PublicBase(v12, "matched_owner_lineage", _T0)]
    )
    assert facts.unexplained == []
    assert facts.distinctive_present_count == 0
    verdict = family_verdict(
        _context(
            held,
            v25,
            [
                _proof(v25, proven=False, relation="matched"),
                _proof(v12, proven=True, relation="matched_owner_lineage"),
            ],
        ),
        facts,
    )
    assert verdict.verdict == "clear"


def test_shared_derivative_base_explains_overlap() -> None:
    """Held builds on a public derivative of the same public root the matched
    agent extends; the root's bytes survive in the derivative."""
    derivative = _view(
        "d" * 32,
        "derivative v6",
        {**_BASE_FILES, "app/nick.py": "NICK = 'nickname lines from the derivative'\n"},
        10,
    )
    held = _view(
        "h" * 32,
        "held v5",
        {**derivative.texts, "app/deadline.py": "TOTAL_DEADLINE_SECONDS = 120\n"},
        48,
    )
    facts = attribute_family(
        held=held,
        matched=_MATCHED,
        bases=[PublicBase(derivative, "declared_by_held", _T0)],
    )
    assert facts.unexplained == []
    assert facts.distinctive_present_count == 0


def test_held_owner_prior_version_suppresses_lines_it_wrote_first() -> None:
    prior = _view("p" * 32, "held v0", {"app/action.py": _ACTION_MATCHED}, 12)
    facts = attribute_family(
        held=_view("h" * 32, "held v1", dict(_MATCHED.texts), 48),
        matched=_MATCHED,
        bases=[PublicBase(_BASE, "similar_public", _T0)],
        held_prior=[prior],
    )
    assert facts.distinctive_total == 0
    assert facts.prior_suppressed == len(_MATCHED_SENTENCES)


def test_starter_kit_content_is_explained() -> None:
    kit_text = "KIT_HELPER = 'shipped with the official starter kit for miners'\n" * 4
    matched = _view("m" * 32, "matched", {"kit/helper.py": kit_text}, 10)
    held = _view("h" * 32, "held", {"kit/helper.py": kit_text}, 20)
    facts = attribute_family(
        held=held,
        matched=matched,
        bases=[],
        kit_digests=frozenset({_digest(kit_text)}),
    )
    assert facts.shared[0]["explained_by"] == STARTER_KIT_BASE
    assert facts.unexplained == []


def test_missing_screening_blocks_auto_clear_but_keeps_recommendation() -> None:
    facts = attribute_family(
        held=_HELD, matched=_MATCHED, bases=[PublicBase(_BASE, "declared_by_held", _T0)]
    )
    verdict = family_verdict(
        _context(
            _HELD,
            _MATCHED,
            [_proof(_BASE, proven=True, relation="declared_by_held")],
            screening=ScreeningEvidence(
                attempt_id=None,
                policy_version=None,
                agent_policy_version=13,
                lineage_scope=None,
                open_quarantines=0,
            ),
        ),
        facts,
    )
    assert verdict.verdict == "clear"
    assert not verdict.auto_clear_eligible
    assert "delta read" in verdict.auto_clear_blockers[0]


def test_same_owner_clears_without_auto_apply() -> None:
    facts = attribute_family(held=_HELD, matched=_MATCHED, bases=[])
    verdict = family_verdict(_context(_HELD, _MATCHED, [], same_owner=True), facts)
    assert verdict.verdict == "clear"
    assert verdict.basis == "same_owner"
    assert not verdict.auto_clear_eligible


def test_public_matched_agent_explains_everything() -> None:
    facts = attribute_family(
        held=_view(
            "c" * 32,
            "copier",
            {**_MATCHED.texts, "app/extra.py": "def extra(x):\n    return x + 1\n"},
            48,
        ),
        matched=_MATCHED,
        bases=[PublicBase(_MATCHED, "matched", _T0)],
    )
    assert facts.unexplained == [] and facts.distinctive_total == 0
    assert facts.held_delta["runtime_changed"] == ["app/extra.py"]
    verdict = family_verdict(
        _context(
            _HELD,
            _MATCHED,
            [_proof(_MATCHED, proven=True, relation="matched")],
            matched_public=True,
        ),
        facts,
    )
    assert verdict.verdict == "clear"
    assert verdict.basis == "matched_agent_public"
    assert not verdict.auto_clear_eligible


def test_declared_name_versions_reads_prose_bases() -> None:
    from ditto.api_server.copy_court_family import declared_name_versions

    pairs = declared_name_versions(
        {
            "DECLARATION.md": "Public base: base-agent v21, plus Hades v6 notes.",
            "app/x.py": "MODEL = 'gpt v5'",
        }
    )
    assert pairs == {("base-agent", 21), ("Hades", 6)}


def test_held_owner_prior_explains_shared_bytes_but_blocks_auto_clear() -> None:
    own = "OWN_TABLE = 'held owner wrote this table before the match existed'\n" * 3
    prior = _view("p" * 32, "held v0", {"app/table.py": own}, 6)
    matched = _view("m" * 32, "matched", {"app/table.py": own}, 24)
    held = _view("h" * 32, "held v1", {"app/table.py": own}, 48)
    facts = attribute_family(
        held=held,
        matched=matched,
        bases=[PublicBase(_BASE, "similar_public", _T0)],
        held_prior=[prior],
    )
    assert facts.unexplained == []
    assert facts.prior_explained_count == 1
    verdict = family_verdict(
        _context(
            held, matched, [_proof(_BASE, proven=True, relation="similar_public")]
        ),
        facts,
    )
    assert verdict.verdict == "clear"
    assert not verdict.auto_clear_eligible
    assert any("who came first" in b for b in verdict.auto_clear_blockers)


def test_unrelated_public_bases_cannot_carry_a_reject() -> None:
    """A reformatted near-copy with no byte-shared files and only unrelated
    public agents as bases: every matched line counts as distinctive, so the
    court escalates rather than recommending a reject on that alone."""
    unrelated = _view(
        "u" * 32, "unrelated v3", {"lib/other.py": "OTHER = 'nothing in common'\n"}, 0
    )
    matched = _view("m" * 32, "matched", {"app/action.py": _ACTION_MATCHED}, 24)
    held = _view(
        "h" * 32,
        "held",
        {"app/action.py": _ACTION_MATCHED.replace("    ", "\t") + "# reformatted\n"},
        48,
    )
    facts = attribute_family(
        held=held,
        matched=matched,
        bases=[PublicBase(unrelated, "similar_public", _T0)],
    )
    assert facts.shared_count == 0
    assert facts.matched_base_overlap == 0
    assert facts.distinctive_present_count >= 3
    verdict = family_verdict(
        _context(
            held, matched, [_proof(unrelated, proven=True, relation="similar_public")]
        ),
        facts,
    )
    assert verdict.verdict == "escalate"
    assert any("matched family" in item for item in verdict.unresolved)


def test_one_convergent_edit_of_a_base_line_clears_without_auto_apply() -> None:
    """Held and matched both made the same one-token edit to a base line."""
    base_line = (
        "    return LEAD_HEADER + '\\n' + '\\n'.join(f'{n}. {t}' for n, t in rows)\n"
    )
    edited = base_line.replace("LEAD_HEADER", "header")
    base = _view("b" * 32, "base", {"app/short.py": "def lead(rows):\n" + base_line}, 0)
    matched = _view(
        "m" * 32,
        "matched",
        {"app/short.py": "def lead(rows):\n" + edited + "\n".join(_MATCHED_SENTENCES)},
        24,
    )
    held = _view("h" * 32, "held", {"app/short.py": "def lead(rows):\n" + edited}, 48)
    facts = attribute_family(
        held=held, matched=matched, bases=[PublicBase(base, "declared_by_held", _T0)]
    )
    assert facts.distinctive_present_count == 0
    assert len(facts.convergent_edits) == 1
    verdict = family_verdict(
        _context(
            held, matched, [_proof(base, proven=True, relation="declared_by_held")]
        ),
        facts,
    )
    assert verdict.verdict == "clear"
    assert "convergent" in verdict.reason
    assert not verdict.auto_clear_eligible


def test_many_convergent_edits_count_as_present() -> None:
    base_lines = [f"VALUE_{i} = compute_value_{i}(record, request)\n" for i in range(5)]
    edited = [line.replace("request", "req") for line in base_lines]
    base = _view("b" * 32, "base", {"app/x.py": "".join(base_lines)}, 0)
    matched = _view("m" * 32, "matched", {"app/x.py": "".join(edited)}, 24)
    held = _view("h" * 32, "held", {"app/x.py": "".join(edited) + "OWN = 1\n"}, 48)
    facts = attribute_family(
        held=held, matched=matched, bases=[PublicBase(base, "similar_public", _T0)]
    )
    assert facts.convergent_edits == []
    assert facts.distinctive_present_count == 5


def _unmodified_fork(files: dict[str, str]) -> tuple:
    held = _view("f" * 32, "fork v3", files, 48)
    facts = attribute_family(
        held=held,
        matched=_MATCHED,
        bases=[PublicBase(_BASE, "declared_by_held", _T0)],
    )
    verdict = family_verdict(
        _context(
            held,
            _MATCHED,
            [
                _proof(_MATCHED, proven=False, relation="matched"),
                _proof(_BASE, proven=True, relation="declared_by_held"),
            ],
        ),
        facts,
    )
    return facts, verdict


def test_unmodified_fork_of_released_base_recommends_reject() -> None:
    """Operator ruling 2026-10-29: forking released source needs own changes.

    Runtime files byte-identical to the released base, only declaration,
    changelog and upstream prose changed: an exact duplicate, never a CLEAR.
    """
    facts, verdict = _unmodified_fork(
        {
            **_BASE_FILES,
            "DECLARATION.md": f"Fork of base v25, sha {_BASE.sha256}\n",
            "CHANGES.md": "- credit the base author\n",
            "UPSTREAM.md": "base v25\n",
        }
    )
    assert facts.unexplained == [] and facts.distinctive_present_count == 0
    assert facts.held_delta["added_count"] == 3
    assert facts.held_delta["runtime_changed_count"] == 0
    assert verdict.verdict == "reject"
    assert verdict.basis == "unmodified_fork_of_released_base"
    assert not verdict.auto_clear_eligible
    assert verdict.citations[0]["agent_id"] == "p-reject"


def test_whitespace_only_runtime_edit_is_not_an_own_change() -> None:
    _facts, verdict = _unmodified_fork(
        {**_BASE_FILES, "app/store.py": _STORE.replace("    ", "      ") + "\n\n"}
    )
    assert verdict.basis == "unmodified_fork_of_released_base"


def test_removal_only_fork_escalates() -> None:
    files = {path: text for path, text in _BASE_FILES.items() if path != "app/store.py"}
    facts, verdict = _unmodified_fork(files)
    assert facts.held_delta["runtime_removed_count"] == 1
    assert verdict.verdict == "escalate"
    assert verdict.basis == "own_delta_removal_only"

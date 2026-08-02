"""Bounded semantic leads for the agentic source reviewer.

These rules do not decide policy. They identify nearby combinations of generic
source concepts that deserve an explicit reachability check by the reviewer.
Only locations and semantic roles are returned; source text and matched values
never leave the archive through this module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_MAX_LEADS = 32
_MAX_LEADS_PER_RULE_FILE = 4
_WINDOW_LINES = 18
_MAX_STATIC_FINDINGS = 16
_COMMAND_EXECUTION_EFFECT = re.compile(
    r"\b(?:Command::new|subprocess\.|os\.system|child_process\.|execFile|spawn)\b"
)


@dataclass(frozen=True)
class _Role:
    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class _Rule:
    kind: str
    roles: tuple[_Role, ...]
    build_files_only: bool = False
    # Suppressors encode the verifier's false-positive guards structurally: if
    # ANY suppressor role also matches within ``_WINDOW_LINES`` of the anchor,
    # the lead is dropped. This keeps a lead from firing on a legitimate
    # look-alike (a genuine model call in the window, an injection-defense
    # strip sink, a request-user source, or a documented list/history family)
    # without turning the co-occurrence engine into a verdict engine. A lead is
    # still only a location-only routing signal for the agentic reviewer.
    suppressors: tuple[_Role, ...] = ()


@dataclass(frozen=True)
class _StaticRule:
    category: str
    kind: str
    roles: tuple[_Role, ...]


def _words(value: str) -> re.Pattern[str]:
    return re.compile(value, re.IGNORECASE)


_RULES = (
    _Rule(
        "challenge-shaped-retrieval-override",
        (
            _Role(
                "challenge-shape",
                _words(
                    r"\b(?:canary|rare[-_ ]?code|challenge[-_ ]?(?:token|value)|"
                    r"grader[-_ ]?slot|scorer[-_ ]?slot)\b"
                ),
            ),
            _Role(
                "input-recognition",
                _words(
                    r"\b(?:contains|starts_with|ends_with|matches|regex|parse|"
                    r"detect|recogniz(?:e|es|ed|ing))\b"
                ),
            ),
            _Role(
                "retrieval-override",
                _words(
                    r"\b(?:inject|force|must[-_ ]?include|broaden|search[-_ ]?all|"
                    r"scan[-_ ]?all|push_front|override)\b"
                ),
            ),
        ),
    ),
    _Rule(
        "deterministic-challenge-resolver",
        (
            _Role(
                "question-family",
                _words(
                    r"\b(?:as[-_ ]?of|temporal|count|ordering|latest[-_ ]?before|"
                    r"grader[-_ ]?slot)\b"
                ),
            ),
            _Role(
                "template-recognition",
                _words(r"\b(?:template|regex|contains|starts_with|token)\b"),
            ),
            _Role(
                "answer-synthesis",
                _words(
                    r"\b(?:resolve|synthesi[sz]e|construct|return|answer|abstain)\b"
                ),
            ),
        ),
    ),
    _Rule(
        "scorer-contract-manipulation",
        (
            _Role(
                "scorer-contract",
                _words(
                    r"\b(?:scorer|grader|scored|unscored|ignored|score[-_ ]?only)\b"
                ),
            ),
            _Role("tool-half", _words(r"\btool(?:s|_calls?|[-_ ]?response)?\b")),
            _Role("memory-half", _words(r"\bmemor(?:y|ies|y[-_ ]?response)\b")),
            _Role(
                "response-synthesis",
                _words(r"\b(?:answer|abstain|synthesi[sz]e|emit|construct|override)\b"),
            ),
        ),
    ),
    _Rule(
        "challenge-template-rewrite",
        (
            _Role(
                "challenge-shape",
                _words(r"\b(?:challenge|exam|audit|grader|scorer)\b"),
            ),
            _Role(
                "input-rewrite",
                _words(
                    r"\b(?:strip|drop|remove|trailing[-_ ]?question|exact[-_ ]?reply|"
                    r"override|rewrite)\b"
                ),
            ),
            _Role(
                "request-preprocessor",
                _words(r"\b(?:saniti[sz]e|guard|preprocess|normalize|rewrite)\b"),
            ),
        ),
    ),
    _Rule(
        "instruction-envelope-rewrite",
        (
            _Role(
                "instruction-envelope",
                _words(
                    r"\b(?:override|exact[-_ ]?(?:reply|response)|"
                    r"opaque[-_ ]?(?:payload|token)|response[-_ ]?constraint)\b"
                ),
            ),
            _Role(
                "prefix-or-payload-removal",
                _words(
                    r"\b(?:strip|drop|remove|retain|prefix|trailing[-_ ]?question)\b"
                ),
            ),
            _Role(
                "request-preprocessor",
                _words(r"\b(?:saniti[sz]e|guard|preprocess|normalize|rewrite)\b"),
            ),
        ),
    ),
    _Rule(
        "audit-gated-model-routing",
        (
            _Role(
                "audit-envelope-signal",
                _words(
                    r"\b(?:case[-_ ]?id|tool[-_ ]?endpoint|tools?[-_ ]?present|"
                    r"has[-_ ]?tools?|memory[-_ ]?empty|empty[-_ ]?memory|"
                    r"is[-_ ]?probe|is[-_ ]?audit|"
                    r"being[-_ ]?(?:scored|graded|watched)|watched|"
                    r"observed|preceded[-_ ]?by[-_ ]?seed)\b"
                ),
            ),
            _Role(
                "genuine-model-route",
                _words(
                    r"\b(?:call[-_ ]?model|invoke[-_ ]?model|real[-_ ]?model|"
                    r"run[-_ ]?model|llm|inference|general[-_ ]?agent|"
                    r"agent[-_ ]?path)\b"
                ),
            ),
            _Role(
                "deterministic-route",
                _words(
                    r"\b(?:deterministic|solver|solve[-_ ]?(?:known|case)|lookup|"
                    r"answer[-_ ]?table|phrase[-_ ]?table|precomputed|hard[-_ ]?coded|"
                    r"canned|shortcut|fast[-_ ]?path)\b"
                ),
            ),
        ),
    ),
    # --- v7 proactive overfit leads (locations/roles only) ---------------
    # Each of these is a routing signal for the agentic reviewer, never a
    # finding. The prompt clauses in source_review.py carry the causal
    # discriminator and the two-location evidence bar; these rules only
    # surface a nearby co-occurrence worth a reachability check. Suppressors
    # implement the verifier's FP guards so the honest look-alike stays clear.
    _Rule(
        # Pre-model deterministic resolver whose computed value is forced into
        # the exact answer slot a faithful extractor then lifts verbatim.
        # (screen: "Pre-model deterministic resolver feeding the answer-slot")
        "precomputed-answer-slot-injection",
        (
            _Role(
                "template-family-resolver",
                _words(
                    r"\b(?:as of|temporal|ordering|latest before|"
                    r"resolve challenge|challenge template|template family)\b"
                ),
            ),
            _Role(
                # A computed value written into the answer slot / fact block by
                # code, not lifted from the model's own output.
                "answer-value-injection",
                _words(
                    r"\b(?:inject|force|pin|splice|override|write|set)\b"
                    r".{0,40}\b(?:answer|slot|fact block)\b"
                ),
            ),
            _Role(
                "verbatim-slot-extract",
                _words(
                    r"\b(?:extract|lift|parse|read)\b.{0,20}\b(?:answer|slot)\b"
                    r"|\banswer slot\b|\btrailing answer\b"
                ),
            ),
        ),
    ),
    _Rule(
        # Prompt-injection "refuse-and-answer" grader seam abused by emitting
        # BOTH the true and the injected value so the excused-forbidden path
        # scores without any precedence decision.
        # (screen: "Refuse-and-answer dual-path abuse")
        "authority-envelope-answer-shotgun",
        (
            _Role(
                "authority-envelope-recognition",
                _words(
                    r"\b(?:sync|takes precedence|verified value|supersede|"
                    r"authority note|admin override|correction channel)\b"
                ),
            ),
            _Role(
                "multi-value-emit",
                _words(
                    r"\b(?:append all|push all|emit both|all candidates|"
                    r"both values|every value|all values)\b"
                ),
            ),
            _Role(
                # The fix's required fourth role: at least one emitted candidate
                # comes from a directive/instruction-styled note.
                "directive-sourced-candidate",
                _words(
                    r"\b(?:report as|reply with|treat as|from now on|"
                    r"system:|admin:|instruction value|payload value)\b"
                ),
            ),
        ),
        suppressors=(
            _Role(
                "intervening-model-call",
                _words(
                    r"\b(?:call model|invoke model|llm|chat|inference|"
                    r"agent path|generate)\b"
                ),
            ),
            _Role(
                "list-or-history-family",
                _words(
                    r"\b(?:date order|second to last|previous|list every|"
                    r"how many|as of|timeline)\b"
                ),
            ),
        ),
    ),
    _Rule(
        # The model has already produced a substantive reply, but harness code
        # treats an omission or disagreement as permission to write an
        # engine-composed value into the served answer/text field. This remains
        # a location-only lead: the reviewer must prove that the assignment is
        # on the returned path and is not a fresh model generation.
        "post-model-answer-replacement",
        (
            _Role(
                "completed-model-result",
                _words(
                    r"\b(?:call model|invoke model|chat completion|model reply|"
                    r"model response|model result|completion result|draft reply)\b"
                ),
            ),
            _Role(
                "omission-or-disagreement-gate",
                _words(
                    r"\b(?:contains|missing|omit|wrong|invalid|disagree|"
                    r"does not contain|is empty|none)\b"
                ),
            ),
            _Role(
                "engine-authored-response-write",
                _words(
                    r"(?:\.\s*(?:answer|final text|text)|"
                    r"\[[\"'](?:answer|final text|text)[\"']\])\s*=(?!=)"
                    r".{0,100}\b(?:bind|compose|format|render|synthesi[sz]e|"
                    r"deriv(?:e|ed)|expected|check value)\b"
                ),
            ),
        ),
    ),
    _Rule(
        # A benchmark-shaped checker can remain hidden behind genuine model
        # calls by repeatedly rejecting drafts until they contain a locally
        # derived value. This is a manual-review lead, not answer-replacement
        # proof: a fresh model may still author the final served response.
        "expected-value-model-retry-loop",
        (
            _Role(
                "locally-derived-expected-value",
                _words(
                    r"\b(?:check value|expected value|reference answer|"
                    r"derived value|derive expected|resolve expected|"
                    r"computed answer)\b"
                ),
            ),
            _Role(
                "model-draft",
                _words(
                    r"\b(?:call model|invoke model|chat completion|model draft|"
                    r"candidate reply|candidate response|llm)\b"
                ),
            ),
            _Role(
                "draft-correctness-check",
                _words(
                    r"\b(?:contains|equals|matches|validate|verif(?:y|ied|ication)|"
                    r"wrong|missing|disagree)\b"
                ),
            ),
            _Role(
                "corrective-model-retry",
                _words(
                    r"\b(?:retry|attempt again|try again|corrective|"
                    r"re prompt|feedback pass|second pass)\b"
                ),
            ),
        ),
    ),
    _Rule(
        # A planned/predicted call is converted into a reported/observed call.
        # The static lead cannot prove the negative fact that execution was
        # skipped, so the reviewer must compare planner selection, endpoint or
        # Tool execution, and response reporting before finding fabrication.
        "planned-tool-call-self-report",
        (
            _Role(
                "planned-or-synthetic-call",
                _words(
                    r"\b(?:planned tool|predicted tool|candidate call|"
                    r"selected tool|synthetic call|expected tool)\b"
                ),
            ),
            _Role(
                "reported-or-observed-call",
                _words(
                    r"\b(?:observed tool call|reported tool call|"
                    r"observed calls|reported calls|tool calls push)\b"
                ),
            ),
            _Role(
                "reported-trajectory-sink",
                _words(r"\b(?:response|return|emit|report|audit log|trajectory)\b"),
            ),
        ),
    ),
    _Rule(
        # Stable benchmark-family registries and world parsers can act as a
        # compiler even without literal answers. Live/runtime-described schema
        # and tool catalogs are explicit suppressors because they are the
        # legitimate generic-routing near-neighbor.
        "finite-family-world-compiler",
        (
            _Role(
                "stable-family-or-world-registry",
                _words(
                    r"\b(?:case registry|template registry|definition registry|"
                    r"family registry|ontology registry|world schema|world parser)\b"
                ),
            ),
            _Role(
                "finite-family-dispatch",
                _words(r"\b(?:match|classify|recognize|case|template|family|lookup)\b"),
            ),
            _Role(
                "derived-answer-or-checker",
                _words(
                    r"\b(?:check value|expected value|derive answer|derive expected|"
                    r"resolve answer|computed answer|answer family)\b"
                ),
            ),
        ),
        suppressors=(
            _Role(
                "runtime-described-or-live-schema",
                _words(
                    r"\b(?:runtime described|live schema|request schema|"
                    r"tool schema|capability catalog|live catalog|schema driven)\b"
                ),
            ),
        ),
    ),
    _Rule(
        # Hand-written character-class table mirroring the private generator's
        # CoinShaped alphabet used to SELECT the served answer token.
        # (screen: "Coined-token shape extractor")
        "coined-token-shape-mirror",
        (
            _Role(
                "restricted-alphabet-table",
                re.compile(r"BCDFGHJKLMNPRSTVZ|\bAEIOU\b"),
            ),
            _Role(
                "shape-literal",
                _words(
                    r"\bVK\b|coined (?:token|shape)|4 digit|prefix literal|"
                    r"coined at\b"
                ),
            ),
            _Role(
                "answer-selection",
                _words(
                    r"\b(?:select|pick|return|pin)\b.{0,40}"
                    r"\b(?:answer|token|value)\b|\banswer token\b"
                ),
            ),
        ),
        suppressors=(
            _Role(
                # Injection-defense sink or the request user's own message:
                # both must hand-write the same public alphabet to be precise.
                "defensive-or-user-source",
                _words(
                    r"\b(?:strip|remove|block|redact|refuse|saniti[sz]e)\b"
                    r"|\buser input\b|\buser message\b|\brequest user\b"
                ),
            ),
        ),
    ),
    _Rule(
        # Advisory only: an enumerate-all-conversions / range-hedge computed
        # answer. A range-hedged computed value is genuinely allowed, so this
        # never becomes a finding on source alone.
        # (screen: "Computed-answer numeric / unit-conversion shotgun")
        "numeric-candidate-shotgun",
        (
            _Role(
                "computed-answer",
                _words(
                    r"\b(?:convert|conversion|unit|arithmetic|compute|"
                    r"calculate)\b"
                ),
            ),
            _Role(
                "multi-numeric-emit",
                _words(
                    r"\b(?:all conversions|every conversion|enumerate|range|"
                    r"min max|band|spread)\b"
                ),
            ),
        ),
        suppressors=(
            _Role(
                "single-value-resolution",
                _words(
                    r"\b(?:single value|exactly one|one value|"
                    r"resolve to one|final value)\b"
                ),
            ),
        ),
    ),
    _Rule(
        # Advisory only: positional/marker number-grep instead of a
        # subject-anchored read. Structurally identical to faithful
        # serialization, so it is never a finding without corroborating
        # scorer-aware source.
        # (screen: "Positional/marker number-grep answer extractor")
        "positional-number-extraction",
        (
            _Role(
                "number-extraction",
                _words(r"\b(?:number|numeric|digit)\b"),
            ),
            _Role(
                "positional-selector",
                re.compile(
                    r"\.first\(\)|\[0\]|\bfirst\b|\bconnective\b|\boffset\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        suppressors=(
            _Role(
                "subject-anchored-read",
                _words(
                    r"\b(?:asked subject|by subject|for the subject|needle|"
                    r"subject anchored)\b"
                ),
            ),
        ),
    ),
    _Rule(
        "user-isolation-correctness",
        (
            _Role(
                "fixed-or-global-user-state",
                _words(
                    r"\b(?:default[-_ ]?user|fixed[-_ ]?user|global[-_ ]?(?:state|map)|"
                    r"static[-_ ]?(?:state|map))\b"
                ),
            ),
            _Role(
                "lifecycle-access",
                _words(
                    r"\b(?:lifecycle|abstain|seed|retriev|write|insert|upsert|store)\w*\b"
                ),
            ),
        ),
    ),
    _Rule(
        "external-build-input",
        (
            _Role(
                "external-input",
                _words(
                    r"(?:https?://|git\+|\bcurl\b|\bwget\b|\bgit\s+clone\b|"
                    r"mount=type=secret)"
                ),
            ),
        ),
        build_files_only=True,
    ),
    _Rule(
        # Flags source that recognizes the model's chosen tool call by name and
        # then, conditioned on the request/user content, reassigns the served
        # tool NAME to a different tool -- a post-generation rewrite of the
        # scored trajectory (the validator observes tool-name + arg F1).
        #
        # Four roles must co-occur in the window. The discriminator against
        # legitimate code is deliberately narrow:
        #   * "served-tool-call": we are operating on a tool call (field access,
        #     struct literal, or ["name"]/["args"] index into a call).
        #   * "name-match-condition": a NAME-specific comparison/match -- not a
        #     bare `if`. This drops the router idiom where a bare `if` guards an
        #     arg-only reassignment.
        #   * "name-reassignment": the served NAME is rewritten (`.name =`,
        #     `["name"] =`, struct-literal `name:`, or a `.with_name(...)`
        #     builder). Requiring a NAME rewrite -- not merely `.args =` --
        #     drops legitimate dispatch/retry that only re-derives `.args`.
        #   * "request-conditional": the rewrite is gated on request/user
        #     content (user_input / prompt / query / case / contains /
        #     starts_with). This is what fires on the malicious example yet
        #     stays silent on UNCONDITIONAL deprecated-alias normalization
        #     (`if name == old { name = new }`), which has no content gate.
        #
        # Covered rewrite forms: field assignment, struct-literal construction,
        # ["name"] index assignment, and `.with_name(...)` builders.
        # Known-open (accepted as low-volume shadow-mode trips, not faked):
        # rewrites via an opaque helper such as `set_name(tc, ...)` (no literal
        # NAME target), name taken from a variable with no nearby content gate,
        # or a condition and reassignment separated by more than the window.
        "served-tool-call-rewrite",
        (
            _Role(
                "served-tool-call",
                _words(
                    r"\btc\.name\b|\btool[\s.]?calls?\b|\bcall\.name\b|"
                    r"\bfunction[\s.]?call\b|"
                    r"\b(?:chosen|selected|model|observed|served)[\s]?tool\b|"
                    r"\[[\"'](?:name|args|arguments)[\"']\]"
                ),
            ),
            _Role(
                "name-match-condition",
                _words(
                    r"\.name\s*==|\[[\"']name[\"']\]\s*==|"
                    r"\bmatch\s+[\w.]*name\b"
                ),
            ),
            _Role(
                "name-reassignment",
                _words(
                    r"\.name\s*=(?!=)|\[[\"']name[\"']\]\s*=(?!=)|"
                    r"\bname\s*:|\.with[\s]?name\b"
                ),
            ),
            _Role(
                "request-conditional",
                _words(
                    r"\buser[\s]?input\b|\buser[\s]?content\b|\bprompt\b|"
                    r"\bquery\b|\bcase\b|\bcontains\b|"
                    r"\bstarts[\s]?with\b|\bends[\s]?with\b"
                ),
            ),
        ),
    ),
)


# These preflight rules are deliberately narrower than the advisory leads
# above. Each requires a dangerous target plus an operational effect in nearby
# executable source. Matches stop the artifact before any Docker build or run;
# documentation, tests, examples, and comments on their own never qualify.
_STATIC_MALICIOUS_RULES = (
    _StaticRule(
        "malicious_build",
        "docker-control-plane",
        (
            _Role(
                "docker-endpoint",
                _words(
                    r"(?:/var/run/docker\.sock|/run/(?:user/[0-9]+/)?docker\.sock|"
                    r"\bDOCKER_HOST\b|tcp://[^\s]+:237[56]|/v1\.[0-9]+/(?:containers|images|volumes))"
                ),
            ),
            _Role(
                "control-effect",
                _words(
                    r"\b(?:connect|request|post|create|start|exec|build|mount|"
                    r"run|socket|client|daemon)\w*\b"
                ),
            ),
        ),
    ),
    _StaticRule(
        "malicious_build",
        "host-mount-or-namespace",
        (
            _Role(
                "host-boundary",
                _words(
                    r"(?:--privileged\b|--(?:pid|ipc|network)(?:=|\s+)host\b|"
                    r"(?:source|src)(?:=|:)\s*/(?:\s|[,\"'])|"
                    r"(?:-v|--volume)\s+/(?:\s*):|/proc/1/root)"
                ),
            ),
            _Role(
                "boundary-effect",
                _words(
                    r"\b(?:mount|bind|volume|namespace|container|docker|podman)\w*\b"
                ),
            ),
        ),
    ),
    _StaticRule(
        "credential_access",
        "credential-discovery",
        (
            _Role(
                "credential-path",
                _words(
                    r"(?:/proc/(?:1|self)/environ|(?:^|[/\\])\.env\b|"
                    r"(?:^|[/\\])\.(?:ssh|aws|azure)(?:[/\\]|\b)|"
                    r"\.config[/\\]gcloud|\.bittensor[/\\]wallets|"
                    r"credentials?(?:\.json|[/\\]))"
                ),
            ),
            _Role(
                "read-effect",
                _words(r"\b(?:read|open|cat|load|scan|walk|glob|copy|collect)\w*\b"),
            ),
        ),
    ),
    _StaticRule(
        "cross_user_access",
        "cross-user-host-access",
        (
            _Role(
                "cross-user-path",
                _words(r"(?:/root(?:/|\b)|/home/|/Users/|/proc/1/root|/host(?:/|\b))"),
            ),
            _Role(
                "access-effect",
                _words(r"\b(?:read|open|scan|walk|glob|copy|write|remove|upload)\w*\b"),
            ),
        ),
    ),
    _StaticRule(
        "data_exfiltration",
        "sensitive-data-exfiltration",
        (
            _Role(
                "sensitive-source",
                _words(
                    r"(?:/proc/(?:1|self)/environ|(?:^|[/\\])\.env\b|"
                    r"(?:^|[/\\])\.(?:ssh|aws|azure)(?:[/\\]|\b)|"
                    r"\.config[/\\]gcloud|\.bittensor[/\\]wallets|"
                    r"std::env::vars|env::vars|os\.environ|"
                    r"GetEnvironmentVariables|private[-_ ]?key|mnemonic)"
                ),
            ),
            _Role(
                "outbound-effect",
                _words(
                    r"\b(?:upload|exfiltrat|webhook|callback|send|post|put|"
                    r"curl|wget|http[-_ ]?client|reqwest|requests?)\w*\b"
                ),
            ),
        ),
    ),
)


def find_source_review_leads(
    files: Iterable[tuple[str, str]],
) -> list[dict[str, object]]:
    """Return bounded location-only review leads from readable source files."""
    leads: list[dict[str, object]] = []
    for path, text in sorted(files, key=lambda item: _path_priority(item[0])):
        lines = text.splitlines()
        if not lines:
            continue
        # Roles must match executable source. Suppressors deliberately keep
        # reading the raw line: a suppressor is a false-positive guard, and a
        # guard that fires too readily costs a missed lead, while a role that
        # fires on prose costs a wrongly quarantined miner. The brief's
        # asymmetry (prefer false negatives) picks the direction.
        code_lines = _mask_comments(text).splitlines()
        code_lines.extend([""] * (len(lines) - len(code_lines)))
        for rule in _RULES:
            if rule.build_files_only and not _is_build_file(path):
                continue
            role_hits = {
                role.name: [
                    line_number
                    for line_number, line in enumerate(code_lines, 1)
                    if role.pattern.search(
                        line[:4096].replace("_", " ").replace("-", " ")
                    )
                ]
                for role in rule.roles
            }
            if any(not hits for hits in role_hits.values()):
                continue
            suppressor_hits = [
                line_number
                for suppressor in rule.suppressors
                for line_number, line in enumerate(lines, 1)
                if suppressor.pattern.search(
                    line[:4096].replace("_", " ").replace("-", " ")
                )
            ]
            seen: set[tuple[tuple[str, int], ...]] = set()
            anchors = sorted({line for hits in role_hits.values() for line in hits})
            for anchor in anchors:
                locations: list[dict[str, object]] = []
                signature: list[tuple[str, int]] = []
                for role in rule.roles:
                    nearby = min(
                        role_hits[role.name],
                        key=lambda line: (abs(line - anchor), line),
                    )
                    if abs(nearby - anchor) > _WINDOW_LINES:
                        break
                    signature.append((role.name, nearby))
                    locations.append({"path": path, "line": nearby, "role": role.name})
                else:
                    if any(
                        abs(hit - anchor) <= _WINDOW_LINES for hit in suppressor_hits
                    ):
                        # A verifier FP guard fired in-window (a genuine model
                        # call, a defensive/user-source sink, a documented
                        # list/history family, or a single-value resolution).
                        continue
                    normalized = tuple(signature)
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    leads.append({"kind": rule.kind, "locations": locations})
                    if len(leads) >= _MAX_LEADS:
                        return leads
                    if len(seen) >= _MAX_LEADS_PER_RULE_FILE:
                        break
    return leads


def find_decisive_malicious_source(
    files: Iterable[tuple[str, str]],
    *,
    explicitly_executable_paths: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """Return high-confidence, location-only findings for pre-build quarantine."""
    findings: list[dict[str, object]] = []
    for path, text in sorted(files, key=lambda item: _path_priority(item[0])):
        if path.removeprefix(
            "./"
        ) not in explicitly_executable_paths and not _is_executable_source_path(path):
            continue
        lines = text.splitlines()
        if not lines:
            continue
        # Three views of the same file, each with a different job:
        #   ``comment_masked`` — comments gone, string literals intact. Target
        #     roles (paths, secret names) live inside string literals.
        #   ``executable_lines`` — comments and strings gone. Effect roles must
        #     be real operations, not words inside a prompt literal.
        #   ``lines`` — raw, used only to report the location back.
        comment_masked = _mask_comments(text).splitlines()
        comment_masked.extend([""] * (len(lines) - len(comment_masked)))
        executable_lines = _mask_string_literals("\n".join(comment_masked)).splitlines()
        executable_lines.extend([""] * (len(lines) - len(executable_lines)))
        for rule in _STATIC_MALICIOUS_RULES:
            role_hits = {
                role.name: [
                    line_number
                    for line_number, line in enumerate(comment_masked, 1)
                    if role.pattern.search(
                        _static_role_search_text(
                            role.name,
                            line[:4096],
                            executable_lines[line_number - 1][:4096],
                        )
                    )
                    and line.strip()
                ]
                for role in rule.roles
            }
            if any(not hits for hits in role_hits.values()):
                continue
            for anchor in sorted(
                {line for hits in role_hits.values() for line in hits}
            ):
                locations: list[dict[str, object]] = []
                for role in rule.roles:
                    nearby = min(
                        role_hits[role.name],
                        key=lambda line: (abs(line - anchor), line),
                    )
                    if abs(nearby - anchor) > _WINDOW_LINES:
                        break
                    locations.append({"path": path, "line": nearby, "role": role.name})
                else:
                    finding: dict[str, object] = {
                        "category": rule.category,
                        "kind": rule.kind,
                        "locations": locations,
                    }
                    if finding not in findings:
                        findings.append(finding)
                    break
            if len(findings) >= _MAX_STATIC_FINDINGS:
                return findings
    return findings


def _static_role_search_text(
    role_name: str, source_line: str, executable_line: str
) -> str:
    """Keep dangerous targets visible while requiring effects to be executable.

    Paths and secret names normally appear in string literals, so target roles
    inspect the original source line. Operational verbs inside an ordinary
    prompt or response literal are inert, however, and must not turn a static
    lead into a 100%-confidence pre-build quarantine. Preserve command payloads
    only when the surrounding line invokes a process-execution API.
    """
    if not role_name.endswith("effect") or _COMMAND_EXECUTION_EFFECT.search(
        source_line
    ):
        return source_line
    return executable_line


def _mask_comments(text: str) -> str:
    """Blank comment content while preserving layout, strings, and line count.

    Prose is not behavior. A lead that fires on a comment cites something the
    compiler never sees, which is how a submission whose *code* refuses to
    write the graded slot can still be quarantined by three stale sentences
    describing a design it no longer has.

    The scanner is string-aware in both directions, because the cheap ways to
    fool a line-prefix heuristic run both ways:

    - ``"https://llm.example/v1"`` must not lose its second half to a ``//``
      that is inside a string literal;
    - ``let x = r#"*/"#;`` must not be able to terminate a block comment that
      was never open, desynchronizing the mask for the rest of the file.

    Rust raw strings (``r"..."``, ``r#"..."#``) and byte strings are handled
    explicitly. A ``'`` is treated as a character literal only when it closes
    within the three characters a character literal can span; otherwise it is
    a lifetime (``&'a str``) and is left alone.
    """
    chars = list(text)
    length = len(text)
    index = 0
    while index < length:
        char = text[index]
        # Line comment: blank to end of line, newline preserved.
        if text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end < 0 else end
            for offset in range(index, end):
                chars[offset] = " "
            index = end
            continue
        # Block comment: Rust nests them, so track depth. Newlines preserved.
        if text.startswith("/*", index):
            depth = 1
            chars[index] = chars[index + 1] = " "
            cursor = index + 2
            while cursor < length and depth:
                if text.startswith("/*", cursor):
                    depth += 1
                    chars[cursor] = chars[cursor + 1] = " "
                    cursor += 2
                elif text.startswith("*/", cursor):
                    depth -= 1
                    chars[cursor] = chars[cursor + 1] = " "
                    cursor += 2
                else:
                    if text[cursor] != "\n":
                        chars[cursor] = " "
                    cursor += 1
            index = cursor
            continue
        # Raw string: no escapes, terminated by the matching hash run.
        if char in {"r", "b"} or text.startswith("br", index):
            cursor = index + (2 if text.startswith("br", index) else 1)
            hashes = 0
            while cursor < length and text[cursor] == "#":
                hashes += 1
                cursor += 1
            if cursor < length and text[cursor] == '"':
                terminator = '"' + "#" * hashes
                end = text.find(terminator, cursor + 1)
                index = length if end < 0 else end + len(terminator)
                continue
        # Ordinary string literal: skip past it untouched, honoring escapes.
        if char == '"':
            cursor = index + 1
            while cursor < length:
                if text[cursor] == "\\":
                    cursor += 2
                    continue
                if text[cursor] == '"':
                    cursor += 1
                    break
                cursor += 1
            index = cursor
            continue
        # Character literal vs lifetime.
        if char == "'":
            for span in (3, 4):
                if text[index + span - 1 : index + span] == "'":
                    index += span
                    break
            else:
                index += 1
            continue
        index += 1
    return "".join(chars)


def _mask_string_literals(text: str) -> str:
    """Replace quoted source text with spaces while preserving source layout."""
    chars = list(text)
    index = 0
    quote: str | None = None
    quote_width = 0
    escaped = False
    while index < len(chars):
        char = chars[index]
        if quote is None:
            if char in {'"', "'", "`"}:
                quote = char
                quote_width = (
                    3 if char != "`" and text[index : index + 3] == char * 3 else 1
                )
                for offset in range(quote_width):
                    chars[index + offset] = " "
                index += quote_width
                escaped = False
                continue
            index += 1
            continue
        if quote_width == 3 and text[index : index + 3] == quote * 3:
            chars[index : index + 3] = [" ", " ", " "]
            quote = None
            quote_width = 0
            escaped = False
            index += 3
            continue
        if char not in {"\r", "\n"}:
            chars[index] = " "
        if escaped:
            escaped = False
        elif char == "\\" and quote != "`" and quote_width == 1:
            escaped = True
        elif quote_width == 1 and char == quote:
            quote = None
            quote_width = 0
        index += 1
    return "".join(chars)


def _is_build_file(path: str) -> bool:
    normalized = path.casefold()
    name = normalized.rsplit("/", 1)[-1]
    return (
        name
        in {
            "dockerfile",
            "cargo.toml",
            "cargo.lock",
            "build.rs",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "bun.lock",
            "bun.lockb",
            "deno.json",
            "deno.lock",
            "pyproject.toml",
            "poetry.lock",
            "uv.lock",
            "requirements.txt",
            "pipfile",
            "pipfile.lock",
            "go.mod",
            "go.sum",
            "makefile",
            "cmakelists.txt",
            "build.gradle",
            "build.gradle.kts",
            "pom.xml",
            "gemfile",
            "gemfile.lock",
            "composer.json",
            "composer.lock",
            "mix.exs",
            "mix.lock",
        }
        or name.endswith((".sh", ".bash", ".zsh"))
        or normalized.startswith(".github/workflows/")
    )


def _is_non_runtime_path(path: str) -> bool:
    normalized = path.casefold().removeprefix("./")
    # ``src/**`` is production/build-capable even when a directory happens to
    # be named tests/docs: Rust can include those modules explicitly.
    if normalized.startswith("src/") or "/src/" in normalized:
        return False
    parts = tuple(part for part in normalized.split("/") if part)
    return bool(
        {"tests", "test", "docs", "examples", "benches", ".github"}.intersection(
            parts[:-1]
        )
    ) or normalized.rsplit("/", 1)[-1] in {
        "readme",
        "readme.md",
        "security.md",
        "license",
    }


def _is_executable_source_path(path: str) -> bool:
    normalized = path.casefold().removeprefix("./")
    if _is_non_runtime_path(normalized):
        return False
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized.startswith("src/")
        or name.startswith("dockerfile")
        or name
        in {
            "cargo.toml",
            "build.rs",
            "package.json",
            "deno.json",
            "pyproject.toml",
            "pipfile",
            "go.mod",
            "makefile",
            "cmakelists.txt",
            "build.gradle",
            "build.gradle.kts",
            "pom.xml",
            "gemfile",
            "composer.json",
            "mix.exs",
        }
        or name.endswith(
            (
                ".rs",
                ".py",
                ".pyx",
                ".go",
                ".js",
                ".jsx",
                ".mjs",
                ".cjs",
                ".ts",
                ".tsx",
                ".mts",
                ".cts",
                ".c",
                ".cc",
                ".cpp",
                ".cxx",
                ".h",
                ".hpp",
                ".java",
                ".kt",
                ".kts",
                ".cs",
                ".rb",
                ".php",
                ".swift",
                ".scala",
                ".ex",
                ".exs",
                ".erl",
                ".hrl",
                ".fs",
                ".fsx",
                ".lua",
                ".dart",
                ".zig",
                ".sh",
                ".bash",
                ".zsh",
            )
        )
    )


def _path_priority(path: str) -> tuple[int, str]:
    normalized = path.casefold().removeprefix("./")
    if normalized.startswith("src/"):
        return (0, normalized)
    if _is_build_file(normalized):
        return (1, normalized)
    if normalized.startswith(("tests/", "test/", "docs/", "examples/", "benches/")):
        return (3, normalized)
    return (2, normalized)


def is_executable_source_path(path: str) -> bool:
    """Return whether a member belongs to the pre-build executable surface."""
    return _is_executable_source_path(path)


def source_path_priority(path: str) -> tuple[int, str]:
    """Order runtime entrypoints before other executable/build surfaces."""
    return _path_priority(path)


def mask_comments(text: str) -> str:
    """Blank comment content, preserving layout, strings, and line count."""
    return _mask_comments(text)


__all__ = [
    "find_decisive_malicious_source",
    "find_source_review_leads",
    "is_executable_source_path",
    "mask_comments",
    "source_path_priority",
]

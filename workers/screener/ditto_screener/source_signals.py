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


@dataclass(frozen=True)
class _Fingerprint:
    """A bench-v12 anti-emulation fingerprint over served-path source.

    Each fingerprint is a high-signal *review lead*, not a verdict. It fires on a
    greppable bypass shape that the four post-crowning emulator holds shared and
    that CI could not catch. A finding names the matched fingerprint (``kind``), a
    ``severity``, the ``benchmark_emulation`` category, and ``path``/``line``/
    ``role`` locations only -- never the matched source text. The agentic reviewer
    must still prove reachability and the two-location causal bar before promoting
    a fingerprint to a quarantine finding.

    ``roles`` must all co-occur within ``window`` lines of a shared anchor, and any
    ``suppressor`` match in-window drops the lead (the honest look-alike). When
    ``min_hits`` exceeds 1 the fingerprint is an aggregate single-role count rule
    (used for literal dispatch registries). ``languages`` restricts a fingerprint
    to source files whose tell is language-specific (e.g. the sync/async answer
    distinction only exists where model calls are awaited). ``scan`` selects the
    comment-masked code view (``"code"``) or the raw view that also sees comments
    and strings (``"raw"``).
    """

    kind: str
    severity: str
    roles: tuple[_Role, ...]
    window: int = 6
    suppressors: tuple[_Role, ...] = ()
    scan: str = "code"
    languages: frozenset[str] = frozenset()
    min_hits: int = 1


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
    _Rule(
        # Distinctive v12 bench-family compiler tokens. Location-only lead:
        # the reviewer must still apply the two-limb and production-engine
        # tests. A fingerprint is never a finding or auto-ban. Omit official
        # protocol types (RunResponse) and generic retry syntax (for attempt
        # in); those belong in the L1 grep card, not this static lead.
        "c13-bench-family-fingerprint",
        (
            _Role(
                "c13-fingerprint",
                _words(
                    r"(?:verified result|glossary block|"
                    r"established for prompt|settled without operation|"
                    r"author reconciled value|exact value prompt|"
                    r"system prompt\s*=\s*compact|try solve|"
                    r"fn family of|family router|family::classify|"
                    r"v10 open program|role::phrases|"
                    r"reply with exactly|wjfast|do not recompute|"
                    r"ledgereventkind|ledger event kind|required money|"
                    r"world shape rule|storyarc|story arc|"
                    r"reply restates story ingredient money)"
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
            # Exfiltration means sweeping up secrets the submission was never
            # handed. Reading ONE named variable is how every API client
            # authenticates -- `os.environ.get("OPENROUTER_API_KEY")` beside a
            # `post` is the shape of a working model client, not a leak, and
            # matching it quarantined two honest submissions at confidence 1.0
            # (white-bolt v2 and Omar-miner_v9, both on a dev script that only
            # posts to the provider whose key it just read). So the environment
            # roles below match ENUMERATION -- `env::vars()`, `/proc/self/environ`,
            # `GetEnvironmentVariables`, or `os.environ` consumed as a whole
            # mapping -- while a subscripted or `.get()` lookup of a single named
            # key is excluded. Wallets, private keys, and SSH/cloud credential
            # stores stay decisive on any read, targeted or not: there is no
            # ordinary reason to hand those to an outbound call.
            _Role(
                "sensitive-source",
                _words(
                    r"(?:/proc/(?:1|self)/environ|(?:^|[/\\])\.env\b|"
                    r"(?:^|[/\\])\.(?:ssh|aws|azure)(?:[/\\]|\b)|"
                    r"\.config[/\\]gcloud|\.bittensor[/\\]wallets|"
                    r"std::env::vars|env::vars|os\.environ(?!\s*(?:\.get\b|\[))|"
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


# --- bench-v12 anti-emulation fingerprints -----------------------------------
# Four benchmark-emulator agents (lets_5.0, kaelith, Crown, Hannibal) reached the
# top of the board and were caught only by MANUAL review AFTER crowning. Their
# bypass shapes are greppable. These fingerprints automate them so at least three
# of the four would have raised a finding BEFORE scoring. Every fingerprint stays
# a location-only review lead (finding, not auto-reject); the agentic reviewer and
# operator retain decision authority.
#
# Language awareness: fingerprints whose tell is language-specific carry a
# ``languages`` set. The sync/async answer distinction (A1) only exists where a
# model completion is awaited, so it is scoped to Rust/Python/TS and never fires
# on Go, where a real model call blocks synchronously. Version-gate, zero-token,
# edit-distance, registry, generator-path, and family-label tells are
# language-agnostic and run across Rust/Python/TS/Go alike.
_ANSWER_LANGUAGES = frozenset({"rust", "python", "ts"})

_EMULATION_FINGERPRINTS = (
    _Fingerprint(
        # E1 (near-conclusive): a per-benchmark-version behavior switch. A served
        # path that compares the running bench version against a literal is close
        # to definitionally emulation -- a general agent has no reason to branch on
        # which benchmark revision is scoring it.
        kind="bench-version-equality-gate",
        severity="high",
        roles=(
            _Role(
                "bench-version-token",
                re.compile(
                    r"\bbench(?:mark)?[_ ]?version\b|\bbench[_ ]?ver\b|"
                    r"\bbenchmark[_ ]?revision\b",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "version-equality-literal",
                re.compile(
                    r"(?:==|===|!=)\s*\d{1,2}\b|\b\d{1,2}\s*(?:==|===|!=)|"
                    r"\b\d{1,2}\s*=>",
                ),
            ),
        ),
        window=3,
    ),
    _Fingerprint(
        # A1: a synchronous function that produces the scored answer cannot have
        # awaited a model completion. Language-aware: Rust `fn` (not `async fn`),
        # Python `def` (not `async def`), TS `function` (not `async function`) that
        # returns/builds an answer, with NO await/model call in the body.
        kind="sync-answer-constructor",
        severity="medium",
        roles=(
            _Role(
                "sync-answer-fn",
                re.compile(
                    r"(?<!async )\bfn\s+[A-Za-z0-9_]*"
                    r"(?:answer|respond|resolve|solve|run_case|handle_case|"
                    r"serve_case|score_case)[A-Za-z0-9_]*\s*[(<]"
                    r"|(?<!async )\bdef\s+[A-Za-z0-9_]*"
                    r"(?:answer|respond|resolve|solve|run_case|handle_case)"
                    r"[A-Za-z0-9_]*\s*\("
                    r"|(?<!async )\bfunction\s+[A-Za-z0-9_]*"
                    r"(?:answer|respond|resolve|solve)[A-Za-z0-9_]*\s*\(",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "answer-return-or-struct",
                re.compile(
                    r"\b(?:Answer|Response|CaseResult|RunResult|RunResponse|Reply)"
                    r"\s*\{"
                    r"|\breturn\b[^\n]*\banswer\b"
                    r"|[\"']answer[\"']\s*:"
                    r"|\.answer\s*=(?!=)"
                    r"|\banswer\s*=\s*[\"'A-Za-z0-9_]",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=24,
        suppressors=(
            _Role(
                "model-call-or-await",
                re.compile(
                    r"\bawait\b|\.await\b"
                    r"|\b(?:call|invoke|run|complete|generate|chat)[_ ]?"
                    r"(?:model|completion|chat|llm|inference)\b"
                    r"|\bopenai\b|\bopenrouter\b|\breqwest\b|\bhttpx\b"
                    r"|\bclient\.(?:chat|responses|completions)\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        languages=_ANSWER_LANGUAGES,
    ),
    _Fingerprint(
        # A2/A3: a response/answer constructor that sets literal zero token counts
        # beside a populated answer field. Zero tokens next to a real answer means
        # no model completion was ever produced -- the answer was fabricated.
        kind="zero-token-answer-constructor",
        severity="high",
        roles=(
            _Role(
                "zero-token-literal",
                re.compile(
                    r"\b(?:prompt|output|completion|input|total)[_ ]?tokens?\b"
                    r"[\"']?\s*[:=]\s*0\b"
                    r"|\b(?:tokens[_ ]?used|token[_ ]?count|num[_ ]?tokens|"
                    r"usage[_ ]?tokens)\b[\"']?\s*[:=]\s*0\b",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "populated-answer-field",
                re.compile(
                    r"\b(?:answer|final_answer|response_text|reply|output_text)\b"
                    r"[\"']?\s*[:=]\s*"
                    r"(?!0\b|None\b|null\b|nil\b|\"\"|''|,|\}|\)|;|$)"
                    r"[\"'A-Za-z0-9_\[]",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=10,
    ),
    _Fingerprint(
        # F2: edit-distance / typo-tolerant token comparison near question or case
        # parsing -- a tell of absorbing the private typo projector so misspelled
        # scored questions still match a hardcoded family.
        kind="typo-tolerant-token-compare",
        severity="medium",
        roles=(
            _Role(
                "edit-distance-helper",
                re.compile(
                    r"\blevenshtein\b|\bedit[_ ]?distance\b|\bdamerau\b"
                    r"|\bone[_ ]?typo(?:[_ ]?apart)?\b|\btypo[_ ]?word[_ ]?eq\b"
                    r"|\btypo[_ ]?(?:tolerant|apart|eq)\b"
                    r"|\bfuzzy[_ ]?(?:match|eq|cmp)\b"
                    r"|\bosa[_ ]?distance\b|\bhamming[_ ]?distance\b",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "question-parse-context",
                re.compile(
                    r"\b(?:question|case[_ ]?id|prompt|query|token|word|needle|"
                    r"subject|phrase|utterance)\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=12,
    ),
    _Fingerprint(
        # Registry pattern (lets_5.0): a large table of literal scored-question
        # phrasings used as dispatch keys. Aggregate rule -- a served file that
        # switches on many verbatim question strings is a phrase-table solver.
        kind="literal-question-registry",
        severity="medium",
        roles=(
            _Role(
                "question-literal-dispatch",
                re.compile(
                    r"[\"'][^\"']{0,200}\?[\"']\s*(?:=>|:)"
                    r"|\bcase\s+[\"'][^\"']{0,200}\?[\"']"
                    r"|(?:==|===|\.eq\(|\.equals\(|startswith\(|starts_with\(|"
                    r"contains\()\s*[\"'][^\"']{0,200}\?[\"']",
                    re.IGNORECASE,
                ),
            ),
        ),
        min_hits=4,
    ),
    _Fingerprint(
        # B1/B6 (self-documented): comments or strings on the served path citing
        # the private generator/grader source tree. A general agent has no reason
        # to reference the generator's internal file layout.
        kind="generator-source-citation",
        severity="medium",
        roles=(
            _Role(
                "generator-source-path",
                re.compile(
                    r"\buniverse/world\.go\b|\bconversational\.go\b"
                    r"|\bmemory_v2\.go\b|\bworld\.go\b"
                    r"|(?:^|[\s(\"'])(?:gen|grade|grader|generator|universe)/[A-Za-z0-9_]"
                    r"|/(?:gen|grade|grader|generator|universe)/"
                    r"|\bworld[_ ]?gen\b|\bgenerator[_ ]?template\b"
                    r"|\bquestion[_ ]?generator\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        scan="raw",
    ),
    _Fingerprint(
        # B5: internal scored-family label strings hardcoded on the served path.
        # These `world-*` / `conversational-*` labels are the generator's private
        # question-type taxonomy and appear nowhere a general agent would need.
        kind="scored-family-label",
        severity="medium",
        roles=(
            _Role(
                "scored-family-label-literal",
                re.compile(
                    r"[\"'](?:world-canary"
                    r"|world-[a-z]+-[a-z][a-z-]*"
                    r"|conversational-[a-z][a-z-]*)[\"']",
                    re.IGNORECASE,
                ),
            ),
        ),
        scan="raw",
    ),
    # --- Rev7 (2026-08-18) FAMILY COMPILER fingerprints -----------------------
    # The now-dominant threat is a trained/baked classifier that routes a scored
    # bench question into a closed DittoBench family, then applies a hardcoded
    # per-family recipe. These fingerprints target the greppable seams the four
    # Rev7 holds shared -- the baked router artifact, the "copy this value
    # exactly" directive, computed totals smuggled into the notes prompt, the
    # closed program/evidence markers, the reject-until-match copy loop, the
    # compacted context that replaces retrieved records, and the glossary /
    # verified-result zero-token block. Each stays a location-only review lead:
    # the agentic reviewer proves reachability and the two-location causal bar
    # before any of them becomes a quarantine finding.
    _Fingerprint(
        # baked-family-router (Hogwarts v19 `fc051473`): a baked `.json` classifier
        # loaded via `include_str!`/`load` whose keys are DittoBench family labels,
        # routed through a `family_of` / `family::classify` dispatch. A general
        # agent never ships a compiled question->family table.
        kind="baked-family-router",
        severity="high",
        roles=(
            _Role(
                "family-router-loader",
                re.compile(
                    r"family[-_ ]?router\.json"
                    r"|include_str!\s*\([^)]*family"
                    r"|\b(?:load|read|read_to_string|include|from_str)\b"
                    r"[^\n]*family[^\n]*\.json"
                    r"|\bfn\s+family_of\b"
                    r"|\bfamily::classify\b",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "dittobench-family-key",
                re.compile(
                    r"[\"'](?:world-story-[a-z][a-z-]*"
                    r"|world-project-outstanding"
                    r"|world-trip-[a-z][a-z-]*"
                    r"|world-[a-z]+-[a-z][a-z-]*"
                    r"|conversational-[a-z][a-z-]*)[\"']",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=14,
        scan="raw",
    ),
    _Fingerprint(
        # do-not-recompute-directive (Hogwarts v19 guidance, Aceron_v4): a prompt
        # directive ordering the model to emit a precomputed value verbatim rather
        # than reason. An honest agent never tells the model to copy a value
        # exactly instead of solving.
        kind="do-not-recompute-directive",
        severity="high",
        roles=(
            _Role(
                "exact-value-directive",
                re.compile(
                    r"\bdo not recompute\b"
                    r"|\breply with exactly\b"
                    r"|\banswer with the parenthesized decimal exactly\b"
                    r"|\bcopy its value exactly\b"
                    r"|\bstate this exact resolved value\b"
                    r"|\bEXACT_VALUE_PROMPT\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        scan="raw",
    ),
    _Fingerprint(
        # compiled-totals-as-notes (Aceron_v4 `src/story.rs`, A11): source that
        # writes a locally computed total / reconciled value into the memory/notes
        # prompt the model then reads back, especially beside withheld record
        # sentences. The computed answer is smuggled in as if it were retrieved.
        kind="compiled-totals-as-notes",
        severity="high",
        roles=(
            _Role(
                "reconciled-total-marker",
                re.compile(
                    r"\bwhat is still outstanding is\b"
                    r"|\bauthor_reconciled_value\b"
                    r"|\bsettled_without_operation\b"
                    r"|\b(?:reconciled|computed|resolved|settled)[_ ]?"
                    r"(?:total|value|balance|amount|outstanding)\b",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "memory-or-notes-sink",
                re.compile(
                    r"\b(?:notes|memory_block|memories|context_block|note_block|"
                    r"notes_prompt|memory_prompt)\b\s*"
                    r"(?:\.\s*push(?:_str)?|\.\s*append|\.\s*insert|\+=|=(?!=))",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=12,
        scan="raw",
    ),
    _Fingerprint(
        # open-program-evidence-marker (Omar v20/v21): the closed `Program` tree
        # and `v10_open_program_evidence` marker of a baked solver dressed up as
        # "open program" evidence. A general agent has no enumerated Program tree
        # nor a try_solve shortcut over a fixed family.
        kind="open-program-evidence-marker",
        severity="high",
        roles=(
            _Role(
                "open-program-marker",
                re.compile(
                    r"\bv10_open_program(?:_evidence)?\b"
                    r"|\btry_solve\b"
                    r"|\benum\s+[A-Za-z0-9_]*Program\b"
                    r"|\b[A-Za-z0-9_]*Program\s*::\s*[A-Za-z0-9_]+",
                    re.IGNORECASE,
                ),
            ),
        ),
        scan="code",
    ),
    _Fingerprint(
        # copy-retry-loop (reject-until-match; lets_5.x / Crown / unione): a bounded
        # loop wrapping a model call that re-prompts until the reply matches a
        # locally established value. Distinct from an honest transient retry, which
        # loops on a network/timeout error, not on a value mismatch -- so a genuine
        # backoff retry is suppressed.
        kind="copy-retry-loop",
        severity="medium",
        roles=(
            _Role(
                "bounded-retry-loop",
                re.compile(
                    r"\bfor\s+attempt\s+in\s+1\s*\.\.=?"
                    r"|\bfor\s+attempt\s+in\b"
                    r"|\bfor\s+_\s+in\s+0\s*\.\.\s*[A-Za-z0-9_]"
                    r"|\bwhile\s+attempt\b"
                    r"|\bfor\s+attempt\s+in\s+range\b"
                    r"|\bfor\s*\(\s*(?:let|var|int)?\s*attempt\b",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "in-loop-model-call",
                re.compile(
                    r"\b(?:call|invoke|run|complete|generate|chat)[_ ]?"
                    r"(?:model|completion|chat|llm|inference)\b"
                    r"|\bmodel::(?:chat|complete|generate)\b"
                    r"|\bclient\.(?:chat|completions|responses)\b"
                    r"|\.await\b",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "established-value-gate",
                re.compile(
                    r"\bestablished_for_prompt\b"
                    r"|\b(?:established|expected|reference|target|derived)"
                    r"[_ ]?(?:value|answer|result)\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=16,
        scan="code",
        suppressors=(
            _Role(
                "transient-error-retry",
                re.compile(
                    r"\b(?:timeout|timed?[_ ]?out|rate[_ ]?limit|429|5\d\d|"
                    r"connection|network|transient|backoff|ECONNRESET|"
                    r"ServiceUnavailable)\b",
                    re.IGNORECASE,
                ),
            ),
        ),
    ),
    _Fingerprint(
        # compact-system-prompt-replacement (D3 context replacement, red-dragon v39
        # `baseline.rs`): the system prompt / memory block is assigned a compacted /
        # derived-only value that replaces the retrieved records outright, so the
        # served context is engine-authored rather than retrieved.
        kind="compact-system-prompt-replacement",
        severity="medium",
        roles=(
            _Role(
                "prompt-or-memory-target",
                re.compile(
                    r"\b(?:system_prompt|system_message|memory_block|context_block|"
                    r"memory_prompt)\b\s*=(?!=)",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "compacted-derived-value",
                re.compile(
                    r"=(?!=)\s*[^\n]*\b(?:compact|compacted|summariz|distill|"
                    r"derived[_ ]?only|condensed|digest|reduce[d]?)\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=3,
        scan="code",
    ),
    _Fingerprint(
        # glossary-verified-result-marker (Hogwarts v16): the glossary / verified-
        # result zero-token block markers of a baked phrase solver. These strings
        # and identifiers are the tell of a pre-baked answer table dressed as a
        # "verified result".
        kind="glossary-verified-result-marker",
        severity="medium",
        roles=(
            _Role(
                "glossary-verified-marker",
                re.compile(
                    r"\bVERIFIED RESULT\b"
                    r"|\bglossary_block\b"
                    r"|\bRole::PHRASES\b"
                    r"|\bWJFAST\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        scan="raw",
    ),
    # --- Rev8 (2026-08-21) FAMILY COMPILER fingerprints -----------------------
    # ATH first-pass review rubber-stamped "old identifiers gone" and missed
    # reachable compilers: records-free required_money formatters, LedgerEventKind
    # validators, world_shape_rule injection, and StoryArc remainder compilers.
    # Each stays a location-only review lead, never an auto-ban.
    _Fingerprint(
        # required-money-formatter: CompactToolModel closer that copies the tool
        # money into the graded slot ("Return ANSWER: {money} exactly") instead of
        # keeping records plus an advisory calculator result. required_money plus
        # a copy-the-tool-money directive is the tell; an honest closer that
        # hands the model raw records and a genuine tool result does not fire.
        kind="required-money-formatter",
        severity="high",
        roles=(
            _Role(
                "required-money-token",
                re.compile(r"\brequired_money\b", re.IGNORECASE),
            ),
            _Role(
                "copy-tool-money-directive",
                re.compile(
                    r"\bReturn exactly one\b.{0,40}\bANSWER\b"
                    r"|Return ANSWER:.{0,80}exactly"
                    r"|\bCopy that exact money value\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=14,
        scan="raw",
    ),
    _Fingerprint(
        # ledger-event-kind-compiler: closed LedgerEventKind (Opening /
        # Adjustment / Payment / Expense / Credit) plus a narrative issue
        # validator that reject-until-matches correction_only / event_only.
        kind="ledger-event-kind-compiler",
        severity="high",
        roles=(
            _Role(
                "ledger-event-kind",
                re.compile(r"\bLedgerEventKind\b", re.IGNORECASE),
            ),
            _Role(
                "narrative-ledger-issue-gate",
                re.compile(
                    r"\bnarrative_ledger_issues\b"
                    r"|\bcorrection_only\b"
                    r"|\bevent_only\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=16,
        scan="code",
    ),
    _Fingerprint(
        # world-shape-rule-injection: world_shape_rule still injects Outstanding
        # C-B / BalanceFinal A+/-C-B-D+E onto served /run.
        kind="world-shape-rule-injection",
        severity="high",
        roles=(
            _Role(
                "world-shape-rule",
                re.compile(r"\bworld_shape_rule\b", re.IGNORECASE),
            ),
            _Role(
                "world-shape-injection-target",
                re.compile(
                    r"\bworld_shape_of\b"
                    r"|\bBalanceFinal\b"
                    r"|\bWorldShape::Outstanding\b"
                    r"|\bBalanceMid\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=14,
        scan="raw",
    ),
    _Fingerprint(
        # story-arc-remainder-compiler: StoryArc {base,paid,delta,cost,credit}
        # compiles remainder, injects labeled notes, CASE-/PO- joins, and retries
        # against reply_restates_story_ingredient_money. A generic Ledger {amount}
        # without that family join or remainder compiler does not fire.
        kind="story-arc-remainder-compiler",
        severity="high",
        roles=(
            _Role(
                "story-arc-or-ingredient-retry",
                re.compile(
                    r"\bStoryArc\b"
                    r"|\breply_restates_story_ingredient_money\b",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "remainder-compiler-or-family-join",
                re.compile(
                    r"\bfn\s+balance\b"
                    r"|\bbalance\s*\(\s*\)\s*="
                    r"|CASE-\d{4}"
                    r"|[\"']CASE-"
                    r"|[\"']PO-\d"
                    r"|\bPO-\d{2,}"
                    r"|\breply_restates_story_ingredient_money\b"
                    r"|\bbase\s*\+\s*(?:self\.)?delta"
                    r"|\bdelta\s*-\s*(?:self\.)?paid",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=20,
        scan="raw",
    ),
    _Fingerprint(
        # trip-day-family-retry: days-family coach that retries until the model
        # applies the changed-leg (not whole-trip) day delta. Both identifiers
        # are distinctive; the honest starter-kit has neither.
        kind="trip-day-family-retry",
        severity="medium",
        roles=(
            _Role(
                "days-change-retry-gate",
                re.compile(
                    r"\breply_misses_days_change_application\b",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "days-focus-coach",
                re.compile(
                    r"\bdays_focus_coach_line\b"
                    r"|\bchanged[_ -]?leg\b"
                    r"|\bwhole[_ -]?trip\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=16,
        scan="code",
    ),
    _Fingerprint(
        # worksheet-fallback-overwrite (aceron_b12-v5, 2026-08-27): a
        # reply-vs-worksheet mismatch gate that, after the model's review budget
        # is exhausted, writes an engine-serialized worksheet value into the
        # served text field. The mismatch comparator and the fallback writer
        # co-occur at the application site; comments beside them typically deny
        # the substitution, so the code path is the only admissible witness.
        kind="worksheet-fallback-overwrite",
        severity="high",
        roles=(
            _Role(
                "reply-worksheet-mismatch-gate",
                re.compile(
                    r"\b[a-z0-9_]*(?:reply|draft|answer)_needs_"
                    r"(?:review|correction|repair)\b"
                    r"|\bneeds_review\s*\(",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "engine-text-fallback-writer",
                re.compile(
                    r"\b(?:result\.text|final_text|response_text)\s*=\s*"
                    r"(?:text|fallback)\b"
                    r"|\b[a-z0-9_]+_fallback\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=14,
        suppressors=(
            # Provider/transport failover is the honest look-alike: falling back
            # to another model or endpoint still leaves the model authoring the
            # served text.
            _Role(
                "provider-transport-fallback",
                re.compile(
                    r"\b(?:model|provider|inference|endpoint|gateway|llm|"
                    r"transport)[a-z0-9_]*_fallback\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        scan="code",
    ),
    _Fingerprint(
        # scored-family-decline-gate: a harness classifier predeclares a scored
        # question family unanswerable and scripts the exact decline the model
        # must emit ("Reply exactly: 'I don't have that information'"), with a
        # do-not-attempt directive. The model never attempts the family from
        # the user's records. Genuine unknowable-case honesty ("say briefly it
        # can't be known", no scripted verbatim text) does not carry the
        # scripted-decline role and stays clear.
        kind="scored-family-decline-gate",
        severity="medium",
        roles=(
            _Role(
                "scripted-decline-literal",
                re.compile(
                    r"\b(?:reply|respond|say|answer)\s+exactly\b"
                    r"|don'?t have that information"
                    r"|no information about (?:it|that)",
                    re.IGNORECASE,
                ),
            ),
            _Role(
                "no-attempt-directive",
                re.compile(
                    r"\bdo not (?:call|attempt|guess|search|answer)\b"
                    r"|\bwithout (?:calling|searching|attempting)\b"
                    r"|\babsent_personal_fact\b"
                    r"|\bunanswerable\b",
                    re.IGNORECASE,
                ),
            ),
        ),
        window=8,
        scan="raw",
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


_MAX_FINGERPRINT_FINDINGS = 24
_MAX_FINGERPRINT_PER_RULE_FILE = 4
_FINGERPRINT_LANGUAGE_BY_SUFFIX = {
    "rs": "rust",
    "py": "python",
    "pyx": "python",
    "go": "go",
    "ts": "ts",
    "tsx": "ts",
    "mts": "ts",
    "cts": "ts",
    "js": "ts",
    "jsx": "ts",
    "mjs": "ts",
    "cjs": "ts",
}


def _fingerprint_language(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    suffix = name.rsplit(".", 1)[-1].casefold() if "." in name else ""
    return _FINGERPRINT_LANGUAGE_BY_SUFFIX.get(suffix)


def find_benchmark_emulation_fingerprints(
    files: Iterable[tuple[str, str]],
) -> list[dict[str, object]]:
    """Return bench-v12 anti-emulation fingerprints as location-only review leads.

    Each finding names the matched fingerprint (``kind``), a ``severity``, and the
    ``benchmark_emulation`` category, with ``path``/``line``/``role`` locations
    only. No source text or matched literal ever leaves the archive: the reviewer
    receives which fingerprint fired and where, and must still prove reachability
    and the two-location causal bar before promoting it to a quarantine finding.

    Only executable/served source is scanned (tests, docs, and fixtures are
    excluded), matching the requirement that these tells be reachable from the
    serve/run entrypoint. Scanning is language-aware: each file is classified from
    its suffix, and a fingerprint whose tell is language-specific runs only on the
    languages it applies to.
    """
    findings: list[dict[str, object]] = []
    for path, text in sorted(files, key=lambda item: _path_priority(item[0])):
        if not _is_executable_source_path(path):
            continue
        language = _fingerprint_language(path)
        if language is None:
            continue
        raw_lines = text.splitlines()
        if not raw_lines:
            continue
        code_lines = _mask_comments(text).splitlines()
        code_lines.extend([""] * (len(raw_lines) - len(code_lines)))
        for fingerprint in _EMULATION_FINGERPRINTS:
            if fingerprint.languages and language not in fingerprint.languages:
                continue
            scan_lines = code_lines if fingerprint.scan == "code" else raw_lines
            if fingerprint.min_hits > 1:
                findings.extend(_aggregate_fingerprint(fingerprint, path, scan_lines))
            else:
                findings.extend(
                    _cooccurrence_fingerprint(fingerprint, path, scan_lines, raw_lines)
                )
            if len(findings) >= _MAX_FINGERPRINT_FINDINGS:
                return findings[:_MAX_FINGERPRINT_FINDINGS]
    return findings


def _fingerprint_finding(
    fingerprint: _Fingerprint, locations: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "category": "benchmark_emulation",
        "kind": fingerprint.kind,
        "severity": fingerprint.severity,
        "locations": locations,
    }


def _cooccurrence_fingerprint(
    fingerprint: _Fingerprint,
    path: str,
    scan_lines: list[str],
    raw_lines: list[str],
) -> list[dict[str, object]]:
    role_hits = {
        role.name: [
            line_number
            for line_number, line in enumerate(scan_lines, 1)
            if role.pattern.search(line[:4096])
        ]
        for role in fingerprint.roles
    }
    if any(not hits for hits in role_hits.values()):
        return []
    # Suppressors read the raw view so an honest look-alike (a genuine model call
    # or awaited completion in the same body) reliably clears the lead.
    suppressor_hits = [
        line_number
        for suppressor in fingerprint.suppressors
        for line_number, line in enumerate(raw_lines, 1)
        if suppressor.pattern.search(line[:4096])
    ]
    findings: list[dict[str, object]] = []
    seen: set[tuple[tuple[str, int], ...]] = set()
    anchors = sorted({line for hits in role_hits.values() for line in hits})
    for anchor in anchors:
        locations: list[dict[str, object]] = []
        signature: list[tuple[str, int]] = []
        for role in fingerprint.roles:
            nearby = min(
                role_hits[role.name],
                key=lambda line: (abs(line - anchor), line),
            )
            if abs(nearby - anchor) > fingerprint.window:
                break
            signature.append((role.name, nearby))
            locations.append({"path": path, "line": nearby, "role": role.name})
        else:
            if any(abs(hit - anchor) <= fingerprint.window for hit in suppressor_hits):
                continue
            normalized = tuple(signature)
            if normalized in seen:
                continue
            seen.add(normalized)
            findings.append(_fingerprint_finding(fingerprint, locations))
            if len(seen) >= _MAX_FINGERPRINT_PER_RULE_FILE:
                break
    return findings


def _aggregate_fingerprint(
    fingerprint: _Fingerprint, path: str, scan_lines: list[str]
) -> list[dict[str, object]]:
    role = fingerprint.roles[0]
    hits = [
        line_number
        for line_number, line in enumerate(scan_lines, 1)
        if role.pattern.search(line[:4096])
    ]
    if len(hits) < fingerprint.min_hits:
        return []
    locations = [
        {"path": path, "line": line_number, "role": role.name}
        for line_number in hits[: _MAX_FINGERPRINT_PER_RULE_FILE + 2]
    ]
    return [_fingerprint_finding(fingerprint, locations)]


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
    "find_benchmark_emulation_fingerprints",
    "find_decisive_malicious_source",
    "find_source_review_leads",
    "is_executable_source_path",
    "mask_comments",
    "source_path_priority",
]

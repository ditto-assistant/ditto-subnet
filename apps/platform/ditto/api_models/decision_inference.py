"""Wire for the dark decision-model lane (System One shape, passed through).

``POST /api/v1/inference/decisions`` on the model relay takes the System One
body that TypeSafe Jev defines and OpenRouter serves at
``POST https://openrouter.ai/api/v1/systemone``:
``{state, questions: {<id>: {type, instructions, criteria?}}, assets?}``.

The relay forwards it nearly verbatim. It forces ``model`` to the grant's
pinned version (as chat forces the ticket model), refuses only the top-level
fields that would change OpenRouter routing or account observability
(``provider``, ``session_id``, ``user``, ``trace``), and passes ``questions``
through without narrowing the question vocabulary (``choice``, ``score``,
``noul``, ``box`` and whatever the provider adds) or limiting how many are
asked. The provider's own limits apply. The only Platform controls are
metering, the per-run token and request walls, the pinned model and the
operator's on/off knob. The authoritative check lives in
``services/model-relay/internal/inference/decisions.go``.

The response is ``{model, answers, usage: {input_tokens, output_tokens}}``.
``answers`` is the provider's map verbatim and ``model`` is the served version,
which the relay has verified against the pin. The lane carries no score.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

DECISION_REFUSED_TOP_LEVEL_FIELDS = frozenset(
    {"provider", "session_id", "user", "trace"}
)


class DecisionQuestion(BaseModel):
    """One question. Only ``type`` is required; everything else passes through."""

    model_config = ConfigDict(extra="allow", frozen=True)

    type: Annotated[str, Field(min_length=1)]


class DecisionRequest(BaseModel):
    """Unknown top-level fields are allowed here; the relay, not this model,
    refuses the four it owns (``DECISION_REFUSED_TOP_LEVEL_FIELDS``)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    state: str | dict[str, Any] | list[Any]
    questions: Annotated[dict[str, DecisionQuestion], Field(min_length=1)]
    assets: dict[str, Any] | None = None
    model: str | None = None
    """Optional and ignored: the relay always sends the grant's pin."""


class DecisionUsage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)] = 0


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    model: str
    answers: dict[str, dict[str, Any]]
    usage: DecisionUsage

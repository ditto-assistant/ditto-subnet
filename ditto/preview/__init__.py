"""Preview channels: isolated SN118 stacks with a Foundry-style mock engine.

The same composition, cheatcodes, and URLs work on a laptop and in GitHub
Actions. Previews never mint GitHub Releases, ``v*`` image tags, or
``compat-2``.
"""

from ditto.preview.composition import (
    PREVIEW_PROFILES,
    CompositionError,
    PreviewPlan,
    compose,
)
from ditto.preview.engine import IsolationError, PreviewEngine
from ditto.preview.identity import preview_id

__all__ = [
    "PREVIEW_PROFILES",
    "CompositionError",
    "IsolationError",
    "PreviewEngine",
    "PreviewPlan",
    "compose",
    "preview_id",
]

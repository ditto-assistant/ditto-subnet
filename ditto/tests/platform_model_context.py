"""Load Platform's model_context wire models into the root test environment.

The root package and Platform both own ``ditto.api_models``, so the subnet
copy cannot import Platform's models by name. Platform's
``apps/platform/ditto/api_models/model_context.py`` depends only on the
standard library and pydantic, so it loads here from its file, under a private
module name, and the validator and miner CLI tests check their wire shapes
against the real producer and consumer models.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PLATFORM_MODEL_CONTEXT = (
    Path(__file__).resolve().parents[2]
    / "apps"
    / "platform"
    / "ditto"
    / "api_models"
    / "model_context.py"
)
_MODULE_NAME = "_platform_api_models_model_context"


def load() -> ModuleType:
    """Platform's ``ditto.api_models.model_context`` module."""
    loaded = sys.modules.get(_MODULE_NAME)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, PLATFORM_MODEL_CONTEXT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {PLATFORM_MODEL_CONTEXT}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: pydantic resolves postponed annotations through
    # ``sys.modules[cls.__module__]``.
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module

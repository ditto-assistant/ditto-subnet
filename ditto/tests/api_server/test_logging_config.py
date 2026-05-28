"""Unit tests for :mod:`ditto.api_server.logging_config`."""

from __future__ import annotations

import logging
import logging.config

from ditto.api_server.logging_config import build_dict_config


class TestBuildDictConfig:
    """Shape checks for the dictConfig payload."""

    def test_required_top_level_keys(self):
        cfg = build_dict_config("INFO")
        assert cfg["version"] == 1
        assert cfg["disable_existing_loggers"] is False
        assert "handlers" in cfg
        assert "loggers" in cfg
        assert "formatters" in cfg
        assert "filters" in cfg
        assert "root" in cfg

    def test_request_id_filter_referenced(self):
        cfg = build_dict_config("INFO")
        assert (
            cfg["filters"]["request_id"]["()"]
            == "ditto.api_server.middleware.request_id.RequestIdFilter"
        )

    def test_stdout_handler_uses_filter_and_formatter(self):
        cfg = build_dict_config("INFO")
        stdout = cfg["handlers"]["stdout"]
        assert stdout["class"] == "logging.StreamHandler"
        assert "request_id" in stdout["filters"]
        assert stdout["formatter"] == "default"

    def test_uvicorn_access_disabled(self):
        """``uvicorn.access`` must have no handlers so the request-id
        middleware's access log line is the only source of truth."""
        cfg = build_dict_config("INFO")
        access = cfg["loggers"]["uvicorn.access"]
        assert access["handlers"] == []
        assert access["propagate"] is False

    def test_root_level_takes_arg(self):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            cfg = build_dict_config(level)
            assert cfg["root"]["level"] == level

    def test_dict_config_is_loadable(self):
        """``logging.config.dictConfig`` must accept the payload."""
        # Filter import is module-level; constructing the payload alone
        # is not enough - we need to confirm dictConfig wires it up.
        logging.config.dictConfig(build_dict_config("WARNING"))
        # Reset root so other tests are not affected.
        logging.getLogger().setLevel(logging.NOTSET)

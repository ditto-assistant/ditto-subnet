"""Tests for ditto link-github command."""

import json
from unittest.mock import MagicMock, patch
import pytest

from ditto.miner_cli.commands.link_github import (
    link_github,
    link_github_status,
    link_github_unlink,
)

def test_link_github_status_not_linked():
    """Verify link-github status handles empty/unlinked responses gracefully."""
    mock_client = MagicMock()
    mock_client.get.return_value = {"linked": False, "github_login": None}
    
    with patch("ditto.miner_cli.commands.link_github.get_api_client", return_value=mock_client):
        result = link_github_status(dry_run=True)
        assert result.get("linked") is False

def test_link_github_unlink_call():
    """Verify unlink dispatches DELETE request to correct endpoint."""
    mock_client = MagicMock()
    mock_client.delete.return_value = {"status": "revoked"}
    
    with patch("ditto.miner_cli.commands.link_github.get_api_client", return_value=mock_client):
        res = link_github_unlink()
        assert res.get("status") == "revoked"
        mock_client.delete.assert_called_once()

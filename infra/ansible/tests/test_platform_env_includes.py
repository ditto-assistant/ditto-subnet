"""The included GitHub settings must remain shell-sourceable in the full env."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from ansible.plugins.filter.core import FilterModule
from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES = Path(__file__).resolve().parents[1] / "roles/platform_app/templates"
BEGIN = "# BEGIN ANSIBLE MANAGED GITHUB LINK"
END = "# END ANSIBLE MANAGED GITHUB LINK"


class PlatformEnvironmentIncludesTest(unittest.TestCase):
    def test_github_block_preserves_shell_assignment_boundaries(self):
        parent = (TEMPLATES / "platform.env.j2").read_text()
        block = parent[parent.index(BEGIN) : parent.index(END) + len(END)]
        for keep_newline in (False, True):
            for enabled in (False, True):
                with self.subTest(keep_newline=keep_newline, enabled=enabled):
                    env = Environment(
                        loader=FileSystemLoader(TEMPLATES),
                        trim_blocks=True,
                        keep_trailing_newline=keep_newline,
                        undefined=StrictUndefined,
                    )
                    env.filters.update(FilterModule().filters())
                    rendered = env.from_string(block).render(
                        platform_github_link_enabled=enabled,
                        platform_github_link_client_id="synthetic-client",
                        platform_secrets={
                            "github_link_client_secret": "synthetic-secret"
                        },
                        platform_github_link_redirect_url="https://example.com/callback",
                        platform_github_link_timeout_seconds=10,
                    )
                    with tempfile.TemporaryDirectory() as directory:
                        fixture = Path(directory) / "platform.env"
                        fixture.write_text(rendered)
                        result = subprocess.run(
                            [
                                "bash",
                                "--noprofile",
                                "--norc",
                                "-euc",
                                '. "$1"; test "$GITHUB_LINK_TIMEOUT_SECONDS" = 10; '
                                'test "$GITHUB_LINK_ENABLED" = "$2"; '
                                'test "$GITHUB_LINK_CLIENT_ID" = synthetic-client; '
                                'test "$GITHUB_LINK_CLIENT_SECRET" = synthetic-secret; '
                                'test "$GITHUB_LINK_REDIRECT_URL" = https://example.com/callback',
                                "platform-env-test",
                                str(fixture),
                                str(enabled).lower(),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

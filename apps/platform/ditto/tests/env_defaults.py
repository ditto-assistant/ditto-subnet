"""Test-only defaults for the environment the suite cannot boot without.

Why this file exists
--------------------
On a pristine checkout ``uv run pytest`` failed eleven tests, every one of
them because ``DITTO_UPLOAD_PAYMENT_ADDRESS`` (and, behind it,
``PYLON_OPEN_ACCESS_TOKEN`` and ``STORAGE_*``) were unset. Those values
lived only in ``.env``, which is gitignored *and* is not copied into a git
worktree -- so a fresh clone and every worktree-based agent inherited a red
suite. The cost was never the eleven failures; it was that a suite which is
red by default teaches everyone to skim past red, which is how a stale test
reached production and sat there for a day.

Why the defaults are **here** and not in ``ditto/api_server/config.py``
----------------------------------------------------------------------
The obvious fix -- give the production parser a fallback -- is the wrong
one and would be worse than the red suite. A platform that boots with a
placeholder receive address silently routes miner upload fees to nowhere;
a platform that refuses to boot pages an operator in thirty seconds. So
``parse_api_server_config_from_env`` keeps raising ``ApiServerConfigError``
on an unset address, and the *test process* -- and nothing else -- seeds an
obviously-fake value into ``os.environ`` before collection
(``pytest_configure`` in ``ditto/tests/conftest.py``).

Two consequences follow from seeding ``os.environ`` rather than patching
the parsers:

* ``monkeypatch.delenv(...)`` still works, so the tests that assert
  production fails loudly on a missing value (``test_config.py``,
  ``test_main.py``) keep proving exactly what they claim to prove;
* a developer's ``.env`` or an exported variable still wins -- every
  default here is applied only when the variable is *unset*.

Choosing the values
-------------------
Every value is either an unmistakable fixture sentinel or the same
throwaway credential ``docker-compose.yml`` and CI already use. Nothing
here is a secret and nothing here is reachable: the payment address is not
a real SS58 key, and Pylon points at a closed port on purpose, so a test
that starts making real chain calls fails loudly instead of quietly
succeeding against whatever Pylon happens to be running on the developer's
laptop.

Adding a newly-required variable
--------------------------------
Add it to :data:`TEST_ENV_DEFAULTS`. If you do not,
``ditto/tests/test_env_defaults.py`` fails and names the variable.
"""

from __future__ import annotations

import os

from ditto.tests import minioharness

# Deliberately *not* a real SS58 key. It satisfies the base58/length shape
# that `config._SS58_RE` enforces (47 chars, no 0/O/I/l) and nothing more:
# read aloud it says "not a real SS58 address, test fixture, do not send TAO
# here". A plausible-looking address -- //Alice, say -- would be the worse
# choice, because the whole point of a sentinel is that it is impossible to
# mistake for a live receive address when it turns up in a log line, a
# database row, or an API response body.
FIXTURE_PAYMENT_ADDRESS = "5NotARea1SS58AddressTestFixtureDoNotSendTaoHere"

# The same throwaway credentials `docker-compose.yml`'s minio service and the
# CI workflow's MinIO container use. Nothing here is a secret.
TEST_ENV_DEFAULTS: dict[str, str] = {
    # --- upload / payment ------------------------------------------------
    "DITTO_UPLOAD_PAYMENT_ADDRESS": FIXTURE_PAYMENT_ADDRESS,
    # --- chain -----------------------------------------------------------
    # ChainConfig refuses to construct without a credential, so this is
    # needed to *build* the app even though nothing in the default suite
    # reaches Pylon. Port 1 has nothing on it by design: the two tests that
    # genuinely need a chain are `needs_chain`-marked and deselected, and if
    # a new test ever starts making real chain calls it should fail here
    # rather than silently depend on the developer's local stack.
    "PYLON_URL": "http://127.0.0.1:1",
    "PYLON_OPEN_ACCESS_TOKEN": "test-fixture-no-chain",
    # --- object storage --------------------------------------------------
    # Points at `minioharness`'s ambient test container, not at compose's
    # store on :9000, for the reason pgharness picks 15433 over 5432: a test
    # run must not be able to write into a bucket somebody is looking at.
    # These values make the *parsers* succeed everywhere; the four
    # `integration` tests that actually move bytes get the container itself
    # provisioned on demand by the `object_storage` fixture.
    "STORAGE_ENDPOINT_URL": f"http://127.0.0.1:{minioharness.HOST_PORT}",
    "STORAGE_BUCKET": minioharness.BUCKET,
    "STORAGE_ACCESS_KEY": minioharness.ACCESS_KEY,
    "STORAGE_SECRET_KEY": minioharness.SECRET_KEY,
    "STORAGE_REGION": "us-east-1",
    "STORAGE_USE_TLS": "false",
}

# Supplied per-worker by the `worker_database` fixture in conftest, not by
# the mapping above: their values are the address of *this* xdist worker's
# private database, which is only known at run time. Named here so the
# regression guard can tell "conftest did not export the DSN" apart from
# "somebody added a required variable and forgot the default".
RUNTIME_PROVIDED_ENV = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")


def apply_test_env_defaults(environ: dict[str, str] | None = None) -> list[str]:
    """Seed :data:`TEST_ENV_DEFAULTS` into ``environ`` where unset.

    Existing values always win, so a developer's ``.env``, a ``make
    test-integration`` invocation, and CI's explicit block all keep their
    behaviour; this only removes the requirement to have any of them.

    Returns:
        The names of the variables this call actually set, in declaration
        order -- useful for the ``-v`` output of the regression guard.
    """
    target = os.environ if environ is None else environ
    applied = []
    for name, value in TEST_ENV_DEFAULTS.items():
        if not target.get(name):
            target[name] = value
            applied.append(name)
    return applied

"""Point the integration suite at this worker's own database and store.

Every file here calls :func:`ditto.db.create_db_engine` inline, with no
fixture, reading ambient ``POSTGRES_*``. Requesting ``worker_database``
autouse rewrites those variables to this worker's private clone, which is
what makes ``-n auto`` safe: the ``TRUNCATE ... CASCADE`` these tests run
now takes ``ACCESS EXCLUSIVE`` on tables nobody else can be holding row
locks on, so the lock-order inversion behind the intermittent
``DeadlockDetectedError`` cannot form.

``object_storage`` is requested autouse for the same reason: the upload
files here build their config from the environment and stream real bytes to
a real S3 endpoint, and reading it from a fixture means those files need no
edit either. It is also what lets `uv run pytest` provision what it needs on
a fresh clone, instead of failing four tests on a store nobody told you to
start with `make stack-up`.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _integration_database(worker_database: object) -> object:
    return worker_database


@pytest.fixture(scope="session", autouse=True)
def _integration_object_storage(object_storage: None) -> None:
    return object_storage

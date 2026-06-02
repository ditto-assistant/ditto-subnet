"""Per-domain query modules.

Empty by design. Each feature PR that needs queries adds its own file
here (``agents.py``, ``payments.py``, ``scores.py``, ``sessions.py``,
...) under the JIT principle: queries land alongside the endpoints
that exercise them, not as a speculative interface up front.

Every function in this package must:

- Take ``session: AsyncSession`` as the first positional argument.
- Operate inside a caller-owned transaction (``async with
  session.begin():``) when crossing more than one INSERT/UPDATE so
  partial-failure rollback is automatic.
- Mutate via the ORM models in :mod:`ditto.db.models` (``session.add``
  + ``session.flush``); never hand-format SQL with caller data.
- Catch ``sqlalchemy.exc.IntegrityError`` at the boundary, dispatch on
  ``e.orig`` (asyncpg-specific) to a typed error, and re-raise with
  ``raise <TypedError>(...) from e``. Domain-specific replay or
  uniqueness outcomes get domain-typed errors (e.g.
  :class:`PaymentReplayedError` for the upload-payment PK collision);
  everything else gets :class:`ditto.db.IntegrityError`.
"""

from __future__ import annotations

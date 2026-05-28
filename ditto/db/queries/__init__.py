"""Per-domain query modules.

Empty by design. Each feature PR that needs queries adds its own file
here (``agents.py``, ``payments.py``, ``scores.py``, ``sessions.py``,
...) under the JIT principle: queries land alongside the endpoints
that exercise them, not as a speculative interface up front.

Every function in this package must:
- Take ``pool: asyncpg.Pool`` as the first positional argument.
- Use ``$N`` placeholders for any parameter that contains caller data.
  Never string-format user values into the SQL.
- Be wrapped with :func:`ditto.db.connection.db_operation` so nested
  calls share one connection.
- Translate ``asyncpg.IntegrityConstraintViolationError`` into
  :class:`ditto.db.IntegrityError` at the boundary.
"""

from __future__ import annotations

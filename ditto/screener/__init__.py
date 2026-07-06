"""SN118 screener worker — the cheap pre-evaluation build gate.

A standalone, stateless daemon (``python -m ditto.screener``) that drains freshly
``uploaded`` agents from the platform, runs a **build + serve** check on each
submitted crate, and reports a signed pass/fail verdict. A pass promotes the
agent ``uploaded -> evaluating`` (the validator queue then picks it up); a fail
moves it to ``screening_failed`` so a crate that does not compile never costs a
full DittoBench scoring run.

It is a sibling of :mod:`ditto.validator`: same HTTP-decoupled shape (talks to
the platform only over the ``/screener/*`` API + signs with a chain hotkey), no
DB, one process per hotkey. The build gate itself lives in :mod:`ditto.screener.gate`.
"""

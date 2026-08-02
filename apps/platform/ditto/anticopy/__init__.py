"""Anti-copy tooling for SN118 (see ``docs/SEMANTIC-CLONE-PREVENTION.md``).

The production anti-copy *signals* live where the data is (lexical fingerprint in
:mod:`ditto.api_server.fingerprint`, structural in dittobench, the gate in
:mod:`ditto.api_server.scoring_gate`). This package holds the **offline
calibration** side: :mod:`ditto.anticopy.calibration` scores each signal against a
labeled clone/independent corpus so thresholds are chosen from data, not guessed.
"""

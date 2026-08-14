# Release delivery recovery

## v0.62.0 and v0.62.1

The v0.62.0 semantic release completed on August 14, 2026, but GitHub Actions
suppressed every post-release job because optional source-verification jobs had
been skipped. The release plan had selected Platform, Backroom, and validator
stack, so the tag alone did not activate those changes: no selected application
deploy or validator-stack image build ran.

The subsequent v0.62.1 release reproduced the same handoff failure after
selecting Platform and Backroom. It published another tag without deploying
either application.

The follow-up release reselects those three component lanes and evaluates every
post-release job explicitly after optional verification skips. Treat v0.62.0
and v0.62.1 as published source metadata, not as deployment or validator-fleet
activation.

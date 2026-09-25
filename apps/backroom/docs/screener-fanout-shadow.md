# Screener fan-out shadow operator note

The complete rollout, budget, and rollback procedure is in the
[worker runbook](../../../workers/screener/docs/fanout-shadow.md).

Production uses the credits-only Ditto Router endpoint
`screener-fanout-shadow` (`ee6e0456-e330-4166-b255-ca92b744fdf8`). Both its
endpoint and dedicated key have a $20 daily cap. Platform reports the provider
cost returned with each response; Router key and endpoint usage report billed
spend after the configured 15% Ditto markup, so those figures are expected to
differ by that amount.

The lower-priority Platform consumer follows the configured source-review
provider priority. It may use spare Targon capacity or the existing Cloud Run
Jobs adapter; Cloud Run runs one 2-CPU, 4-GiB task with no provider retry and a
hard timeout 300 seconds beyond the artifact's application wall-time budget.
It does not change or use the GCE VM autoscaler.

# Import provenance

This app was imported from
[`ditto-assistant/ditto-platform`](https://github.com/ditto-assistant/ditto-platform)
at commit `ac95b516464fe0687ff91162ec441144acac8d49`.

The original repository retains the pre-monorepo history. New subnet Platform
development belongs in this directory after the deployment cutover layer lands.

Intentional monorepo-only changes after that pin include repository-local
source links, the deployment-workflow compatibility test, and deterministic
calibration archives with a zero gzip timestamp. Until the cutover disables the
source repository's deploy workflow, its calibration archive bytes can differ
from this directory even when the logical archive contents are identical.

# CHANGELOG

<!-- version list -->

## v0.117.5 (2026-08-28)

### Bug Fixes

- **platform**: Back off failed screening attempts from failure time, not lease end
  ([#1218](https://github.com/ditto-assistant/ditto-subnet/pull/1218),
  [`e9c5db1`](https://github.com/ditto-assistant/ditto-subnet/commit/e9c5db102a3ee1be3abc9de5467f3d8c8abbdc35))

- **screener**: Ride the long unbilled ladder for HTTP faults and non-JSON bodies
  ([#1219](https://github.com/ditto-assistant/ditto-subnet/pull/1219),
  [`57cdf96`](https://github.com/ditto-assistant/ditto-subnet/commit/57cdf967c16efd698539cb2d45a5df112b1f74fb))


## v0.117.4 (2026-08-28)

### Bug Fixes

- **screener**: Retry unclassified model-body faults on the transport ladder
  ([#1214](https://github.com/ditto-assistant/ditto-subnet/pull/1214),
  [`25ef293`](https://github.com/ditto-assistant/ditto-subnet/commit/25ef293225fe8b75fd5f92a78579a73c28352787))


## v0.117.3 (2026-08-28)

### Bug Fixes

- **platform**: Carry Targon refusal bodies into TargonAPIError reasons
  ([#1213](https://github.com/ditto-assistant/ditto-subnet/pull/1213),
  [`2bf4c0e`](https://github.com/ditto-assistant/ditto-subnet/commit/2bf4c0e79499698c8067702b7e6ed560875ea519))

### Documentation

- **skills**: Route live log diagnosis to pm2 and Cloud Run log surfaces
  ([#1212](https://github.com/ditto-assistant/ditto-subnet/pull/1212),
  [`0dd8143`](https://github.com/ditto-assistant/ditto-subnet/commit/0dd8143b290cb5653f1124665f495f931b656c91))


## v0.117.2 (2026-08-28)

### Bug Fixes

- **screener-orchestrator**: Pull Kaniko base images through mirror.gcr.io
  ([#1211](https://github.com/ditto-assistant/ditto-subnet/pull/1211),
  [`9fd45ea`](https://github.com/ditto-assistant/ditto-subnet/commit/9fd45eaa69f623147aae944ecc7e60801e3dd4fe))


## v0.117.1 (2026-08-28)

### Bug Fixes

- **platform**: Persist platform-attested quarantine audits without 500
  ([#1209](https://github.com/ditto-assistant/ditto-subnet/pull/1209),
  [`93e7702`](https://github.com/ditto-assistant/ditto-subnet/commit/93e7702f62138b47d49691566ef4cc4c000a56f0))

- **screener**: Retry model faults relayed inside HTTP 200 review bodies
  ([#1210](https://github.com/ditto-assistant/ditto-subnet/pull/1210),
  [`a2526bb`](https://github.com/ditto-assistant/ditto-subnet/commit/a2526bb6942bda30869504fc7e30064a8430fb10))


## v0.117.0 (2026-08-28)

### Features

- **backroom**: Compact get_miner_owner_footprint payloads
  ([#1200](https://github.com/ditto-assistant/ditto-subnet/pull/1200),
  [`e7b52ef`](https://github.com/ditto-assistant/ditto-subnet/commit/e7b52ef2401aa0339fa38b45f7b5720cc1e0719a))


## v0.116.1 (2026-08-28)

### Bug Fixes

- **platform**: Keep fleet rows and review holds across operations polls
  ([#1198](https://github.com/ditto-assistant/ditto-subnet/pull/1198),
  [`c6408e8`](https://github.com/ditto-assistant/ditto-subnet/commit/c6408e856afa852ebd8a0701e4a392efb43efd2a))


## v0.116.0 (2026-08-28)

### Bug Fixes

- **infra**: Drop deleted Google identities from ssh_users
  ([#1199](https://github.com/ditto-assistant/ditto-subnet/pull/1199),
  [`ef68b35`](https://github.com/ditto-assistant/ditto-subnet/commit/ef68b3560580197a513376678a0f4e220e507bdb))

### Chores

- Agent/ath v10 independent pass
  ([#1197](https://github.com/ditto-assistant/ditto-subnet/pull/1197),
  [`f683e4f`](https://github.com/ditto-assistant/ditto-subnet/commit/f683e4ff20fb97f92e60b318a2924bd056ca4397))

### Features

- **preview**: Publish guarded dashboard URLs
  ([#1088](https://github.com/ditto-assistant/ditto-subnet/pull/1088),
  [`678234c`](https://github.com/ditto-assistant/ditto-subnet/commit/678234ce19d3c0fa1b59d6687a6428235f319236))


## v0.115.3 (2026-08-27)

### Bug Fixes

- **platform**: Honor retryable_infra source-review dispositions
  ([#1191](https://github.com/ditto-assistant/ditto-subnet/pull/1191),
  [`66443bb`](https://github.com/ditto-assistant/ditto-subnet/commit/66443bb4def2b95718b9dc97029f42c32f69f425))

- **screener**: Recover provider error bodies and toolless model turns
  ([#1192](https://github.com/ditto-assistant/ditto-subnet/pull/1192),
  [`01b00ac`](https://github.com/ditto-assistant/ditto-subnet/commit/01b00ac1cc66eb7d9ce626cde1bae52291501a11))

- **validator**: Keep canonical slots polling during retests
  ([#1195](https://github.com/ditto-assistant/ditto-subnet/pull/1195),
  [`afd3120`](https://github.com/ditto-assistant/ditto-subnet/commit/afd31205233948e6e6712e0e8518948ef322692d))


## v0.115.2 (2026-08-27)

### Bug Fixes

- **platform**: Keep the pipeline lanes' scroll containers alive across polls
  ([#1194](https://github.com/ditto-assistant/ditto-subnet/pull/1194),
  [`3bb122c`](https://github.com/ditto-assistant/ditto-subnet/commit/3bb122cc9624ef52fbf8e6523082d7bff5e44407))

- **platform**: Require manual confirmation retries
  ([#1193](https://github.com/ditto-assistant/ditto-subnet/pull/1193),
  [`9533e0a`](https://github.com/ditto-assistant/ditto-subnet/commit/9533e0af10485f40852114ffd0c90af3cd689b80))


## v0.115.1 (2026-08-27)

### Bug Fixes

- **platform**: Preserve dashboard scroll and row identity
  ([`aa47df6`](https://github.com/ditto-assistant/ditto-subnet/commit/aa47df669cfd2c71312cca59fefbbe5898beba4a))

### Refactoring

- **platform**: Render lists through For/Index, not .map
  ([`8c3bf91`](https://github.com/ditto-assistant/ditto-subnet/commit/8c3bf91a695d5acdc0ba0a306500fd1efa29d28a))


## v0.115.0 (2026-08-27)

### Features

- **backroom**: Control screener policy manifests
  ([#1188](https://github.com/ditto-assistant/ditto-subnet/pull/1188),
  [`64a8994`](https://github.com/ditto-assistant/ditto-subnet/commit/64a8994df8284b94a47dad1d4d0b8d49b26bb745))


## v0.114.1 (2026-08-27)

### Bug Fixes

- **platform**: Bound policy-bump rescreens to the active benchmark era
  ([#1187](https://github.com/ditto-assistant/ditto-subnet/pull/1187),
  [`be209fb`](https://github.com/ditto-assistant/ditto-subnet/commit/be209fb5e04b3f7f50b4dfc398ac55fb82a423e0))


## v0.114.0 (2026-08-27)

### Bug Fixes

- **infra**: Size platform app boot disks to 100G
  ([#1185](https://github.com/ditto-assistant/ditto-subnet/pull/1185),
  [`f1a986c`](https://github.com/ditto-assistant/ditto-subnet/commit/f1a986c7a3bddcc726d532f6e14eeac862b4dc0e))

### Documentation

- **backroom-review**: Pin renamed-compiler ATH holdings
  ([#1184](https://github.com/ditto-assistant/ditto-subnet/pull/1184),
  [`4bfb2dd`](https://github.com/ditto-assistant/ditto-subnet/commit/4bfb2ddd4ff73d32e8815b930fac81a3ad7d172f))

### Features

- **screener**: Enforce policy v10 invariants
  ([#1186](https://github.com/ditto-assistant/ditto-subnet/pull/1186),
  [`26b3fe2`](https://github.com/ditto-assistant/ditto-subnet/commit/26b3fe2e58c00a26b271da23e50b9823d8cef38c))


## v0.113.4 (2026-08-27)

### Bug Fixes

- **platform**: Retry runtime smoke without rebuilding kaniko
  ([#1181](https://github.com/ditto-assistant/ditto-subnet/pull/1181),
  [`70d9225`](https://github.com/ditto-assistant/ditto-subnet/commit/70d92256d711b8d910c3bb783cf9d4c94a2d0f2d))


## v0.113.3 (2026-08-27)

### Bug Fixes

- **inference**: Stop token-wall retries and allow a 150M cap
  ([#1180](https://github.com/ditto-assistant/ditto-subnet/pull/1180),
  [`52f6e85`](https://github.com/ditto-assistant/ditto-subnet/commit/52f6e852f2ecadf268dbbc4ed791a8d6b69bf4f1))


## v0.113.2 (2026-08-27)

### Bug Fixes

- **platform**: Park repeated provider-backoff screening failures with peer evidence
  ([#1183](https://github.com/ditto-assistant/ditto-subnet/pull/1183),
  [`c87ab8b`](https://github.com/ditto-assistant/ditto-subnet/commit/c87ab8bf14c48906929b3e388b6385a8eea95079))


## v0.113.1 (2026-08-27)

### Bug Fixes

- **screener**: Catch worksheet-fallback overwrite and scored-family decline gates
  ([#1182](https://github.com/ditto-assistant/ditto-subnet/pull/1182),
  [`72445d9`](https://github.com/ditto-assistant/ditto-subnet/commit/72445d939db25fd7665cabe66289f0f09fd61bba))

### Chores

- **agents**: Tighten context lookup routing
  ([#1175](https://github.com/ditto-assistant/ditto-subnet/pull/1175),
  [`33e3142`](https://github.com/ditto-assistant/ditto-subnet/commit/33e3142ada02c30d7ececd921760c1e3dd86c588))

- **tests**: Wrap confirmation-transport bind-failure signature
  ([#1178](https://github.com/ditto-assistant/ditto-subnet/pull/1178),
  [`099f32f`](https://github.com/ditto-assistant/ditto-subnet/commit/099f32f2dad51678d8afcfe0c8c6904ac9c77441))


## v0.113.0 (2026-08-26)

### Bug Fixes

- **dittobench**: Distinguish confirmation tool_endpoint bind failures
  ([#1171](https://github.com/ditto-assistant/ditto-subnet/pull/1171),
  [`ca09cfe`](https://github.com/ditto-assistant/ditto-subnet/commit/ca09cfee7de90b99258522822d72a357711f1a69))

- **platform**: Pin Cloud Run after Targon provision deaths
  ([#1170](https://github.com/ditto-assistant/ditto-subnet/pull/1170),
  [`635ebad`](https://github.com/ditto-assistant/ditto-subnet/commit/635ebaddab577200c363d2bd10f594c42635bfaa))

- **platform**: Raise confirmation dollar ceiling to $2000
  ([#1177](https://github.com/ditto-assistant/ditto-subnet/pull/1177),
  [`0aca476`](https://github.com/ditto-assistant/ditto-subnet/commit/0aca476787624be76f06552c2b012f3f0e72e351))

### Chores

- **tests**: Wrap v9 confirmation transport test signature
  ([#1179](https://github.com/ditto-assistant/ditto-subnet/pull/1179),
  [`99ccc64`](https://github.com/ditto-assistant/ditto-subnet/commit/99ccc64cd934eb98ceded6c298a6b2035f98bef1))

### Features

- Add Backroom reject for running screening submissions
  ([#1172](https://github.com/ditto-assistant/ditto-subnet/pull/1172),
  [`f6a3e23`](https://github.com/ditto-assistant/ditto-subnet/commit/f6a3e2330803ccb836bc33f694a6523aaaac408d))

- **infra**: Add brian@omniaura.ai to platform ssh_users
  ([#1174](https://github.com/ditto-assistant/ditto-subnet/pull/1174),
  [`ae832cc`](https://github.com/ditto-assistant/ditto-subnet/commit/ae832cc87ed834c2210c66b0f042fc467a35af41))

- **infra**: Grant debug operators unconditioned compute.viewer
  ([#1173](https://github.com/ditto-assistant/ditto-subnet/pull/1173),
  [`9f43e07`](https://github.com/ditto-assistant/ditto-subnet/commit/9f43e07a3f2e31cedfe3cc9e834d8cfc7c0617f3))

- **infra**: Grant ssh_users actAs on platform API SA
  ([#1176](https://github.com/ditto-assistant/ditto-subnet/pull/1176),
  [`dc7fcd9`](https://github.com/ditto-assistant/ditto-subnet/commit/dc7fcd9a875ae8a4ecb70c354720ccfe2ab02387))


## v0.112.2 (2026-08-26)

### Bug Fixes

- **dittobench**: Advertise scored-path tool_endpoint on LongMem
  ([#1158](https://github.com/ditto-assistant/ditto-subnet/pull/1158),
  [`3f4f378`](https://github.com/ditto-assistant/ditto-subnet/commit/3f4f378eea2efabfb576b4f359a2f4bb6e22a54b))


## v0.112.1 (2026-08-26)

### Bug Fixes

- **platform**: Omit ledger metrics from inference-routes console
  ([#1161](https://github.com/ditto-assistant/ditto-subnet/pull/1161),
  [`d0580a8`](https://github.com/ditto-assistant/ditto-subnet/commit/d0580a8ccfabde1836736fde078f38236e279dd0))

- **platform**: Reject Cloud Run Kaniko compile failures as docker-build
  ([#1169](https://github.com/ditto-assistant/ditto-subnet/pull/1169),
  [`703c300`](https://github.com/ditto-assistant/ditto-subnet/commit/703c30087bb7a112552b2528c53abab565199ad4))


## v0.112.0 (2026-08-26)

### Bug Fixes

- **infra**: Grant Brian IAP via project IAM the apply SA can write
  ([#1165](https://github.com/ditto-assistant/ditto-subnet/pull/1165),
  [`516cda0`](https://github.com/ditto-assistant/ditto-subnet/commit/516cda077eefff4d02919e6bf1a98b50d6babaeb))

- **platform**: Fall Targon Kaniko deaths through to Cloud Run
  ([#1168](https://github.com/ditto-assistant/ditto-subnet/pull/1168),
  [`d2584a7`](https://github.com/ditto-assistant/ditto-subnet/commit/d2584a75171646ce3fb327dce5bfcb124f3eeb25))

### Features

- **infra**: Grant Brian subnet debug IAM on leftover VMs
  ([#1163](https://github.com/ditto-assistant/ditto-subnet/pull/1163),
  [`c3c6c25`](https://github.com/ditto-assistant/ditto-subnet/commit/c3c6c252155cd126ae39c59fb52494e439f5e097))


## v0.111.0 (2026-08-26)

### Bug Fixes

- **infra**: Allow targeted gcp-platform plans
  ([#1159](https://github.com/ditto-assistant/ditto-subnet/pull/1159),
  [`f0eb8ae`](https://github.com/ditto-assistant/ditto-subnet/commit/f0eb8aeafe0d6ad7de1d25dd06e7030dcb25a70e))

### Features

- **backroom**: Expose benchmark rollout start on MCP
  ([#1160](https://github.com/ditto-assistant/ditto-subnet/pull/1160),
  [`36fdef1`](https://github.com/ditto-assistant/ditto-subnet/commit/36fdef1f2299dce00ac51adee28f0f4fb0bb71a6))


## v0.110.7 (2026-08-25)

### Bug Fixes

- **platform**: Lease confirmation retests to the public-board champion
  ([#1157](https://github.com/ditto-assistant/ditto-subnet/pull/1157),
  [`d40cc78`](https://github.com/ditto-assistant/ditto-subnet/commit/d40cc781afaa970f5a8c20b5e7c8eaa84e7d3bf0))


## v0.110.6 (2026-08-25)

### Bug Fixes

- Shorten scoring lease from 430 to 180 minutes
  ([#1154](https://github.com/ditto-assistant/ditto-subnet/pull/1154),
  [`3c7adf2`](https://github.com/ditto-assistant/ditto-subnet/commit/3c7adf22ae88cb330ed02dcde194b6cdb42a63a8))


## v0.110.5 (2026-08-25)

### Bug Fixes

- **platform**: Admit Cloud Run smoke with embedding sidecar
  ([#1155](https://github.com/ditto-assistant/ditto-subnet/pull/1155),
  [`56e4a40`](https://github.com/ditto-assistant/ditto-subnet/commit/56e4a408cef6058e40c378b6891cf3a70c2c1234))


## v0.110.4 (2026-08-25)

### Bug Fixes

- **platform**: Do not crash Cloud Run smoke on frozen registry_auth
  ([#1153](https://github.com/ditto-assistant/ditto-subnet/pull/1153),
  [`5a797bc`](https://github.com/ditto-assistant/ditto-subnet/commit/5a797bc2ef3eed1d47f9a29656bb368a37e0f414))

- **platform**: Lease confirmation retests to a depth-zero champion
  ([#1152](https://github.com/ditto-assistant/ditto-subnet/pull/1152),
  [`5c2ad9a`](https://github.com/ditto-assistant/ditto-subnet/commit/5c2ad9a0a01b7304ab1645982e3bf84281bddfcb))


## v0.110.3 (2026-08-25)

### Bug Fixes

- **platform**: Smoke on Cloud Run after Targon timeout
  ([#1151](https://github.com/ditto-assistant/ditto-subnet/pull/1151),
  [`4f48a8d`](https://github.com/ditto-assistant/ditto-subnet/commit/4f48a8d4d12d640b4d09fde72a327ac7eb35e31b))


## v0.110.2 (2026-08-25)

### Bug Fixes

- **platform**: Issue LongMem confirmation to the live board king
  ([#1149](https://github.com/ditto-assistant/ditto-subnet/pull/1149),
  [`d358c36`](https://github.com/ditto-assistant/ditto-subnet/commit/d358c365648ba9280bfc9e9d2470b9504c4604ed))


## v0.110.1 (2026-08-25)

### Bug Fixes

- **dittobench**: Fund 48-case LongMem embedding seeds
  ([#1148](https://github.com/ditto-assistant/ditto-subnet/pull/1148),
  [`9f862e0`](https://github.com/ditto-assistant/ditto-subnet/commit/9f862e07b80b44b675c070f6dd5f92c1ac4af121))

- **platform**: Fail-retry targon screens after runtime timeout
  ([#1147](https://github.com/ditto-assistant/ditto-subnet/pull/1147),
  [`47be4ec`](https://github.com/ditto-assistant/ditto-subnet/commit/47be4ecf07a5e1defc3c55ee86c348e7a548247f))


## v0.110.0 (2026-08-24)

### Features

- **dittobench**: Run 48 LongMem cases with live fleet progress
  ([#1141](https://github.com/ditto-assistant/ditto-subnet/pull/1141),
  [`71c0720`](https://github.com/ditto-assistant/ditto-subnet/commit/71c0720b40ed877298e174540af285296a068f73))


## v0.109.2 (2026-08-24)

### Bug Fixes

- **platform**: Raise live chat RPM and wait out lane saturation
  ([#1145](https://github.com/ditto-assistant/ditto-subnet/pull/1145),
  [`b156404`](https://github.com/ditto-assistant/ditto-subnet/commit/b156404aaf4e87d833eb4b0e0cdf06aa8b5a8d73))


## v0.109.1 (2026-08-24)

### Bug Fixes

- **platform**: Show shadow LongMem scores and live fleet progress
  ([#1136](https://github.com/ditto-assistant/ditto-subnet/pull/1136),
  [`73a045a`](https://github.com/ditto-assistant/ditto-subnet/commit/73a045ab43e12acedac62a878ee24829deea64dc))


## v0.109.0 (2026-08-24)

### Documentation

- **skills**: Add miner-comms for Discord replies
  ([#1134](https://github.com/ditto-assistant/ditto-subnet/pull/1134),
  [`b03eba6`](https://github.com/ditto-assistant/ditto-subnet/commit/b03eba68f94756cbb44a347c4e5a6979684222e4))

### Features

- **platform**: Give Backroom audited MCP access to the inference trace archive
  ([#1135](https://github.com/ditto-assistant/ditto-subnet/pull/1135),
  [`89abcf0`](https://github.com/ditto-assistant/ditto-subnet/commit/89abcf0f6a7408f29b447f0ae411fa335e04ccdb))


## v0.108.0 (2026-08-24)

### Bug Fixes

- **platform**: Name public pipeline infrastructure failure codes
  ([#1132](https://github.com/ditto-assistant/ditto-subnet/pull/1132),
  [`d147de5`](https://github.com/ditto-assistant/ditto-subnet/commit/d147de5bf1b05ad71b72d1d604beca0dcd247ac8))

### Features

- **dittobench**: Attribute relayed inference calls to their run and case
  ([#1081](https://github.com/ditto-assistant/ditto-subnet/pull/1081),
  [`faf1690`](https://github.com/ditto-assistant/ditto-subnet/commit/faf1690a259dae8b2661d470d8cb65c23c95b8f1))

- **model-relay**: Capture every brokered inference call to S3 trace buckets
  ([#1079](https://github.com/ditto-assistant/ditto-subnet/pull/1079),
  [`2cc12c0`](https://github.com/ditto-assistant/ditto-subnet/commit/2cc12c00737fb45824b5d2f43d75eb1728011890))


## v0.107.1 (2026-08-23)

### Bug Fixes

- **mine**: Pin harness that connects Turso per overlapping /run
  ([#1125](https://github.com/ditto-assistant/ditto-subnet/pull/1125),
  [`ffe4041`](https://github.com/ditto-assistant/ditto-subnet/commit/ffe4041bc9e682c1e3190e56e9e454986ef8206a))


## v0.107.0 (2026-08-23)

### Bug Fixes

- **screener**: Screen starter-kit with Kaniko identity contract
  ([#1069](https://github.com/ditto-assistant/ditto-subnet/pull/1069),
  [`afd58d3`](https://github.com/ditto-assistant/ditto-subnet/commit/afd58d379756475cb3b21aecd2f3a15d6a5d30e7))

- **screener**: Start capacity controller on drifted systemd units
  ([#1114](https://github.com/ditto-assistant/ditto-subnet/pull/1114),
  [`c04cb23`](https://github.com/ditto-assistant/ditto-subnet/commit/c04cb23d4808156d4e0d01bf7931b3ea21f279f7))

### Features

- **screener**: Run L1 L2 L3 in one Targon rental
  ([#1090](https://github.com/ditto-assistant/ditto-subnet/pull/1090),
  [`46aa715`](https://github.com/ditto-assistant/ditto-subnet/commit/46aa715aaa5d0e8791a284adc0b3ac66c2576462))


## v0.106.6 (2026-08-23)

### Bug Fixes

- **bench**: Keep LongMem mix after enforce ablation completion
  ([#1113](https://github.com/ditto-assistant/ditto-subnet/pull/1113),
  [`63c4779`](https://github.com/ditto-assistant/ditto-subnet/commit/63c4779a490deb30d56bf8243d4d4ea14a576f35))


## v0.106.5 (2026-08-23)

### Bug Fixes

- **bench**: Qualify shadow LongMem after observational ablation drop
  ([#1110](https://github.com/ditto-assistant/ditto-subnet/pull/1110),
  [`303883d`](https://github.com/ditto-assistant/ditto-subnet/commit/303883dfb7b52dcd910ff179860639718058b572))


## v0.106.4 (2026-08-22)

### Bug Fixes

- **platform**: Reap crashed Targon Kaniko replicas immediately
  ([#1111](https://github.com/ditto-assistant/ditto-subnet/pull/1111),
  [`587c7a2`](https://github.com/ditto-assistant/ditto-subnet/commit/587c7a261d5ac884997ff72daefa56d2fba1418b))

### Chores

- **skills**: Apply backroom review bar in /mine before upload
  ([#1106](https://github.com/ditto-assistant/ditto-subnet/pull/1106),
  [`67c9dd8`](https://github.com/ditto-assistant/ditto-subnet/commit/67c9dd8fbc309e707980865819cc663c4727162a))

- **skills**: Document overlapping /run and stack trunk fallback
  ([#1107](https://github.com/ditto-assistant/ditto-subnet/pull/1107),
  [`91b316b`](https://github.com/ditto-assistant/ditto-subnet/commit/91b316b9782bbfe6256247e7489c35aa5215a7ff))

### Documentation

- **skills**: Cover localstack scoring and foundry cheatcodes
  ([#1102](https://github.com/ditto-assistant/ditto-subnet/pull/1102),
  [`02e3df0`](https://github.com/ditto-assistant/ditto-subnet/commit/02e3df08f37be5bc4484dc7aab47d7afed401404))


## v0.106.3 (2026-08-22)

### Bug Fixes

- **platform**: Reopen rejected auto-copy ATH holds
  ([#1104](https://github.com/ditto-assistant/ditto-subnet/pull/1104),
  [`9286773`](https://github.com/ditto-assistant/ditto-subnet/commit/9286773d860078cac9d10ecafc8c769bc17aac99))


## v0.106.2 (2026-08-22)

### Bug Fixes

- **inference**: Raise chat body, max tokens, and request budget
  ([#1094](https://github.com/ditto-assistant/ditto-subnet/pull/1094),
  [`9001e7f`](https://github.com/ditto-assistant/ditto-subnet/commit/9001e7fa601dbae367428581e3f7eea291518981))

- **validator**: Keep KOTH hysteresis with efficiency ranking
  ([#1099](https://github.com/ditto-assistant/ditto-subnet/pull/1099),
  [`9b14bb0`](https://github.com/ditto-assistant/ditto-subnet/commit/9b14bb04b080fbb2820b430c10eca0432c25bcba))


## v0.106.1 (2026-08-22)

### Bug Fixes

- **bench**: Raise confirmation embedding ablation budget
  ([#1100](https://github.com/ditto-assistant/ditto-subnet/pull/1100),
  [`465fa9a`](https://github.com/ditto-assistant/ditto-subnet/commit/465fa9ac931411fb82323d1d608593bb83f7fd46))


## v0.106.0 (2026-08-22)

### Features

- **dashboard**: Lead the overview with a masthead band
  ([#1096](https://github.com/ditto-assistant/ditto-subnet/pull/1096),
  [`aa19e3e`](https://github.com/ditto-assistant/ditto-subnet/commit/aa19e3e5670adad34ea5b352b68c4aec328171ff))


## v0.105.4 (2026-08-22)

### Bug Fixes

- **platform**: Show v11 memory timeline
  ([#1095](https://github.com/ditto-assistant/ditto-subnet/pull/1095),
  [`2a8f4ee`](https://github.com/ditto-assistant/ditto-subnet/commit/2a8f4eef0bbdfcd0f3080d72829ed39691564a01))


## v0.105.3 (2026-08-22)

### Bug Fixes

- **backroom**: Parse unused-reader LongMem zero evidence
  ([#1097](https://github.com/ditto-assistant/ditto-subnet/pull/1097),
  [`bd5d656`](https://github.com/ditto-assistant/ditto-subnet/commit/bd5d6561b2ba9ef06556a6ac98ca4c729b30dad7))


## v0.105.2 (2026-08-22)

### Bug Fixes

- **platform**: Stop comparing ablation evidence and profile contracts
  ([#1084](https://github.com/ditto-assistant/ditto-subnet/pull/1084),
  [`96a5499`](https://github.com/ditto-assistant/ditto-subnet/commit/96a54999b5bc46afe04057e3e2b85f2e6d62caba))


## v0.105.1 (2026-08-22)

### Bug Fixes

- **platform**: Keep lineage time on sub-dethrone improvements
  ([#1092](https://github.com/ditto-assistant/ditto-subnet/pull/1092),
  [`9dac18b`](https://github.com/ditto-assistant/ditto-subnet/commit/9dac18b165e7282fe9e87df642695fd3f1baf0a8))


## v0.105.0 (2026-08-22)

### Features

- **preview**: Add plan validation and secure mock controls
  ([#1067](https://github.com/ditto-assistant/ditto-subnet/pull/1067),
  [`0e8d4d6`](https://github.com/ditto-assistant/ditto-subnet/commit/0e8d4d61daf4c3f5a6ab264b34da39011808a72a))


## v0.104.1 (2026-08-22)

### Bug Fixes

- **platform**: Refuse retry grants for agent-attributable exhaustion
  ([#1046](https://github.com/ditto-assistant/ditto-subnet/pull/1046),
  [`9ed99a4`](https://github.com/ditto-assistant/ditto-subnet/commit/9ed99a4e97e4d7d604dfbfa236e44ad39ca413a7))


## v0.104.0 (2026-08-22)

### Features

- **screener**: Lead on StoryArc, money formatter, world_shape_rule
  ([#1085](https://github.com/ditto-assistant/ditto-subnet/pull/1085),
  [`d43cb96`](https://github.com/ditto-assistant/ditto-subnet/commit/d43cb961b2c69263ee7f3c23fcff98b5811641f1))


## v0.103.3 (2026-08-22)

### Bug Fixes

- **platform**: Answer inference runtime metrics in seconds, not minutes
  ([#1071](https://github.com/ditto-assistant/ditto-subnet/pull/1071),
  [`1bd0a73`](https://github.com/ditto-assistant/ditto-subnet/commit/1bd0a7321a40729ed865d20c8f6dd3197e6beec5))

- **platform**: Mint every tooltip description id from one counter
  ([#1080](https://github.com/ditto-assistant/ditto-subnet/pull/1080),
  [`6d86da6`](https://github.com/ditto-assistant/ditto-subnet/commit/6d86da62e31b6ca71f1ba2d0414ae3f292f56e86))

- **platform**: Reclaim idle retest leases and widen eviction
  ([#1078](https://github.com/ditto-assistant/ditto-subnet/pull/1078),
  [`b4c07a9`](https://github.com/ditto-assistant/ditto-subnet/commit/b4c07a901ec75ad12615749aa3b19faf588c47e1))


## v0.103.2 (2026-08-22)

### Bug Fixes

- **platform**: Persist allowlisted confirmation prepare-report 409s
  ([#1077](https://github.com/ditto-assistant/ditto-subnet/pull/1077),
  [`d8388c9`](https://github.com/ditto-assistant/ditto-subnet/commit/d8388c9e79d153a6f4802a77ccea78d750dd5f85))


## v0.103.1 (2026-08-22)

### Bug Fixes

- **platform**: Rank by lineage time and keep the best score
  ([#1064](https://github.com/ditto-assistant/ditto-subnet/pull/1064),
  [`0bfe31c`](https://github.com/ditto-assistant/ditto-subnet/commit/0bfe31c7f95ed4f5c71614c7a3b524265f8d1754))

### Chores

- **bench**: Remove dead per-case inference gate code
  ([#1059](https://github.com/ditto-assistant/ditto-subnet/pull/1059),
  [`6d983a3`](https://github.com/ditto-assistant/ditto-subnet/commit/6d983a3e539ffc4173c4fe1b64c786bb32ae2874))

### Refactoring

- **platform**: One shared /public/weights resource for the dashboard
  ([#1072](https://github.com/ditto-assistant/ditto-subnet/pull/1072),
  [`95702f2`](https://github.com/ditto-assistant/ditto-subnet/commit/95702f223094ade12969e8743fdfe62fdf776060))


## v0.103.0 (2026-08-21)

### Features

- **platform**: Put the payout countdown in the rail as a live clock
  ([#1068](https://github.com/ditto-assistant/ditto-subnet/pull/1068),
  [`8924bdc`](https://github.com/ditto-assistant/ditto-subnet/commit/8924bdcb57991deada4295b09fac77caacea4e49))


## v0.102.0 (2026-08-21)

### Features

- **platform**: Count down to the next weight fold and emission payout
  ([#1065](https://github.com/ditto-assistant/ditto-subnet/pull/1065),
  [`21a9308`](https://github.com/ditto-assistant/ditto-subnet/commit/21a930843dd3696db4aba6f040fbe057d3d4c162))


## v0.101.0 (2026-08-21)

### Features

- Add /mine skill and default local practice to live bench 11
  ([#1056](https://github.com/ditto-assistant/ditto-subnet/pull/1056),
  [`78d5b1a`](https://github.com/ditto-assistant/ditto-subnet/commit/78d5b1af4b5b446a0afe1dbdf89dbb0955fd9b64))


## v0.100.5 (2026-08-21)

### Bug Fixes

- **platform**: Yield idle retests when a newer family agent needs quorum
  ([#1062](https://github.com/ditto-assistant/ditto-subnet/pull/1062),
  [`9405258`](https://github.com/ditto-assistant/ditto-subnet/commit/9405258a5952594bc56dea3a0cc3115ac8a49307))


## v0.100.4 (2026-08-21)

### Bug Fixes

- **bench**: Accept 430-minute inference grant activations
  ([#1058](https://github.com/ditto-assistant/ditto-subnet/pull/1058),
  [`89d89a0`](https://github.com/ditto-assistant/ditto-subnet/commit/89d89a0cca9a5d63cbe209043ccb793de26d9b71))


## v0.100.3 (2026-08-21)

### Bug Fixes

- **bench**: Session-scoped v10+ tool provenance under concurrent /run
  ([#1054](https://github.com/ditto-assistant/ditto-subnet/pull/1054),
  [`ebf8556`](https://github.com/ditto-assistant/ditto-subnet/commit/ebf855639555b1245f93d20b163d61a9a3dcc880))


## v0.100.2 (2026-08-21)

### Bug Fixes

- **platform**: Pair renamed copy-review source diffs
  ([#1052](https://github.com/ditto-assistant/ditto-subnet/pull/1052),
  [`c5a7062`](https://github.com/ditto-assistant/ditto-subnet/commit/c5a70626d491e190b1bf157258ffe72c2767b8e6))


## v0.100.1 (2026-08-21)

### Bug Fixes

- **platform**: Require padding growth for copy-gate containment
  ([#1049](https://github.com/ditto-assistant/ditto-subnet/pull/1049),
  [`5d36fa4`](https://github.com/ditto-assistant/ditto-subnet/commit/5d36fa4541000ca45236386530e20b46d3be6712))


## v0.100.0 (2026-08-21)

### Bug Fixes

- Raise serial scoring timeout to 400 minutes
  ([#1048](https://github.com/ditto-assistant/ditto-subnet/pull/1048),
  [`4a2bcdf`](https://github.com/ditto-assistant/ditto-subnet/commit/4a2bcdfd8b3be5d5bf2881774f5bc5b0129603b2))

- **platform**: Require 15% residual growth for resubmission containment
  ([#1047](https://github.com/ditto-assistant/ditto-subnet/pull/1047),
  [`58bb118`](https://github.com/ditto-assistant/ditto-subnet/commit/58bb118f618f78a65841159423ade1ffb9c62b55))

### Features

- **bench**: Overlap /run without per-case inference URLs
  ([#1040](https://github.com/ditto-assistant/ditto-subnet/pull/1040),
  [`bea9a44`](https://github.com/ditto-assistant/ditto-subnet/commit/bea9a44e88e3363e9bfa62c03e7ba743b69a09fe))


## v0.99.9 (2026-08-21)

### Bug Fixes

- Raise chat body cap and pin platform middle-out
  ([#1045](https://github.com/ditto-assistant/ditto-subnet/pull/1045),
  [`c78e1a0`](https://github.com/ditto-assistant/ditto-subnet/commit/c78e1a0f844413c4a7e426c25b883df1885be826))

- Raise serial bench-11 scoring timeout to 150 minutes
  ([#1042](https://github.com/ditto-assistant/ditto-subnet/pull/1042),
  [`1e1c332`](https://github.com/ditto-assistant/ditto-subnet/commit/1e1c3323175b6c5039207a9b0e0961cd76d906ee))

- **model-relay**: Wait for postgres in gen-schema under set -e
  ([#1044](https://github.com/ditto-assistant/ditto-subnet/pull/1044),
  [`ed77b30`](https://github.com/ditto-assistant/ditto-subnet/commit/ed77b3092eb9b72ddf4cf4ec6dbc5f836140e91c))

### Chores

- Merge gcloud DB and Targon debug skills
  ([#1037](https://github.com/ditto-assistant/ditto-subnet/pull/1037),
  [`e390295`](https://github.com/ditto-assistant/ditto-subnet/commit/e39029533b239eecb2a0ecf1d8a586b2d8762817))


## v0.99.8 (2026-08-21)

### Bug Fixes

- **dittobench**: Accept kaniko docker-save config names
  ([#1036](https://github.com/ditto-assistant/ditto-subnet/pull/1036),
  [`36e8d11`](https://github.com/ditto-assistant/ditto-subnet/commit/36e8d11beb517880bad7fa841ce7aa314d7e8bea))


## v0.99.7 (2026-08-21)

### Bug Fixes

- **platform**: Deploy builder that parses kaniko tar config names
  ([#1035](https://github.com/ditto-assistant/ditto-subnet/pull/1035),
  [`70a9af0`](https://github.com/ditto-assistant/ditto-subnet/commit/70a9af05db62aa54d85746fee5024e17bee4a6f4))

### Chores

- Add read-only Targon rental logs debug skill
  ([#1033](https://github.com/ditto-assistant/ditto-subnet/pull/1033),
  [`3850a48`](https://github.com/ditto-assistant/ditto-subnet/commit/3850a48e415a93ec615222e3551e80e7563a0915))


## v0.99.6 (2026-08-21)

### Bug Fixes

- **screener**: Parse kaniko tar config digest names
  ([#1032](https://github.com/ditto-assistant/ditto-subnet/pull/1032),
  [`f872ef7`](https://github.com/ditto-assistant/ditto-subnet/commit/f872ef7079d4bc6e25ae04b89cacc13703fac579))


## v0.99.5 (2026-08-21)

### Bug Fixes

- **platform**: Stop leftover GCE builder claiming miner Kaniko
  ([#1031](https://github.com/ditto-assistant/ditto-subnet/pull/1031),
  [`5c1a50e`](https://github.com/ditto-assistant/ditto-subnet/commit/5c1a50e42732611799d7ff5842b2026015227908))

### Chores

- **deps**: Bump solid-js from 1.9.14 to 1.9.15 in /apps/platform/dashboard
  ([#1023](https://github.com/ditto-assistant/ditto-subnet/pull/1023),
  [`063374c`](https://github.com/ditto-assistant/ditto-subnet/commit/063374c830a639893f220d58ff43dca46ecfddab))


## v0.99.4 (2026-08-21)

### Bug Fixes

- **platform**: Treat nested Cloud Run execution refs as running
  ([#1029](https://github.com/ditto-assistant/ditto-subnet/pull/1029),
  [`b005872`](https://github.com/ditto-assistant/ditto-subnet/commit/b005872aeba7f9346dcfa58fb0320a937f7009b3))


## v0.99.3 (2026-08-21)

### Bug Fixes

- **platform**: Reap stale Targon inflight and detect Cloud Run running
  ([#1028](https://github.com/ditto-assistant/ditto-subnet/pull/1028),
  [`011b730`](https://github.com/ditto-assistant/ditto-subnet/commit/011b730b023e928b9d3e3bb379027a17f332ab4f))


## v0.99.2 (2026-08-21)

### Bug Fixes

- **platform**: Do not launch a stale Kaniko builder
  ([#1027](https://github.com/ditto-assistant/ditto-subnet/pull/1027),
  [`3893074`](https://github.com/ditto-assistant/ditto-subnet/commit/3893074955c36be32332ac130243c84f542fa9c6))


## v0.99.1 (2026-08-21)

### Bug Fixes

- **platform**: Pin Kaniko screened ids from tar config
  ([#1016](https://github.com/ditto-assistant/ditto-subnet/pull/1016),
  [`665ef8c`](https://github.com/ditto-assistant/ditto-subnet/commit/665ef8c33ccf32b30a21759fdacd48c972d7aa44))


## v0.99.0 (2026-08-21)

### Features

- **dittobench**: Accept gzip docker-save screened images
  ([#1012](https://github.com/ditto-assistant/ditto-subnet/pull/1012),
  [`d99cf29`](https://github.com/ditto-assistant/ditto-subnet/commit/d99cf290199f9d59cc7e0ed89f332aeb9c45f60b))


## v0.98.10 (2026-08-20)

### Bug Fixes

- **platform**: Pin Kaniko images from registry config digest
  ([#1011](https://github.com/ditto-assistant/ditto-subnet/pull/1011),
  [`d8f529a`](https://github.com/ditto-assistant/ditto-subnet/commit/d8f529a293110a813fac193318da37f92df3fd70))


## v0.98.9 (2026-08-20)

### Bug Fixes

- **dashboard**: Type confirmation progress across evidence versions
  ([#979](https://github.com/ditto-assistant/ditto-subnet/pull/979),
  [`86bcc5e`](https://github.com/ditto-assistant/ditto-subnet/commit/86bcc5e6ec6fec286e5315c002a7bc5bfb144ed3))

- **scoring**: Ingest v12 gates without false-zeroing gaps
  ([#976](https://github.com/ditto-assistant/ditto-subnet/pull/976),
  [`b5cc8c7`](https://github.com/ditto-assistant/ditto-subnet/commit/b5cc8c760632380175db2fadcb00a57bf52c34fc))

- **scoring**: Keep v12 answer-stuffing default on penalize
  ([#977](https://github.com/ditto-assistant/ditto-subnet/pull/977),
  [`44f6321`](https://github.com/ditto-assistant/ditto-subnet/commit/44f6321b47aa3a37cf16b5818aa2291ec895138e))

### Chores

- **tests**: Pin v12 on capability and confirmation regressions
  ([#978](https://github.com/ditto-assistant/ditto-subnet/pull/978),
  [`9f1d283`](https://github.com/ditto-assistant/ditto-subnet/commit/9f1d28366ee938ac99ea2337b9527aa0dbeec273))


## v0.98.8 (2026-08-20)

### Bug Fixes

- **platform**: Pin Targon screened images to config digest
  ([#1010](https://github.com/ditto-assistant/ditto-subnet/pull/1010),
  [`b223c7f`](https://github.com/ditto-assistant/ditto-subnet/commit/b223c7f54fd2c254f675d3a3ee387912903e415a))


## v0.98.7 (2026-08-20)

### Bug Fixes

- **dittobench**: Accept Kaniko attempt-scoped screened image tags
  ([#1008](https://github.com/ditto-assistant/ditto-subnet/pull/1008),
  [`b5e800f`](https://github.com/ditto-assistant/ditto-subnet/commit/b5e800fb5959a9174c2f9d55ecb1c3137fdb0a77))


## v0.98.6 (2026-08-20)

### Bug Fixes

- **platform**: Unstick screens after Cloud Run builder image misses
  ([#1006](https://github.com/ditto-assistant/ditto-subnet/pull/1006),
  [`6f202a2`](https://github.com/ditto-assistant/ditto-subnet/commit/6f202a21062b6aa8b9ed9cc67273e5439985cbc8))


## v0.98.5 (2026-08-20)

### Bug Fixes

- **inference**: Forward assistant reasoning traces to OpenRouter
  ([#1005](https://github.com/ditto-assistant/ditto-subnet/pull/1005),
  [`b110ea9`](https://github.com/ditto-assistant/ditto-subnet/commit/b110ea972eaca5df3afef43d11c422a55ce3face))


## v0.98.4 (2026-08-20)

### Bug Fixes

- **inference**: Heal conflicting reasoning aliases before OpenRouter
  ([#1001](https://github.com/ditto-assistant/ditto-subnet/pull/1001),
  [`2d497b0`](https://github.com/ditto-assistant/ditto-subnet/commit/2d497b015c9b31adbeb4806658edbaf63ef9aa4e))


## v0.98.3 (2026-08-20)

### Bug Fixes

- **platform**: Pin dataset after Targon smoke finalize
  ([#1003](https://github.com/ditto-assistant/ditto-subnet/pull/1003),
  [`ead48d1`](https://github.com/ditto-assistant/ditto-subnet/commit/ead48d16b703a7487fe2339c9f2722e0608ecb28))


## v0.98.2 (2026-08-20)

### Bug Fixes

- **platform**: Count inference tokens from receipts not estimates
  ([#999](https://github.com/ditto-assistant/ditto-subnet/pull/999),
  [`aa5b6e9`](https://github.com/ditto-assistant/ditto-subnet/commit/aa5b6e90abecc5df6464f1e055ee151e4e44abe5))


## v0.98.1 (2026-08-20)

### Bug Fixes

- **platform**: Accept gcp on public operations snapshot
  ([#1000](https://github.com/ditto-assistant/ditto-subnet/pull/1000),
  [`9164b7b`](https://github.com/ditto-assistant/ditto-subnet/commit/9164b7b4ddba17abae2c21fd7c49105ec2e2b871))


## v0.98.0 (2026-08-20)

### Features

- **backroom**: Expose validator fleet identity on MCP
  ([#997](https://github.com/ditto-assistant/ditto-subnet/pull/997),
  [`2ff6c44`](https://github.com/ditto-assistant/ditto-subnet/commit/2ff6c443eeeaa7e1ccb5205efea095c0252c3991))


## v0.97.0 (2026-08-20)

### Bug Fixes

- **platform**: Cap concurrent Targon screening rentals at 10
  ([#998](https://github.com/ditto-assistant/ditto-subnet/pull/998),
  [`7455df5`](https://github.com/ditto-assistant/ditto-subnet/commit/7455df5cd69d8bd32638d20eec0bbb7ecfdcc691))

- **platform**: Time out Targon rentals that never leave provisioning
  ([#991](https://github.com/ditto-assistant/ditto-subnet/pull/991),
  [`2771fcc`](https://github.com/ditto-assistant/ditto-subnet/commit/2771fcc4b2363e6d6df109d4520a7f7d3c94820f))

### Features

- **platform**: Fall back Targon screening lanes to Cloud Run
  ([#994](https://github.com/ditto-assistant/ditto-subnet/pull/994),
  [`2abbb5e`](https://github.com/ditto-assistant/ditto-subnet/commit/2abbb5e59d12ac8b6c8144e0046921635ffcf6b4))


## v0.96.6 (2026-08-20)

### Bug Fixes

- **platform**: Drop OpenRouter routing extras instead of 400ing them
  ([#992](https://github.com/ditto-assistant/ditto-subnet/pull/992),
  [`9d9a288`](https://github.com/ditto-assistant/ditto-subnet/commit/9d9a288e3bbd301c91f29d860a3f205098dcbe75))


## v0.96.5 (2026-08-20)

### Bug Fixes

- **platform**: Publish inference_request_rejected on the public pipeline
  ([#990](https://github.com/ditto-assistant/ditto-subnet/pull/990),
  [`5e41756`](https://github.com/ditto-assistant/ditto-subnet/commit/5e41756c53c2b0dcb5fcc97eef0be123dae42bd2))

- **platform**: Unfurl page-specific OG for dashboard shares
  ([#971](https://github.com/ditto-assistant/ditto-subnet/pull/971),
  [`f02bc7e`](https://github.com/ditto-assistant/ditto-subnet/commit/f02bc7ebdef5b22a153d69a6d69c269656abd40b))


## v0.96.4 (2026-08-20)

### Bug Fixes

- **platform**: Keep Targon Kaniko leases alive on a leftover pet
  ([#987](https://github.com/ditto-assistant/ditto-subnet/pull/987),
  [`73f296f`](https://github.com/ditto-assistant/ditto-subnet/commit/73f296f3b380ab561d087d30bcb727360ecbb1f6))


## v0.96.3 (2026-08-20)

### Bug Fixes

- **dittobench-api**: Fail-closed missing budget evidence
  ([#989](https://github.com/ditto-assistant/ditto-subnet/pull/989),
  [`f39af32`](https://github.com/ditto-assistant/ditto-subnet/commit/f39af32d193639ab431ca16cb01370371abc30b2))


## v0.96.2 (2026-08-20)

### Bug Fixes

- **platform**: Delete Kaniko rentals on complete
  ([#988](https://github.com/ditto-assistant/ditto-subnet/pull/988),
  [`4ca9eeb`](https://github.com/ditto-assistant/ditto-subnet/commit/4ca9eeb4d052ddfa9c456baa0342f62fe9b6444a))


## v0.96.1 (2026-08-19)

### Bug Fixes

- **validator**: Default managed stack auto-update on
  ([#975](https://github.com/ditto-assistant/ditto-subnet/pull/975),
  [`fa4692a`](https://github.com/ditto-assistant/ditto-subnet/commit/fa4692a73f1520dc86744911c1b4d3aa169f2129))


## v0.96.0 (2026-08-19)

### Bug Fixes

- **platform**: Align LongMem confirmation reader with scoring LLM relay
  ([#963](https://github.com/ditto-assistant/ditto-subnet/pull/963),
  [`f2e8991`](https://github.com/ditto-assistant/ditto-subnet/commit/f2e899107df7a39d71e3046806a7447a8c87da6a))

### Chores

- **ci**: Accept every conventional type the release tool parses
  ([#962](https://github.com/ditto-assistant/ditto-subnet/pull/962),
  [`b4b0f8d`](https://github.com/ditto-assistant/ditto-subnet/commit/b4b0f8d85117a1cc04e2f212b69dc9e65bad8701))

### Features

- **screener**: Remove nested Docker Targon worker lane
  ([#964](https://github.com/ditto-assistant/ditto-subnet/pull/964),
  [`4044afd`](https://github.com/ditto-assistant/ditto-subnet/commit/4044afdd567652ef77f3ba98b2ce4c5516440839))


## v0.95.3 (2026-08-19)

### Bug Fixes

- **scoring**: Do not charge miners for impossible allowance declines
  ([#972](https://github.com/ditto-assistant/ditto-subnet/pull/972),
  [`fe871fd`](https://github.com/ditto-assistant/ditto-subnet/commit/fe871fdff38d24d3a3db2bac1a86330ed3e4b966))


## v0.95.2 (2026-08-19)

### Bug Fixes

- **dashboard**: Show the KOTH crown clock not tarball upload
  ([#969](https://github.com/ditto-assistant/ditto-subnet/pull/969),
  [`edf8d91`](https://github.com/ditto-assistant/ditto-subnet/commit/edf8d915147f9020416a291e532d4e6124556398))

- **dashboard**: Stop comparing score column to dethrone bar
  ([#967](https://github.com/ditto-assistant/ditto-subnet/pull/967),
  [`12405eb`](https://github.com/ditto-assistant/ditto-subnet/commit/12405eb67db94ff7f04cf79a17f67fab9d8d6175))


## v0.95.1 (2026-08-19)

### Bug Fixes

- **platform**: Delete finished Targon one-shot rentals
  ([#968](https://github.com/ditto-assistant/ditto-subnet/pull/968),
  [`e8b388f`](https://github.com/ditto-assistant/ditto-subnet/commit/e8b388f501b5ef8ab683c0b3cd89eb0c5159225c))


## v0.95.0 (2026-08-19)

### Bug Fixes

- **dittobench**: Run v9 LongMem instrument against v11 subjects
  ([#959](https://github.com/ditto-assistant/ditto-subnet/pull/959),
  [`2496e19`](https://github.com/ditto-assistant/ditto-subnet/commit/2496e19af304043b9db748d370b1a3ebc57c0045))

- **platform**: Cooldown LongMem reissue after a failed ticket
  ([#960](https://github.com/ditto-assistant/ditto-subnet/pull/960),
  [`ca5375a`](https://github.com/ditto-assistant/ditto-subnet/commit/ca5375a19f1bc88b1d551c0f10bc68cc87719302))

### Features

- **platform**: Attest Targon screens without a GCE screener fleet
  ([#956](https://github.com/ditto-assistant/ditto-subnet/pull/956),
  [`bf265ef`](https://github.com/ditto-assistant/ditto-subnet/commit/bf265ef09b894ebdf2c988e4ae436f153d3dd0b2))

- **screener**: Screen Targon health and L1 without nested Docker
  ([#955](https://github.com/ditto-assistant/ditto-subnet/pull/955),
  [`1befa6d`](https://github.com/ditto-assistant/ditto-subnet/commit/1befa6db20e3c9cba62d12a306e1ac8d7337143f))


## v0.94.0 (2026-08-19)

### Chores

- **ci**: Verify generated confirmation release assets
  ([#953](https://github.com/ditto-assistant/ditto-subnet/pull/953),
  [`5cfce4a`](https://github.com/ditto-assistant/ditto-subnet/commit/5cfce4aa560df13b8e861d81d587347da6f1538d))

### Features

- **screener**: Name L2 failures for Backroom diagnosis
  ([#958](https://github.com/ditto-assistant/ditto-subnet/pull/958),
  [`1aac69a`](https://github.com/ditto-assistant/ditto-subnet/commit/1aac69a397713ed7bb3ad7423c22dbd448557767))


## v0.93.0 (2026-08-19)

### Chores

- **agents**: Record LongMem confirmation as a permanent bench dimension
  ([#950](https://github.com/ditto-assistant/ditto-subnet/pull/950),
  [`08b63a4`](https://github.com/ditto-assistant/ditto-subnet/commit/08b63a4df6268875a6cbab5fc84866640b1d2943))

- **ci**: Accept docs and perf PR titles
  ([#951](https://github.com/ditto-assistant/ditto-subnet/pull/951),
  [`ffa6e13`](https://github.com/ditto-assistant/ditto-subnet/commit/ffa6e137ad917db49d98b7b70d10cd5c430e23fd))

- **contract**: Leave one generator for the wire-contract goldens
  ([#949](https://github.com/ditto-assistant/ditto-subnet/pull/949),
  [`9844d4d`](https://github.com/ditto-assistant/ditto-subnet/commit/9844d4dc0da3973413f4c8baf53d86ace0f71245))

### Features

- **screener**: Put L1 model and timeout on Backroom settings
  ([#952](https://github.com/ditto-assistant/ditto-subnet/pull/952),
  [`e9c7a9b`](https://github.com/ditto-assistant/ditto-subnet/commit/e9c7a9b355bec4460056b21d1d50025fe1ffaa77))

- **screener**: Raise L1 source-review budget to 200 steps / 8MB
  ([#948](https://github.com/ditto-assistant/ditto-subnet/pull/948),
  [`93e6f01`](https://github.com/ditto-assistant/ditto-subnet/commit/93e6f011f39b3c425bed45458e32e079b54ea782))


## v0.92.2 (2026-08-19)

### Bug Fixes

- **confirmation**: Run the LongMem confirmation lane at every supported epoch
  ([#946](https://github.com/ditto-assistant/ditto-subnet/pull/946),
  [`a395f55`](https://github.com/ditto-assistant/ditto-subnet/commit/a395f55b0e7d63828f3f8d98898c71b42251709d))


## v0.92.1 (2026-08-19)

### Bug Fixes

- **backroom**: Extend staff session lifetime to 7 days
  ([#947](https://github.com/ditto-assistant/ditto-subnet/pull/947),
  [`d605e19`](https://github.com/ditto-assistant/ditto-subnet/commit/d605e1965775094532d64878db8fe7d519dcfa46))


## v0.92.0 (2026-08-19)

### Features

- Make harness stderr obtainable by operators and miners
  ([#778](https://github.com/ditto-assistant/ditto-subnet/pull/778),
  [`6474b6a`](https://github.com/ditto-assistant/ditto-subnet/commit/6474b6a2c05be7ad27221a3d4d4b9b1bab35f9e9))


## v0.91.1 (2026-08-19)

### Bug Fixes

- **dittobench**: Mask every private bench version on the harness wire
  ([#945](https://github.com/ditto-assistant/ditto-subnet/pull/945),
  [`01b1c2a`](https://github.com/ditto-assistant/ditto-subnet/commit/01b1c2af06257ee3893e3459ac0851b787cf4728))


## v0.91.0 (2026-08-19)

### Features

- **platform**: Show why the KOTH crown did not move
  ([#940](https://github.com/ditto-assistant/ditto-subnet/pull/940),
  [`44c395c`](https://github.com/ditto-assistant/ditto-subnet/commit/44c395c66b9001d5d401596954ade1596a14c2e8))


## v0.90.0 (2026-08-19)

### Bug Fixes

- **validator**: Honor supports_confirmation on LongMem leases
  ([#941](https://github.com/ditto-assistant/ditto-subnet/pull/941),
  [`804d159`](https://github.com/ditto-assistant/ditto-subnet/commit/804d1597a0ea45c41c829e4d048fb971bba3be2c))

### Features

- **dashboard**: Make miner avatars read as identity, not favicons
  ([#943](https://github.com/ditto-assistant/ditto-subnet/pull/943),
  [`79b0aee`](https://github.com/ditto-assistant/ditto-subnet/commit/79b0aeed836943f01ddfd31e5b74e003010322f9))

- **dashboard**: Make the miner panel scannable instead of a wall
  ([#944](https://github.com/ditto-assistant/ditto-subnet/pull/944),
  [`9d5ab72`](https://github.com/ditto-assistant/ditto-subnet/commit/9d5ab729288871c87dcab7b26cecfba932cf6d62))

- **platform**: Ship the bench v12 contract as an operator rollout target
  ([#942](https://github.com/ditto-assistant/ditto-subnet/pull/942),
  [`6166a41`](https://github.com/ditto-assistant/ditto-subnet/commit/6166a412082fce8fa67a61737f0a5d10f65346f8))


## v0.89.0 (2026-08-18)

### Bug Fixes

- **validator**: Verify confirmation receipts on every confirmation bench version
  ([#938](https://github.com/ditto-assistant/ditto-subnet/pull/938),
  [`c915961`](https://github.com/ditto-assistant/ditto-subnet/commit/c91596145157cc5de2a545b9d7811aee059e1f95))

### Features

- **platform**: Use miner avatars as Open Graph images
  ([#939](https://github.com/ditto-assistant/ditto-subnet/pull/939),
  [`d80dc94`](https://github.com/ditto-assistant/ditto-subnet/commit/d80dc94aae9ab7ac756b8dcc6342c3c53cd8428a))


## v0.88.2 (2026-08-18)

### Bug Fixes

- **screener**: Isolate skopeo home under ProtectHome
  ([#937](https://github.com/ditto-assistant/ditto-subnet/pull/937),
  [`41d3725`](https://github.com/ditto-assistant/ditto-subnet/commit/41d372513897ae9e84b86eb30970f4127bdfcdb6))


## v0.88.1 (2026-08-18)

### Bug Fixes

- **confirmation**: Carry bench_version through the confirmation wire
  ([#934](https://github.com/ditto-assistant/ditto-subnet/pull/934),
  [`de1b8e4`](https://github.com/ditto-assistant/ditto-subnet/commit/de1b8e4fddfb0356a972eb41fe0b5f38220ad142))

### Chores

- **agents**: Add Dependabot security-review skill
  ([#933](https://github.com/ditto-assistant/ditto-subnet/pull/933),
  [`2cc5ed6`](https://github.com/ditto-assistant/ditto-subnet/commit/2cc5ed6a367f35d513d4d6b3bd5d849b15dad315))

- **agents**: Add the bench-version-bump skill
  ([#935](https://github.com/ditto-assistant/ditto-subnet/pull/935),
  [`d108d1a`](https://github.com/ditto-assistant/ditto-subnet/commit/d108d1abc289f2c42e1444d8ac7f421752bf83bf))

- **deps**: Bump golang.org/x/sys from 0.29.0 to 0.47.0 in /services/dittobench-api
  ([#725](https://github.com/ditto-assistant/ditto-subnet/pull/725),
  [`89f1b7a`](https://github.com/ditto-assistant/ditto-subnet/commit/89f1b7a177575b4f93659015ee882ea64e4dc06a))

- **deps**: Bump hashicorp/setup-packer in the actions group
  ([#732](https://github.com/ditto-assistant/ditto-subnet/pull/732),
  [`eeadf3c`](https://github.com/ditto-assistant/ditto-subnet/commit/eeadf3cd2c4a54019db57dbccd87a09010a91120))

- **deps**: Bump numpy from 2.5.1 to 2.5.2 in /workers/screener
  ([#728](https://github.com/ditto-assistant/ditto-subnet/pull/728),
  [`50c6190`](https://github.com/ditto-assistant/ditto-subnet/commit/50c61904e44246932cb40590bace16b19c3df100))

- **deps-dev**: Bump @testing-library/jest-dom from 7.0.0 to 7.0.1 in /apps/platform/dashboard
  ([#730](https://github.com/ditto-assistant/ditto-subnet/pull/730),
  [`6911f7b`](https://github.com/ditto-assistant/ditto-subnet/commit/6911f7ba7ea051608c1ae34b02275af75700b6a4))

- **deps-dev**: Bump @types/node from 26.1.2 to 26.2.0 in /apps/platform/dashboard
  ([#731](https://github.com/ditto-assistant/ditto-subnet/pull/731),
  [`9fc1ac3`](https://github.com/ditto-assistant/ditto-subnet/commit/9fc1ac3a6ffbb442c2e7f9416c01c6c66ffe934a))

- **deps-dev**: Bump oxlint from 1.77.0 to 1.78.0 in /apps/platform/dashboard
  ([#726](https://github.com/ditto-assistant/ditto-subnet/pull/726),
  [`0f409b6`](https://github.com/ditto-assistant/ditto-subnet/commit/0f409b679a759e2c0720a7cd35a7a8a2a8a2f3fb))

- **deps-dev**: Bump vite from 8.2.0 to 8.2.1 in /apps/platform/dashboard
  ([#729](https://github.com/ditto-assistant/ditto-subnet/pull/729),
  [`9913a4e`](https://github.com/ditto-assistant/ditto-subnet/commit/9913a4ee031c2a48369829fbfd48bd59f4eac34a))

- **deps-dev**: Update setuptools requirement from <84,>=77 to >=77,<85 in
  /services/dittobench-api/integrations/hermes
  ([#727](https://github.com/ditto-assistant/ditto-subnet/pull/727),
  [`94ea5fd`](https://github.com/ditto-assistant/ditto-subnet/commit/94ea5fdefee485272b252f4bf1dc87ef280bc0d9))


## v0.88.0 (2026-08-18)

### Bug Fixes

- **platform**: Keep a live bench live after holds and rejects
  ([#931](https://github.com/ditto-assistant/ditto-subnet/pull/931),
  [`cba27c8`](https://github.com/ditto-assistant/ditto-subnet/commit/cba27c82f375bef5b8cfe7f244c2e6875dba76ff))

- **platform**: Name same-miner rejected ancestors in hold notice
  ([#928](https://github.com/ditto-assistant/ditto-subnet/pull/928),
  [`6236a12`](https://github.com/ditto-assistant/ditto-subnet/commit/6236a1286ec6226a0a8c8af33541ab3e773901dd))

- **screener**: Use dest-authfile on skopeo 1.18
  ([#930](https://github.com/ditto-assistant/ditto-subnet/pull/930),
  [`729987e`](https://github.com/ditto-assistant/ditto-subnet/commit/729987e81c46caf0eb27493d3dbe8c860bb8aa99))

### Features

- **bench**: Private bench v12 — layered anti-emulation defense
  ([#932](https://github.com/ditto-assistant/ditto-subnet/pull/932),
  [`95fc780`](https://github.com/ditto-assistant/ditto-subnet/commit/95fc78076638c750851c72478f011d3f4da2f4ee))

- **screener**: Give L1 Luna a real budget and Backroom MCP debug
  ([#908](https://github.com/ditto-assistant/ditto-subnet/pull/908),
  [`afd3be5`](https://github.com/ditto-assistant/ditto-subnet/commit/afd3be5370e2ba40e9d9eab032abb331207cb4d5))


## v0.87.1 (2026-08-18)

### Bug Fixes

- **platform**: Close leftover rollouts without muting current retests
  ([#929](https://github.com/ditto-assistant/ditto-subnet/pull/929),
  [`77564ac`](https://github.com/ditto-assistant/ditto-subnet/commit/77564ac0d9c0e6b676f77893c308359f8a9fb2b0))


## v0.87.0 (2026-08-18)

### Features

- **miner-cli**: Print uvx login and pick local wallets
  ([#920](https://github.com/ditto-assistant/ditto-subnet/pull/920),
  [`4d17b28`](https://github.com/ditto-assistant/ditto-subnet/commit/4d17b285e37229673df1b16a8b198e34c5d14e97))


## v0.86.1 (2026-08-18)

### Bug Fixes

- **screener**: Format runtime smoke or-handled wrap
  ([#927](https://github.com/ditto-assistant/ditto-subnet/pull/927),
  [`3666147`](https://github.com/ditto-assistant/ditto-subnet/commit/36661476fd970e60208d951a4cfe7f367d945aef))

- **screener**: Smoke miner archives after gce consume
  ([#926](https://github.com/ditto-assistant/ditto-subnet/pull/926),
  [`71f7bee`](https://github.com/ditto-assistant/ditto-subnet/commit/71f7bee839e12c5e97aabac186feae2947d3869a))


## v0.86.0 (2026-08-18)

### Bug Fixes

- **screener**: Pass artifact registry creds on skopeo stdin
  ([#923](https://github.com/ditto-assistant/ditto-subnet/pull/923),
  [`a1d7046`](https://github.com/ditto-assistant/ditto-subnet/commit/a1d70463eebeed994f61d5b2d570f26c9b6fe993))

- **screener**: Wrap skopeo inspect failures for mypy
  ([#925](https://github.com/ditto-assistant/ditto-subnet/pull/925),
  [`854e327`](https://github.com/ditto-assistant/ditto-subnet/commit/854e3275bbe099ec31f9431982b3dcffc7a7a282))

### Chores

- **agents**: Combine backroom review skills
  ([#919](https://github.com/ditto-assistant/ditto-subnet/pull/919),
  [`a0b9e07`](https://github.com/ditto-assistant/ditto-subnet/commit/a0b9e0795692cb29cd53934177ddaf9b17c86c14))

### Features

- **platform**: Add dashboard SEO with 30s crawler snapshots
  ([#924](https://github.com/ditto-assistant/ditto-subnet/pull/924),
  [`6d33688`](https://github.com/ditto-assistant/ditto-subnet/commit/6d336887f42eaee6bfef20cc2c5c9c05720f4762))

- **screener**: Teach L1 the v12 two-limb and engine bar
  ([#918](https://github.com/ditto-assistant/ditto-subnet/pull/918),
  [`28b6303`](https://github.com/ditto-assistant/ditto-subnet/commit/28b63036f332d59abd4b2ffff2b0b5e52ca685cb))


## v0.85.0 (2026-08-18)

### Bug Fixes

- **dashboard**: Stop scored agent cards from remounting case rows
  ([#917](https://github.com/ditto-assistant/ditto-subnet/pull/917),
  [`42bc1bf`](https://github.com/ditto-assistant/ditto-subnet/commit/42bc1bfad1c3c605f5711a8031365e0cb3a5de90))

- **platform**: Keep desired bench live after a reject
  ([#913](https://github.com/ditto-assistant/ditto-subnet/pull/913),
  [`da494a3`](https://github.com/ditto-assistant/ditto-subnet/commit/da494a3990a1c0866d2d0b6441136972a5f3d4b4))

- **screener**: Promote kaniko oci tars for targon smoke
  ([#912](https://github.com/ditto-assistant/ditto-subnet/pull/912),
  [`b7c9a82`](https://github.com/ditto-assistant/ditto-subnet/commit/b7c9a82f58314e411f3ac04353b41655280e38b9))

- **screener**: Treat targon delete 137 bounce as torn down
  ([#910](https://github.com/ditto-assistant/ditto-subnet/pull/910),
  [`aad1272`](https://github.com/ditto-assistant/ditto-subnet/commit/aad1272415d63fc2c3c4499f7846482aa9754c46))

### Chores

- **docs**: Document handle claims and avatars in the miner CLI
  ([#915](https://github.com/ditto-assistant/ditto-subnet/pull/915),
  [`81f0b97`](https://github.com/ditto-assistant/ditto-subnet/commit/81f0b97f7def4900ba286d3a5b5cec0b68453879))

- **screener**: Flatten post-delete 404 return
  ([#916](https://github.com/ditto-assistant/ditto-subnet/pull/916),
  [`b509fc8`](https://github.com/ditto-assistant/ditto-subnet/commit/b509fc889a19beeeb06a48f7fe4e3fe9afd7d8ab))

- **screener**: Format kaniko oci archive helper
  ([#914](https://github.com/ditto-assistant/ditto-subnet/pull/914),
  [`4cae012`](https://github.com/ditto-assistant/ditto-subnet/commit/4cae012fede4bce568d4642f069127941e8d4cd4))

- **screener**: Format targon 137 teardown test
  ([#911](https://github.com/ditto-assistant/ditto-subnet/pull/911),
  [`eae1a60`](https://github.com/ditto-assistant/ditto-subnet/commit/eae1a605932e4d68a6d9aa9a2a95a9205bac4884))

### Features

- Add miner profiles, hotkey sign-in, and hosted MCP
  ([#899](https://github.com/ditto-assistant/ditto-subnet/pull/899),
  [`70340f4`](https://github.com/ditto-assistant/ditto-subnet/commit/70340f450d71b1d04f4e5d4b18f29126a40f9ce5))


## v0.84.2 (2026-08-17)

### Bug Fixes

- **platform**: Stop efficiency tiebreak saturating at the 1.1 cap
  ([#893](https://github.com/ditto-assistant/ditto-subnet/pull/893),
  [`f07720b`](https://github.com/ditto-assistant/ditto-subnet/commit/f07720b96467a11fd2b2f0d71622b8162e95a5a7))


## v0.84.1 (2026-08-17)

### Bug Fixes

- **screener**: Keep claimed runtime archives until smoke finishes
  ([#909](https://github.com/ditto-assistant/ditto-subnet/pull/909),
  [`ed47784`](https://github.com/ditto-assistant/ditto-subnet/commit/ed477845056e18e41ef560ca05d9a4156ff28abd))

### Chores

- **skills**: Promote nested skills to repo-root agents and claude
  ([#907](https://github.com/ditto-assistant/ditto-subnet/pull/907),
  [`4844554`](https://github.com/ditto-assistant/ditto-subnet/commit/484455489aeae5e1cc3cf64d84afe99af07897a6))


## v0.84.0 (2026-08-17)

### Bug Fixes

- **scoring**: Stop charging agents for an unfinished route challenge
  ([#900](https://github.com/ditto-assistant/ditto-subnet/pull/900),
  [`446ef91`](https://github.com/ditto-assistant/ditto-subnet/commit/446ef919f4000250fce0e6f033ab05a0b3acaed1))

### Features

- **backroom**: Add ATH precedent search and board-review skill
  ([#906](https://github.com/ditto-assistant/ditto-subnet/pull/906),
  [`081a5ca`](https://github.com/ditto-assistant/ditto-subnet/commit/081a5ca08bfa44fd49d691d7a7379d6e69c5871a))


## v0.83.6 (2026-08-17)

### Bug Fixes

- **screener**: Omit gated targon persistent-workload experiment
  ([#902](https://github.com/ditto-assistant/ditto-subnet/pull/902),
  [`f753e76`](https://github.com/ditto-assistant/ditto-subnet/commit/f753e76042d4ef9a75071feedf202846530df000))

- **screener**: Replace leftover targon images before delete
  ([#903](https://github.com/ditto-assistant/ditto-subnet/pull/903),
  [`dd8574b`](https://github.com/ditto-assistant/ditto-subnet/commit/dd8574bf5217019c6df768ba5a3dc879782a90d0))


## v0.83.5 (2026-08-17)

### Bug Fixes

- **platform**: Keep desired bench live after frozen-member bans
  ([#905](https://github.com/ditto-assistant/ditto-subnet/pull/905),
  [`15fc5b9`](https://github.com/ditto-assistant/ditto-subnet/commit/15fc5b998b0a52d1b50cc9e4da21cd2a2a7eacdb))


## v0.83.4 (2026-08-17)

### Bug Fixes

- **screener**: Hold targon one-shots until delete
  ([#901](https://github.com/ditto-assistant/ditto-subnet/pull/901),
  [`6f1bed4`](https://github.com/ditto-assistant/ditto-subnet/commit/6f1bed455ee6edf9e96095c02d8d26830d637aff))


## v0.83.3 (2026-08-17)

### Bug Fixes

- **platform**: Re-cut the rejected-resubmission lexical bar from production data
  ([#898](https://github.com/ditto-assistant/ditto-subnet/pull/898),
  [`af0df79`](https://github.com/ditto-assistant/ditto-subnet/commit/af0df79390722268c39d826ca82b1b23cac90ac0))


## v0.83.2 (2026-08-17)

### Bug Fixes

- **screener**: Sweep leftover targon one-shot rentals
  ([#892](https://github.com/ditto-assistant/ditto-subnet/pull/892),
  [`0fdc872`](https://github.com/ditto-assistant/ditto-subnet/commit/0fdc8720de320983efc5b297eabc8bf97bc0afef))


## v0.83.1 (2026-08-17)

### Bug Fixes

- **confirmation**: Follow the live benchmark and persist failure diagnostics
  ([#894](https://github.com/ditto-assistant/ditto-subnet/pull/894),
  [`4f69050`](https://github.com/ditto-assistant/ditto-subnet/commit/4f69050037e194a73c922e61f38ab8e334d83428))

- **validator**: Pay every bench version the fleet can execute
  ([#897](https://github.com/ditto-assistant/ditto-subnet/pull/897),
  [`83524c5`](https://github.com/ditto-assistant/ditto-subnet/commit/83524c5169ca9a5d3d48806587383131b76c9324))

### Chores

- **skills**: Symlink repo skills into claude skills
  ([#896](https://github.com/ditto-assistant/ditto-subnet/pull/896),
  [`16e6bc7`](https://github.com/ditto-assistant/ditto-subnet/commit/16e6bc7062173a750a091009b796310333ed8131))


## v0.83.0 (2026-08-17)

### Features

- **platform**: Let miners set a signed hotkey profile picture
  ([#880](https://github.com/ditto-assistant/ditto-subnet/pull/880),
  [`a2e7d6b`](https://github.com/ditto-assistant/ditto-subnet/commit/a2e7d6b4baf2313380fbd4e7f02f9767d7853da5))

- **platform**: Reserve miner handles via signed claims
  ([#865](https://github.com/ditto-assistant/ditto-subnet/pull/865),
  [`8ab46d1`](https://github.com/ditto-assistant/ditto-subnet/commit/8ab46d1056e2f6b765e154828d9cc6056ba2036f))


## v0.82.0 (2026-08-17)

### Features

- **platform**: Hold resubmissions of rejected artifacts
  ([#891](https://github.com/ditto-assistant/ditto-subnet/pull/891),
  [`32a1f14`](https://github.com/ditto-assistant/ditto-subnet/commit/32a1f14423d1ef89e49b1c660e3f27197cb80ced))


## v0.81.0 (2026-08-17)

### Bug Fixes

- **screener**: Degrade unconfigured builder lanes instead of crash-looping
  ([#890](https://github.com/ditto-assistant/ditto-subnet/pull/890),
  [`f4f0aa0`](https://github.com/ditto-assistant/ditto-subnet/commit/f4f0aa06c1c6cea790f272b1d9eabf401910bef6))

### Chores

- **infra**: Add a static inventory for the screener capacity controller
  ([#888](https://github.com/ditto-assistant/ditto-subnet/pull/888),
  [`69be175`](https://github.com/ditto-assistant/ditto-subnet/commit/69be1756efedb1355d8610902337105d03a05083))

### Features

- **validator**: Publish an allowlisted confirmation failure class
  ([#889](https://github.com/ditto-assistant/ditto-subnet/pull/889),
  [`8585055`](https://github.com/ditto-assistant/ditto-subnet/commit/858505561a1d4c31e54cf2e014a665cc9a436dc7))


## v0.80.1 (2026-08-16)

### Bug Fixes

- **platform**: Unpin curve-v3 efficiency schema from bench 9
  ([#885](https://github.com/ditto-assistant/ditto-subnet/pull/885),
  [`f04ecc9`](https://github.com/ditto-assistant/ditto-subnet/commit/f04ecc9dfe991621bb09f950382b5b4ded9e0ea4))

- **screener**: Stop the fleet bootstrap leaking a root-only ssh command
  ([#886](https://github.com/ditto-assistant/ditto-subnet/pull/886),
  [`672cea2`](https://github.com/ditto-assistant/ditto-subnet/commit/672cea283a40643e71796d950d1de20bedf4f463))


## v0.80.0 (2026-08-16)

### Features

- **screener**: Control Targon provider routing
  ([#704](https://github.com/ditto-assistant/ditto-subnet/pull/704),
  [`480353f`](https://github.com/ditto-assistant/ditto-subnet/commit/480353f74e4e21e5a5822e2cda6a2fd4ead676b2))


## v0.79.2 (2026-08-16)

### Bug Fixes

- **dittobench**: Score a proven zero-inference v10/v11 run as 0.00
  ([#883](https://github.com/ditto-assistant/ditto-subnet/pull/883),
  [`0249b03`](https://github.com/ditto-assistant/ditto-subnet/commit/0249b0370866763d4be4e7f965ed21d03738eeb8))


## v0.79.1 (2026-08-16)

### Bug Fixes

- **dashboard**: Quiet the fleet header to exceptions only
  ([#879](https://github.com/ditto-assistant/ditto-subnet/pull/879),
  [`3e0e170`](https://github.com/ditto-assistant/ditto-subnet/commit/3e0e170b2c06185b85996369abb57d47fb54456a))

- **platform**: Publish the score floor on the ranking scale it comes from
  ([#882](https://github.com/ditto-assistant/ditto-subnet/pull/882),
  [`bc8dd46`](https://github.com/ditto-assistant/ditto-subnet/commit/bc8dd46f678366f3d681f42c91c89f05b9821470))


## v0.79.0 (2026-08-16)

### Features

- **platform-dashboard**: Distill the validator fleet table to three columns
  ([#878](https://github.com/ditto-assistant/ditto-subnet/pull/878),
  [`7138fc3`](https://github.com/ditto-assistant/ditto-subnet/commit/7138fc3491d0ca81a1bc0c48d89b17863685f28c))


## v0.78.7 (2026-08-16)

### Bug Fixes

- **platform**: Project v9 base evidence for every carried-forward bench version
  ([#877](https://github.com/ditto-assistant/ditto-subnet/pull/877),
  [`6bd0dea`](https://github.com/ditto-assistant/ditto-subnet/commit/6bd0dea539f331fe8580b53921021b6d5b449f90))


## v0.78.6 (2026-08-16)

### Bug Fixes

- **validator**: Advertise bench v11 scorer capability
  ([#876](https://github.com/ditto-assistant/ditto-subnet/pull/876),
  [`0fc88cb`](https://github.com/ditto-assistant/ditto-subnet/commit/0fc88cbf82974ac4d4254801ae1f8e7451dedb4a))


## v0.78.5 (2026-08-16)

### Bug Fixes

- **scoring**: Narrow LongMem reader rejection attribution
  ([#875](https://github.com/ditto-assistant/ditto-subnet/pull/875),
  [`0eb40ad`](https://github.com/ditto-assistant/ditto-subnet/commit/0eb40ad3189c70aeec88973b89d87033fb867f9e))


## v0.78.4 (2026-08-16)

### Bug Fixes

- **release**: Expect bench v11 in the dittobench deploy identity gate
  ([#874](https://github.com/ditto-assistant/ditto-subnet/pull/874),
  [`8143634`](https://github.com/ditto-assistant/ditto-subnet/commit/8143634ffc0c341546db8177028d4774a0158735))


## v0.78.3 (2026-08-16)

### Bug Fixes

- **dittobench**: Settle v10/v11 case attribution so base evidence assembles
  ([#873](https://github.com/ditto-assistant/ditto-subnet/pull/873),
  [`f68d1fe`](https://github.com/ditto-assistant/ditto-subnet/commit/f68d1fe7e681e7d1490bc1e7af2a89fce47bca81))


## v0.78.2 (2026-08-16)

### Bug Fixes

- **tests**: Stop contract generators emitting from a stale protocol install
  ([#870](https://github.com/ditto-assistant/ditto-subnet/pull/870),
  [`c9ef64d`](https://github.com/ditto-assistant/ditto-subnet/commit/c9ef64d3d59f2aa33021d4952096b493b8fb6d2d))


## v0.78.1 (2026-08-16)

### Bug Fixes

- **dittobench**: Stop enforcing v9 case attribution on bench v10
  ([#872](https://github.com/ditto-assistant/ditto-subnet/pull/872),
  [`836dcc6`](https://github.com/ditto-assistant/ditto-subnet/commit/836dcc67e5ed1067a7bc259a3e4105b762f2a348))


## v0.78.0 (2026-08-16)

### Features

- **platform**: Ship the bench v11 contract as an operator rollout target
  ([#869](https://github.com/ditto-assistant/ditto-subnet/pull/869),
  [`b22b22f`](https://github.com/ditto-assistant/ditto-subnet/commit/b22b22f3d76ff7a34fa0915ce70a3c15cd9a7ad2))


## v0.77.1 (2026-08-16)

### Bug Fixes

- **scoring**: Attribute rejected LongMem reader requests
  ([#867](https://github.com/ditto-assistant/ditto-subnet/pull/867),
  [`2398b09`](https://github.com/ditto-assistant/ditto-subnet/commit/2398b09176ab73b7ea41dc93829ddb6f020941a8))

- **scoring**: Cap the KOTH dethrone band at the score left to win
  ([#868](https://github.com/ditto-assistant/ditto-subnet/pull/868),
  [`47dbbac`](https://github.com/ditto-assistant/ditto-subnet/commit/47dbbac1255e06a409a002a8e5a355db9db46ad5))

### Chores

- **agents**: Add local ditto-subnet github skill
  ([#866](https://github.com/ditto-assistant/ditto-subnet/pull/866),
  [`4ed8b59`](https://github.com/ditto-assistant/ditto-subnet/commit/4ed8b59e40b245ce40426c37aaa7df221ceb5c22))

- **tests**: Stop screener heartbeat tampering test racing the clock
  ([#864](https://github.com/ditto-assistant/ditto-subnet/pull/864),
  [`6448d16`](https://github.com/ditto-assistant/ditto-subnet/commit/6448d162914fdcc5a055f93b71471db1db5e7126))


## v0.77.0 (2026-08-16)

### Features

- **dittobench**: Execute private bench v11 with the v9 evidence stack
  ([#861](https://github.com/ditto-assistant/ditto-subnet/pull/861),
  [`aff0474`](https://github.com/ditto-assistant/ditto-subnet/commit/aff04749a7a5a96f485de6b70428d4172cfadbbf))


## v0.76.0 (2026-08-16)

### Chores

- **agent**: Add LongMem confirmation rollout skill
  ([#863](https://github.com/ditto-assistant/ditto-subnet/pull/863),
  [`4b6769b`](https://github.com/ditto-assistant/ditto-subnet/commit/4b6769bb44c6940cc994553c39edfafc6de937c5))

### Features

- **datagen**: Define private bench v11 anti-template-fitting contract
  ([#860](https://github.com/ditto-assistant/ditto-subnet/pull/860),
  [`e95904f`](https://github.com/ditto-assistant/ditto-subnet/commit/e95904f97ec469953116067b0331a7b2a66cec45))


## v0.75.4 (2026-08-16)

### Bug Fixes

- **scoring**: Carry the v9 evidence, gate, and curve-v3 stack forward to bench v10
  ([#859](https://github.com/ditto-assistant/ditto-subnet/pull/859),
  [`f44a3c9`](https://github.com/ditto-assistant/ditto-subnet/commit/f44a3c942d902ebde7b302974e062a78fa39c82f))


## v0.75.3 (2026-08-16)

### Bug Fixes

- **scoring**: Settle unused LongMem reader as zero
  ([`8b29417`](https://github.com/ditto-assistant/ditto-subnet/commit/8b2941751651a675d1a6e9b70631c88c3ca5e26b))


## v0.75.2 (2026-08-15)

### Bug Fixes

- **dittobench**: Run cross-encoder rerank on the blocking pool
  ([#853](https://github.com/ditto-assistant/ditto-subnet/pull/853),
  [`5addaaa`](https://github.com/ditto-assistant/ditto-subnet/commit/5addaaad399fd9474a316a8cc28f91e57d94eeaa))


## v0.75.1 (2026-08-15)

### Bug Fixes

- **platform-dashboard**: Clarify held KOTH crowns
  ([#857](https://github.com/ditto-assistant/ditto-subnet/pull/857),
  [`55fee45`](https://github.com/ditto-assistant/ditto-subnet/commit/55fee453be110e0991c20740d3862e6fe2541d7f))


## v0.75.0 (2026-08-15)

### Features

- **dashboard**: Surface current on-chain weights on leaderboard and fleet
  ([#856](https://github.com/ditto-assistant/ditto-subnet/pull/856),
  [`f97e70e`](https://github.com/ditto-assistant/ditto-subnet/commit/f97e70e5bfffe245110d1978f8f7dd62cf178cad))


## v0.74.1 (2026-08-15)

### Bug Fixes

- **scoring**: Seal LongMem case isolation
  ([`869024e`](https://github.com/ditto-assistant/ditto-subnet/commit/869024ef22d78d50a61e587e5893843613ae66dd))


## v0.74.0 (2026-08-15)

### Features

- **benchmark**: Add v10 runtime controls
  ([#851](https://github.com/ditto-assistant/ditto-subnet/pull/851),
  [`d39d9c3`](https://github.com/ditto-assistant/ditto-subnet/commit/d39d9c354c9d89736c4c0a1bd051a992f28bd930))


## v0.73.0 (2026-08-15)

### Features

- **dashboard**: Mobile card layouts for pipeline, fleet, and submissions
  ([#855](https://github.com/ditto-assistant/ditto-subnet/pull/855),
  [`34ea07e`](https://github.com/ditto-assistant/ditto-subnet/commit/34ea07e2e39c76fe8618427373238775d5c7818f))


## v0.72.0 (2026-08-15)

### Bug Fixes

- **platform**: Count only admitted validator work
  ([#854](https://github.com/ditto-assistant/ditto-subnet/pull/854),
  [`586a2a6`](https://github.com/ditto-assistant/ditto-subnet/commit/586a2a6dc4de6c3eb3a6c4d0546513b20d7b829c))

### Features

- **dashboard**: Split operations into pipeline and fleet pages with compact slot lines
  ([#852](https://github.com/ditto-assistant/ditto-subnet/pull/852),
  [`d826138`](https://github.com/ditto-assistant/ditto-subnet/commit/d826138230154503c2e572262c33c46c12f154e5))


## v0.71.0 (2026-08-15)

### Bug Fixes

- **scoring**: Isolate LongMem harness case failures
  ([#847](https://github.com/ditto-assistant/ditto-subnet/pull/847),
  [`b2b607d`](https://github.com/ditto-assistant/ditto-subnet/commit/b2b607dbcded26e20d76849a1706bc1aa7f8d8c1))

### Features

- **backroom**: Add inference runtime diagnostics
  ([#848](https://github.com/ditto-assistant/ditto-subnet/pull/848),
  [`ecdd681`](https://github.com/ditto-assistant/ditto-subnet/commit/ecdd681f582144cc330cbd96e916a16523907772))

- **dashboard**: Surface managed updater progress
  ([#849](https://github.com/ditto-assistant/ditto-subnet/pull/849),
  [`430aad9`](https://github.com/ditto-assistant/ditto-subnet/commit/430aad96fd86cadf026a438e62fa24edbbd0308a))


## v0.70.0 (2026-08-15)

### Bug Fixes

- **platform**: Show composites at six decimals on the board
  ([#845](https://github.com/ditto-assistant/ditto-subnet/pull/845),
  [`f5a5158`](https://github.com/ditto-assistant/ditto-subnet/commit/f5a5158d1b894b00b0c6eceaf0d3d37fa5079f54))

### Features

- **dashboard**: Reflow the leaderboard into cards on phones
  ([#846](https://github.com/ditto-assistant/ditto-subnet/pull/846),
  [`d3854d4`](https://github.com/ditto-assistant/ditto-subnet/commit/d3854d469de780ad1dcdb6cf3b245391c431ed98))


## v0.69.0 (2026-08-15)

### Bug Fixes

- **dittobench**: Advertise executable bench v10
  ([#841](https://github.com/ditto-assistant/ditto-subnet/pull/841),
  [`cf6df30`](https://github.com/ditto-assistant/ditto-subnet/commit/cf6df306f546e045bbbbcfd54541fac83415b87e))

- **dittobench**: Require model-backed v10 tool execution
  ([#843](https://github.com/ditto-assistant/ditto-subnet/pull/843),
  [`06908dd`](https://github.com/ditto-assistant/ditto-subnet/commit/06908ddcba53f40dd944a757f7fed4e1410a886e))

- **platform**: Ship benchmark v10 rollout contract
  ([#842](https://github.com/ditto-assistant/ditto-subnet/pull/842),
  [`9956355`](https://github.com/ditto-assistant/ditto-subnet/commit/9956355617f5f29b229e43b9a1aefec293b10a58))

### Features

- **datagen**: Add state-dependent v10 tool routing
  ([#837](https://github.com/ditto-assistant/ditto-subnet/pull/837),
  [`74a2f43`](https://github.com/ditto-assistant/ditto-subnet/commit/74a2f437d52c5702df5bdd6921e9ad1a624f4732))

- **datagen**: Define private bench v10 generator contract
  ([#836](https://github.com/ditto-assistant/ditto-subnet/pull/836),
  [`ca64cf9`](https://github.com/ditto-assistant/ditto-subnet/commit/ca64cf9b141c098265ea7b1cdc938b4b1837a39b))

- **datagen**: Gate v10 computed memory exposure
  ([#838](https://github.com/ditto-assistant/ditto-subnet/pull/838),
  [`61d5bc3`](https://github.com/ditto-assistant/ditto-subnet/commit/61d5bc39abf90229f46f8e9c78da95a8d7106b54))

- **dittobench**: Add private v10 deep-history profile
  ([#839](https://github.com/ditto-assistant/ditto-subnet/pull/839),
  [`d05ef0f`](https://github.com/ditto-assistant/ditto-subnet/commit/d05ef0fbef234d3fc780fcbed7499d355c83953d))

- **dittobench**: Add private v10 qualification gate
  ([#840](https://github.com/ditto-assistant/ditto-subnet/pull/840),
  [`890fb8e`](https://github.com/ditto-assistant/ditto-subnet/commit/890fb8e293bf21634055f19a1a79525c9021fdc2))


## v0.68.21 (2026-08-15)

### Bug Fixes

- **scoring**: Surface safe confirmation diagnostics
  ([`18b04c8`](https://github.com/ditto-assistant/ditto-subnet/commit/18b04c8d9c486c29edec50efed8463ec73315926))

### Chores

- **perf**: Harden profiling evidence workflow
  ([#834](https://github.com/ditto-assistant/ditto-subnet/pull/834),
  [`3847967`](https://github.com/ditto-assistant/ditto-subnet/commit/3847967190a3dc310de0b3360cfb4d9cee100185))


## v0.68.20 (2026-08-15)

### Bug Fixes

- **platform**: Show the efficiency tie-break as a direction
  ([#832](https://github.com/ditto-assistant/ditto-subnet/pull/832),
  [`d8ccd72`](https://github.com/ditto-assistant/ditto-subnet/commit/d8ccd7296fc2d3bfbee969cb3b513b5d45b786c2))

- **release**: Centralize post-merge verification
  ([#831](https://github.com/ditto-assistant/ditto-subnet/pull/831),
  [`7d50a83`](https://github.com/ditto-assistant/ditto-subnet/commit/7d50a83b807670edef05323ae42c09656cc761c1))


## v0.68.19 (2026-08-15)

### Bug Fixes

- **platform**: Allow disabled public disk ceiling
  ([#833](https://github.com/ditto-assistant/ditto-subnet/pull/833),
  [`68e2227`](https://github.com/ditto-assistant/ditto-subnet/commit/68e22279daea7d1dadf656319a5e54396eefc411))


## v0.68.18 (2026-08-15)

### Bug Fixes

- **platform**: Preserve scored agents during rescores
  ([#830](https://github.com/ditto-assistant/ditto-subnet/pull/830),
  [`79e3f9a`](https://github.com/ditto-assistant/ditto-subnet/commit/79e3f9a36210d30f2ca8047ab5a856de4431bbd5))


## v0.68.17 (2026-08-15)

### Bug Fixes

- **platform**: Enforce confirmation retry deadline
  ([#828](https://github.com/ditto-assistant/ditto-subnet/pull/828),
  [`6ff9e55`](https://github.com/ditto-assistant/ditto-subnet/commit/6ff9e55619bc274e0607b96cffbc71b3760b466c))

- **release**: Accelerate relay and controller deploys
  ([#829](https://github.com/ditto-assistant/ditto-subnet/pull/829),
  [`238ded1`](https://github.com/ditto-assistant/ditto-subnet/commit/238ded13caaee3ee5f8ff387ce1ed91eb3cbdd8f))


## v0.68.16 (2026-08-15)

### Bug Fixes

- **platform**: Prevent retry past relay deadline
  ([#827](https://github.com/ditto-assistant/ditto-subnet/pull/827),
  [`97cf9f6`](https://github.com/ditto-assistant/ditto-subnet/commit/97cf9f608ea70c07529f6ca71b6db727856e6b1d))


## v0.68.15 (2026-08-15)

### Bug Fixes

- **platform**: Expose active efficiency tiebreak
  ([#825](https://github.com/ditto-assistant/ditto-subnet/pull/825),
  [`c77ec54`](https://github.com/ditto-assistant/ditto-subnet/commit/c77ec543c2357cf707084df00a2d9d2cd757164c))

- **platform**: Persist validator name cache
  ([#822](https://github.com/ditto-assistant/ditto-subnet/pull/822),
  [`35648ad`](https://github.com/ditto-assistant/ditto-subnet/commit/35648ad72c7e4c5337760d92f11c1a25f1cf0f49))

- **platform**: Reuse pipeline ranking snapshot
  ([#821](https://github.com/ditto-assistant/ditto-subnet/pull/821),
  [`7df6752`](https://github.com/ditto-assistant/ditto-subnet/commit/7df67526718c7db9ca58946973aaa5f590b8cd2b))

- **protocol**: Accept additive JSON fields
  ([#823](https://github.com/ditto-assistant/ditto-subnet/pull/823),
  [`4199ca0`](https://github.com/ditto-assistant/ditto-subnet/commit/4199ca0a4ade2075afbde773316c67e3d1c69435))

- **scoring**: Preserve zero ablation usage fields
  ([`cf02a86`](https://github.com/ditto-assistant/ditto-subnet/commit/cf02a861294e71f036bdba1039b3bd97809b2d40))

- **validator**: Reclaim obsolete managed images
  ([#826](https://github.com/ditto-assistant/ditto-subnet/pull/826),
  [`d87a2ac`](https://github.com/ditto-assistant/ditto-subnet/commit/d87a2acba3c1737f14d5be199398a88f361e5673))


## v0.68.14 (2026-08-15)

### Bug Fixes

- **release**: Stage relay artifacts through gcs
  ([#820](https://github.com/ditto-assistant/ditto-subnet/pull/820),
  [`dfd0424`](https://github.com/ditto-assistant/ditto-subnet/commit/dfd04242e674b6b908cc3640491e32ec1fe136c0))


## v0.68.13 (2026-08-15)

### Bug Fixes

- **validator**: Preserve productive benchmark attempts
  ([#819](https://github.com/ditto-assistant/ditto-subnet/pull/819),
  [`ef043a3`](https://github.com/ditto-assistant/ditto-subnet/commit/ef043a34fa9d55c6f824c22bc3463ae9bfc7ab10))


## v0.68.12 (2026-08-15)

### Bug Fixes

- **scoring**: Classify LongMem harness failures
  ([`dab2175`](https://github.com/ditto-assistant/ditto-subnet/commit/dab21759b3e557a08ae7d28a3ad64393712f6a35))


## v0.68.11 (2026-08-15)

### Bug Fixes

- **platform**: Show family retest evidence
  ([#746](https://github.com/ditto-assistant/ditto-subnet/pull/746),
  [`f974c2f`](https://github.com/ditto-assistant/ditto-subnet/commit/f974c2f56d984423d8e3250e842320d82aea5406))


## v0.68.10 (2026-08-15)

### Bug Fixes

- **dittobench**: Queue embedding backpressure safely
  ([#805](https://github.com/ditto-assistant/ditto-subnet/pull/805),
  [`83898c2`](https://github.com/ditto-assistant/ditto-subnet/commit/83898c286c4d31b01b1de087b54f87b19fbd1456))


## v0.68.9 (2026-08-15)

### Bug Fixes

- **platform**: Route continual retests authoritatively
  ([#812](https://github.com/ditto-assistant/ditto-subnet/pull/812),
  [`139ab8f`](https://github.com/ditto-assistant/ditto-subnet/commit/139ab8f5690aec00664d97582a0ad73728a90822))

- **scoring**: Retry idempotent LongMem seeds
  ([`e494216`](https://github.com/ditto-assistant/ditto-subnet/commit/e4942165d034b2984192efc3a2ecb516cd96a641))


## v0.68.8 (2026-08-15)

### Bug Fixes

- **platform**: Extend LongMem reader backpressure recovery
  ([`6e10442`](https://github.com/ditto-assistant/ditto-subnet/commit/6e10442667ab7f5662ea8325df606567c5f6973f))


## v0.68.7 (2026-08-15)

### Bug Fixes

- **platform**: Retry pre-provider confirmation route misses
  ([`26b57ef`](https://github.com/ditto-assistant/ditto-subnet/commit/26b57ef5f59bfa168f6a39604ebde2fd5f77b60b))


## v0.68.6 (2026-08-15)

### Bug Fixes

- **scoring**: Tolerate unjudgeable LongMem cases
  ([#811](https://github.com/ditto-assistant/ditto-subnet/pull/811),
  [`b23c048`](https://github.com/ditto-assistant/ditto-subnet/commit/b23c04867eb58fa342222ed5886dce133e08b1d1))


## v0.68.5 (2026-08-15)

### Bug Fixes

- **platform**: Anchor the crown on the defended score
  ([#783](https://github.com/ditto-assistant/ditto-subnet/pull/783),
  [`224b3cc`](https://github.com/ditto-assistant/ditto-subnet/commit/224b3cc08fd57e9a4ab75bb60f27282858317559))

- **platform**: Represent an owner by its newest tied generation
  ([#786](https://github.com/ditto-assistant/ditto-subnet/pull/786),
  [`183163d`](https://github.com/ditto-assistant/ditto-subnet/commit/183163d55288c373091f0b9c81120f0f1107df2f))

- **release**: Standardize managed validator capacity
  ([#806](https://github.com/ditto-assistant/ditto-subnet/pull/806),
  [`816bc7a`](https://github.com/ditto-assistant/ditto-subnet/commit/816bc7a632e3db5023a81fd4a06af31ecd78a291))

### Chores

- **tests**: Harden LongMem retry accounting
  ([#810](https://github.com/ditto-assistant/ditto-subnet/pull/810),
  [`998562a`](https://github.com/ditto-assistant/ditto-subnet/commit/998562a9dfac64c3b9a5738d5e147375124b7447))


## v0.68.4 (2026-08-15)

### Bug Fixes

- **platform**: Retry LongMem confirmation backpressure
  ([`d228272`](https://github.com/ditto-assistant/ditto-subnet/commit/d2282726cc1af3cd9c976d9a283cfdc6a2cdb87e))


## v0.68.3 (2026-08-15)

### Bug Fixes

- **dittobench**: Route LongMem providers under ZDR
  ([#808](https://github.com/ditto-assistant/ditto-subnet/pull/808),
  [`6fd4570`](https://github.com/ditto-assistant/ditto-subnet/commit/6fd457059d76fb4fd86aaeaf86da715937ca5f8e))


## v0.68.2 (2026-08-15)

### Bug Fixes

- **dittobench**: Decode official LongMem numeric answers
  ([#807](https://github.com/ditto-assistant/ditto-subnet/pull/807),
  [`160d3ad`](https://github.com/ditto-assistant/ditto-subnet/commit/160d3ada26a9a26a33f6e54489e84c8884bc8ae9))


## v0.68.1 (2026-08-15)

### Bug Fixes

- **release**: Skip unrelated root verification
  ([#804](https://github.com/ditto-assistant/ditto-subnet/pull/804),
  [`d96b2ab`](https://github.com/ditto-assistant/ditto-subnet/commit/d96b2abd6c376e537606c39ae9b16fb9a8101514))


## v0.68.0 (2026-08-15)

### Bug Fixes

- **platform**: Accept LongMem embedding provider slug
  ([#803](https://github.com/ditto-assistant/ditto-subnet/pull/803),
  [`0f766b4`](https://github.com/ditto-assistant/ditto-subnet/commit/0f766b41779d60e3f98128879bcbf2208cd88303))

- **platform**: Reuse queue preview owner aliases
  ([#801](https://github.com/ditto-assistant/ditto-subnet/pull/801),
  [`7d3221a`](https://github.com/ditto-assistant/ditto-subnet/commit/7d3221a9c25b94b117ac7cc0294aa9acc056059d))

- **release**: Retire completed WSL updater bootstrap
  ([#800](https://github.com/ditto-assistant/ditto-subnet/pull/800),
  [`8e5112e`](https://github.com/ditto-assistant/ditto-subnet/commit/8e5112ea83ad6d37198dc21122af397ffc01e5b3))

### Features

- **dittobench**: Add relay delay-fingerprint shadow evidence for per-case model use
  ([#802](https://github.com/ditto-assistant/ditto-subnet/pull/802),
  [`e46bc53`](https://github.com/ditto-assistant/ditto-subnet/commit/e46bc5398f953593180812879da280a1995c9525))

- **validator**: Report managed updater status
  ([#777](https://github.com/ditto-assistant/ditto-subnet/pull/777),
  [`22fb4bb`](https://github.com/ditto-assistant/ditto-subnet/commit/22fb4bb8b6f5cfc757000d659fae0569f87bab19))


## v0.67.1 (2026-08-15)

### Bug Fixes

- **release**: Parallelize validator stack gates
  ([#798](https://github.com/ditto-assistant/ditto-subnet/pull/798),
  [`85795a7`](https://github.com/ditto-assistant/ditto-subnet/commit/85795a7b983473ebee8466c833a72df7f9e3822e))

- **release**: Restore frozen relay manifest
  ([#799](https://github.com/ditto-assistant/ditto-subnet/pull/799),
  [`b4ae0a5`](https://github.com/ditto-assistant/ditto-subnet/commit/b4ae0a553c90639561aec8dd79027bc955ccea95))


## v0.67.0 (2026-08-15)

### Bug Fixes

- **release**: Pin profiler compatibility source
  ([#795](https://github.com/ditto-assistant/ditto-subnet/pull/795),
  [`b0f0b79`](https://github.com/ditto-assistant/ditto-subnet/commit/b0f0b79c4cbfb67eecad3a663cb6d38a3c7aec14))

### Features

- **miner-cli**: Offer inline hotkey registration on a 1101 pre-check
  ([#776](https://github.com/ditto-assistant/ditto-subnet/pull/776),
  [`7c3b62e`](https://github.com/ditto-assistant/ditto-subnet/commit/7c3b62ee7516229208404702e0fd9154d10d05aa))

- **platform**: Raise inference concurrency ceiling to 512
  ([#797](https://github.com/ditto-assistant/ditto-subnet/pull/797),
  [`532bd7a`](https://github.com/ditto-assistant/ditto-subnet/commit/532bd7af7abe9c1461342b1a64144ff6172560cf))


## v0.66.3 (2026-08-15)

### Bug Fixes

- **release**: Restore frozen relay compatibility source
  ([#794](https://github.com/ditto-assistant/ditto-subnet/pull/794),
  [`a2211d9`](https://github.com/ditto-assistant/ditto-subnet/commit/a2211d904638ffde14f43a2a3f4aed68958ef1c1))


## v0.66.2 (2026-08-15)

### Bug Fixes

- **dittobench**: Accept ticket-scoped LongMem proxy
  ([#796](https://github.com/ditto-assistant/ditto-subnet/pull/796),
  [`f181faf`](https://github.com/ditto-assistant/ditto-subnet/commit/f181fafc08932ce5c79239950de8997a58d1d75a))


## v0.66.1 (2026-08-15)

### Bug Fixes

- **release**: Build relay compatibility beside scorers
  ([#793](https://github.com/ditto-assistant/ditto-subnet/pull/793),
  [`b5142ea`](https://github.com/ditto-assistant/ditto-subnet/commit/b5142ea37ae96b3a6e3987491af7f396ad9b7f19))


## v0.66.0 (2026-08-15)

### Bug Fixes

- **platform**: Skip unchanged efficiency materialization
  ([#792](https://github.com/ditto-assistant/ditto-subnet/pull/792),
  [`fd024b6`](https://github.com/ditto-assistant/ditto-subnet/commit/fd024b6d29e5f1a91033fc9fd572046ddc5d5815))

### Features

- **perf**: Add cross-runtime profiling
  ([#789](https://github.com/ditto-assistant/ditto-subnet/pull/789),
  [`6e07f14`](https://github.com/ditto-assistant/ditto-subnet/commit/6e07f14e8589ee9b73ac707d323473d1c95f91fe))


## v0.65.2 (2026-08-15)

### Bug Fixes

- **release**: Run semantic release without Docker
  ([#791](https://github.com/ditto-assistant/ditto-subnet/pull/791),
  [`1bbe2f2`](https://github.com/ditto-assistant/ditto-subnet/commit/1bbe2f2eac5220d4a659e0be26de7b712781864e))


## v0.65.1 (2026-08-15)

### Bug Fixes

- **platform**: Cache fresh scoring ledgers
  ([#790](https://github.com/ditto-assistant/ditto-subnet/pull/790),
  [`6aa42e2`](https://github.com/ditto-assistant/ditto-subnet/commit/6aa42e2d58aee11c82adc3451596846acf73ce38))

- **release**: Shard root source verification
  ([#788](https://github.com/ditto-assistant/ditto-subnet/pull/788),
  [`9a37da3`](https://github.com/ditto-assistant/ditto-subnet/commit/9a37da381067c5ad2d0e170baa244abc5466a57a))


## v0.65.0 (2026-08-14)

### Features

- **validator**: Publish LongMem heartbeat progress
  ([#768](https://github.com/ditto-assistant/ditto-subnet/pull/768),
  [`aed592c`](https://github.com/ditto-assistant/ditto-subnet/commit/aed592c240cb3dcadc32ffcb0ac8a93925b49381))


## v0.64.0 (2026-08-14)

### Bug Fixes

- **platform**: Share fleet-safe efficiency ledgers
  ([#775](https://github.com/ditto-assistant/ditto-subnet/pull/775),
  [`fb2c35e`](https://github.com/ditto-assistant/ditto-subnet/commit/fb2c35e4459cfb79ffdec3bf2c0840fb3178bab6))

- **platform**: Show paused validators in operations
  ([#781](https://github.com/ditto-assistant/ditto-subnet/pull/781),
  [`aba3d30`](https://github.com/ditto-assistant/ditto-subnet/commit/aba3d30a9d7bbb8e0b47927a838af93dad35ae2d))

### Features

- **platform**: Make hosted inference policy live
  ([#780](https://github.com/ditto-assistant/ditto-subnet/pull/780),
  [`ec5b8d9`](https://github.com/ditto-assistant/ditto-subnet/commit/ec5b8d9e2af76d90fb52044b9d604477710a7e5f))


## v0.63.6 (2026-08-14)

### Bug Fixes

- **release**: Cache verified scorer assets
  ([#785](https://github.com/ditto-assistant/ditto-subnet/pull/785),
  [`50a8935`](https://github.com/ditto-assistant/ditto-subnet/commit/50a8935f1ffff7f08a21b2603331686f161b55d8))


## v0.63.5 (2026-08-14)

### Bug Fixes

- **platform**: Raise hosted inference token ceiling
  ([`9c0ce7a`](https://github.com/ditto-assistant/ditto-subnet/commit/9c0ce7adfe6e01d34f54c115361e7f0582368a07))


## v0.63.4 (2026-08-14)

### Bug Fixes

- **release**: Parallelize artifact authentication
  ([#782](https://github.com/ditto-assistant/ditto-subnet/pull/782),
  [`7d0d05f`](https://github.com/ditto-assistant/ditto-subnet/commit/7d0d05f49899e17373387a881af7d02b1b1a8dc7))


## v0.63.3 (2026-08-14)

### Bug Fixes

- **release**: Build validator on native architectures
  ([#779](https://github.com/ditto-assistant/ditto-subnet/pull/779),
  [`6f8291e`](https://github.com/ditto-assistant/ditto-subnet/commit/6f8291e4f286dd3bea68b42ae449afbc2247744b))


## v0.63.2 (2026-08-14)

### Bug Fixes

- **release**: Build scorer on native architectures
  ([#773](https://github.com/ditto-assistant/ditto-subnet/pull/773),
  [`c0b2958`](https://github.com/ditto-assistant/ditto-subnet/commit/c0b295845429ea6e659e8ad9e89151f7001b6331))


## v0.63.1 (2026-08-14)

### Bug Fixes

- **platform**: Skip completed efficiency audits
  ([#774](https://github.com/ditto-assistant/ditto-subnet/pull/774),
  [`3d2a128`](https://github.com/ditto-assistant/ditto-subnet/commit/3d2a1289341b445e13a091a6d3fe56b5360085d9))


## v0.63.0 (2026-08-14)

### Features

- **platform**: Move upload admission to Go request plane
  ([#766](https://github.com/ditto-assistant/ditto-subnet/pull/766),
  [`9b368e7`](https://github.com/ditto-assistant/ditto-subnet/commit/9b368e7a913d01e9cdd698008442054555b74381))


## v0.62.3 (2026-08-14)

### Bug Fixes

- **platform**: Singleflight validator ledger reads
  ([#772](https://github.com/ditto-assistant/ditto-subnet/pull/772),
  [`914b915`](https://github.com/ditto-assistant/ditto-subnet/commit/914b9151d1755f6d033d4d272336d722fc994946))


## v0.62.2 (2026-08-14)

### Bug Fixes

- **release**: Resume post-release fanout after skips
  ([#770](https://github.com/ditto-assistant/ditto-subnet/pull/770),
  [`9a36ecc`](https://github.com/ditto-assistant/ditto-subnet/commit/9a36ecc26ae6307ddfbac8be3e87e296f05a9676))

- **validator**: Accept private updater checkout isolation
  ([#771](https://github.com/ditto-assistant/ditto-subnet/pull/771),
  [`69f0c49`](https://github.com/ditto-assistant/ditto-subnet/commit/69f0c49598f069cf47683eeed209ad1236aef6d3))


## v0.62.1 (2026-08-14)

### Bug Fixes

- **platform**: Prioritize confirmation policy writes
  ([#767](https://github.com/ditto-assistant/ditto-subnet/pull/767),
  [`8d68b81`](https://github.com/ditto-assistant/ditto-subnet/commit/8d68b81185ecd53f58d6fd84bf724d2d26a933ad))


## v0.62.0 (2026-08-14)

### Bug Fixes

- **platform**: Bound validator ledger evidence reads
  ([#757](https://github.com/ditto-assistant/ditto-subnet/pull/757),
  [`e49bc44`](https://github.com/ditto-assistant/ditto-subnet/commit/e49bc44b9b0926f4dccfa58fde8c9d7634716172))

- **platform**: Stop confirmation polls from saturating API
  ([#756](https://github.com/ditto-assistant/ditto-subnet/pull/756),
  [`9ace1a4`](https://github.com/ditto-assistant/ditto-subnet/commit/9ace1a470dddc20ff442b1b1d296dc6c70bb8cc8))

- **release**: Evaluate release after optional skips
  ([#763](https://github.com/ditto-assistant/ditto-subnet/pull/763),
  [`5608101`](https://github.com/ditto-assistant/ditto-subnet/commit/5608101715abb2519f53c94d52e9f7860d36a31a))

- **release**: Install uv for model relay gate
  ([#761](https://github.com/ditto-assistant/ditto-subnet/pull/761),
  [`3e0579f`](https://github.com/ditto-assistant/ditto-subnet/commit/3e0579f9aa7fc9ebb2ff61ac323b31c4084bdc7e))

- **release**: Reject stale candidates before parallel verification
  ([#753](https://github.com/ditto-assistant/ditto-subnet/pull/753),
  [`c368071`](https://github.com/ditto-assistant/ditto-subnet/commit/c3680718357f095208414b7b632c9d41bbedd89c))

- **release**: Route bottlenecks to 8-core runner
  ([#760](https://github.com/ditto-assistant/ditto-subnet/pull/760),
  [`fb0bb77`](https://github.com/ditto-assistant/ditto-subnet/commit/fb0bb77545c94c57cf942b9722f93d6fbd585547))

- **scoring**: Keep quality primary in efficiency order
  ([#758](https://github.com/ditto-assistant/ditto-subnet/pull/758),
  [`a66b3b0`](https://github.com/ditto-assistant/ditto-subnet/commit/a66b3b0aa65469c639258a0daae15bf7ce05b1ab))

### Chores

- **ci**: Shard Platform pull request checks
  ([#759](https://github.com/ditto-assistant/ditto-subnet/pull/759),
  [`1af548d`](https://github.com/ditto-assistant/ditto-subnet/commit/1af548dd8b47b804eaa3632918d9e26cf7127fad))

- **ci**: Share Platform verification with releases
  ([#765](https://github.com/ditto-assistant/ditto-subnet/pull/765),
  [`6bea621`](https://github.com/ditto-assistant/ditto-subnet/commit/6bea62124c7d958e188260bac9b7b85f4b5f8186))

### Features

- **release**: Prefetch signed validator stack candidates
  ([#754](https://github.com/ditto-assistant/ditto-subnet/pull/754),
  [`99393ba`](https://github.com/ditto-assistant/ditto-subnet/commit/99393bab3cc8cb8f0892e915452131da40286392))


## v0.61.4 (2026-08-14)

### Bug Fixes

- **dittobench**: Unblock LongMem shadow diagnostics
  ([#752](https://github.com/ditto-assistant/ditto-subnet/pull/752),
  [`3f2e288`](https://github.com/ditto-assistant/ditto-subnet/commit/3f2e2883e757d361745856daecc77262bd5a1178))

- **platform**: Narrow the crown-anchor band
  ([#748](https://github.com/ditto-assistant/ditto-subnet/pull/748),
  [`b0370d8`](https://github.com/ditto-assistant/ditto-subnet/commit/b0370d8a017b865104a1d94207b333eee5feac0d))

- **platform**: Publish the KOTH crown anchor
  ([#747](https://github.com/ditto-assistant/ditto-subnet/pull/747),
  [`93c4ac3`](https://github.com/ditto-assistant/ditto-subnet/commit/93c4ac3037981c12378d3505afa32763caf1e9f3))


## v0.61.3 (2026-08-14)

### Bug Fixes

- **release**: Provide validator identity to stack smoke
  ([#750](https://github.com/ditto-assistant/ditto-subnet/pull/750),
  [`421530c`](https://github.com/ditto-assistant/ditto-subnet/commit/421530c563d55e5c3ee42e302c450cea9f719250))

- **release**: Unblock relay and validator stack activation
  ([#751](https://github.com/ditto-assistant/ditto-subnet/pull/751),
  [`02ee2ab`](https://github.com/ditto-assistant/ditto-subnet/commit/02ee2abcd0e11eacb5eb6e8695789b59f4b7a6ef))


## v0.61.2 (2026-08-14)

### Bug Fixes

- **platform**: Admit finalized v9 efficiency cohorts
  ([#744](https://github.com/ditto-assistant/ditto-subnet/pull/744),
  [`9810a17`](https://github.com/ditto-assistant/ditto-subnet/commit/9810a1740dd3aed52671429b1f39c6cb83a43e04))


## v0.61.1 (2026-08-14)

### Bug Fixes

- **platform**: Extract relay artifacts as deploy user
  ([#745](https://github.com/ditto-assistant/ditto-subnet/pull/745),
  [`2ed8042`](https://github.com/ditto-assistant/ditto-subnet/commit/2ed80422a48188bad14d06bcf278514cbf963252))

- **validator**: Bootstrap WSL frozen updater
  ([#743](https://github.com/ditto-assistant/ditto-subnet/pull/743),
  [`e5d593a`](https://github.com/ditto-assistant/ditto-subnet/commit/e5d593a755d9f7d4dc0ba19a9e4ebe6e0a178b04))


## v0.61.0 (2026-08-14)

### Features

- **platform**: Rewrite model relay in Go with binary release pipeline
  ([#742](https://github.com/ditto-assistant/ditto-subnet/pull/742),
  [`bb5b3a3`](https://github.com/ditto-assistant/ditto-subnet/commit/bb5b3a3b97b5ae200477c58c98c10a2323f525f8))


## v0.60.1 (2026-08-14)

### Bug Fixes

- **platform**: Align public emissions with owner-ranked board
  ([#739](https://github.com/ditto-assistant/ditto-subnet/pull/739),
  [`d6bc0f3`](https://github.com/ditto-assistant/ditto-subnet/commit/d6bc0f3fc3b07b2dd57d27fb0c533dcb642c743f))

- **validator**: Preserve one-seed confirmation stderr
  ([#741](https://github.com/ditto-assistant/ditto-subnet/pull/741),
  [`9a877ad`](https://github.com/ditto-assistant/ditto-subnet/commit/9a877ad964d560ec6f48a7e818219a38f8409110))


## v0.60.0 (2026-08-14)

### Bug Fixes

- **validator**: Prevent continual retest resets
  ([#738](https://github.com/ditto-assistant/ditto-subnet/pull/738),
  [`1c356b3`](https://github.com/ditto-assistant/ditto-subnet/commit/1c356b3ef994ef280c00b9a941d405688413b143))

### Chores

- **tests**: Preserve paused continual retest lease
  ([#740](https://github.com/ditto-assistant/ditto-subnet/pull/740),
  [`f131009`](https://github.com/ditto-assistant/ditto-subnet/commit/f131009fa1f08523e7eb25e09e25409c0521a078))

### Features

- **platform**: Add validator issuance pauses
  ([#737](https://github.com/ditto-assistant/ditto-subnet/pull/737),
  [`551f4ee`](https://github.com/ditto-assistant/ditto-subnet/commit/551f4ee9b179a3127f1ed29fcaad510c3e3fc3b6))


## v0.59.1 (2026-08-14)

### Bug Fixes

- **validator**: Stage retest claims to fill idle slots
  ([#736](https://github.com/ditto-assistant/ditto-subnet/pull/736),
  [`1d1557a`](https://github.com/ditto-assistant/ditto-subnet/commit/1d1557ad5744f67303a7552b1eb6a41177a465f7))


## v0.59.0 (2026-08-14)

### Bug Fixes

- **release**: Recover validator stack upgrades
  ([#720](https://github.com/ditto-assistant/ditto-subnet/pull/720),
  [`98c5381`](https://github.com/ditto-assistant/ditto-subnet/commit/98c538103b124cdda431dca226b49ab032867940))

### Features

- **dittobench**: Install bounded LongMem shadow profile
  ([#721](https://github.com/ditto-assistant/ditto-subnet/pull/721),
  [`5525811`](https://github.com/ditto-assistant/ditto-subnet/commit/5525811fb7f151ff600593d54ca7d6bf25e4c9cb))


## v0.58.3 (2026-08-14)

### Bug Fixes

- **backroom**: Bound stuck submission lists
  ([#733](https://github.com/ditto-assistant/ditto-subnet/pull/733),
  [`c3f11a1`](https://github.com/ditto-assistant/ditto-subnet/commit/c3f11a1b9ebf797f4611663a60cd1de33afe5737))

- **platform**: Prevent embedding startup stampedes
  ([#735](https://github.com/ditto-assistant/ditto-subnet/pull/735),
  [`dc7e965`](https://github.com/ditto-assistant/ditto-subnet/commit/dc7e9657adac366af5eb2210307e468853483207))

- **validator**: Bound continual retest failure loops
  ([#734](https://github.com/ditto-assistant/ditto-subnet/pull/734),
  [`e89af4f`](https://github.com/ditto-assistant/ditto-subnet/commit/e89af4fa3756153b42ffd1e249abf8ee7f213b62))


## v0.58.2 (2026-08-14)

### Bug Fixes

- **platform**: Exclude deregistered miners from public crown
  ([#718](https://github.com/ditto-assistant/ditto-subnet/pull/718),
  [`5f9cc44`](https://github.com/ditto-assistant/ditto-subnet/commit/5f9cc44313fdc432d7428a56955c9c847f6e89df))


## v0.58.1 (2026-08-14)

### Bug Fixes

- **dittobench**: Remove validator cloud secret runtime
  ([#716](https://github.com/ditto-assistant/ditto-subnet/pull/716),
  [`1edee93`](https://github.com/ditto-assistant/ditto-subnet/commit/1edee93705ec088bc3cc6b1cd794e5437b76fbfb))

- **platform**: Bound public handler round trips
  ([#711](https://github.com/ditto-assistant/ditto-subnet/pull/711),
  [`abd54cc`](https://github.com/ditto-assistant/ditto-subnet/commit/abd54cc9dcc72ddbb94d9597f3c5cf2f8bf5619b))

- **platform**: Collapse confirmation replay admission
  ([#712](https://github.com/ditto-assistant/ditto-subnet/pull/712),
  [`bbf3df8`](https://github.com/ditto-assistant/ditto-subnet/commit/bbf3df8c19453d063faa4f99b0e7e4add30f4d8e))

- **screener**: Stop quarantining a targeted API-key read as exfiltration
  ([#717](https://github.com/ditto-assistant/ditto-subnet/pull/717),
  [`c6e37f4`](https://github.com/ditto-assistant/ditto-subnet/commit/c6e37f46bd395cf83ddcab888ed92ed8af164086))


## v0.58.0 (2026-08-14)

### Features

- **validator**: Share ceiling-deadlocked crowns
  ([#692](https://github.com/ditto-assistant/ditto-subnet/pull/692),
  [`e165a07`](https://github.com/ditto-assistant/ditto-subnet/commit/e165a0790c121a2edc85e0f3c5dbd22723279172))


## v0.57.0 (2026-08-13)

### Bug Fixes

- **backroom**: Accept null rank on public leaderboard rows
  ([#708](https://github.com/ditto-assistant/ditto-subnet/pull/708),
  [`08df572`](https://github.com/ditto-assistant/ditto-subnet/commit/08df5729fafdb4a8bcb19fa52f851dd8c2e87ffa))

- **dittobench**: Harden validator embedding gateway
  ([#709](https://github.com/ditto-assistant/ditto-subnet/pull/709),
  [`7f4a241`](https://github.com/ditto-assistant/ditto-subnet/commit/7f4a241fb403151d28a2b1896b5ad23e643937dd))

- **platform**: Average run cost over completed leases only
  ([#710](https://github.com/ditto-assistant/ditto-subnet/pull/710),
  [`e04a8e9`](https://github.com/ditto-assistant/ditto-subnet/commit/e04a8e98c953eb511d94ca693d6cbc75b20baf78))

- **platform**: Restore v9 contract retest queue
  ([#703](https://github.com/ditto-assistant/ditto-subnet/pull/703),
  [`0d7f619`](https://github.com/ditto-assistant/ditto-subnet/commit/0d7f6197f5ef3d231e0308f8bc13babdb98c8d10))

### Features

- **dittobench**: Proxy LongMem inference through Platform
  ([#699](https://github.com/ditto-assistant/ditto-subnet/pull/699),
  [`ef68c2d`](https://github.com/ditto-assistant/ditto-subnet/commit/ef68c2d14d48ba4fa773cc4e3e7b37bd830ae92f))

- **platform**: Select LongMemEval by base score
  ([#698](https://github.com/ditto-assistant/ditto-subnet/pull/698),
  [`5cf637d`](https://github.com/ditto-assistant/ditto-subnet/commit/5cf637d0e781954215f0f2ff745c9448958dc0ba))

- **validator**: Isolate LongMem execution capacity
  ([#700](https://github.com/ditto-assistant/ditto-subnet/pull/700),
  [`11c16e6`](https://github.com/ditto-assistant/ditto-subnet/commit/11c16e616d4bcef6d43d70865cb7e2fed27f0172))


## v0.56.4 (2026-08-13)

### Bug Fixes

- **platform**: Suppress exhausted rollout tail
  ([#706](https://github.com/ditto-assistant/ditto-subnet/pull/706),
  [`ef888c7`](https://github.com/ditto-assistant/ditto-subnet/commit/ef888c7e69ca1913b7f82bb1d99a13be0b81f5f7))


## v0.56.3 (2026-08-13)

### Bug Fixes

- **platform**: Honor coherent source scorer identity
  ([#705](https://github.com/ditto-assistant/ditto-subnet/pull/705),
  [`6799e06`](https://github.com/ditto-assistant/ditto-subnet/commit/6799e06b5c02a201389373486178f0275c5eb815))


## v0.56.2 (2026-08-13)

### Bug Fixes

- **platform**: Load relay env from monorepo root
  ([#702](https://github.com/ditto-assistant/ditto-subnet/pull/702),
  [`c70955e`](https://github.com/ditto-assistant/ditto-subnet/commit/c70955e79326ab1aa41ee83b94c9df8c77706053))

- **release**: Classify stale runs as superseded
  ([#697](https://github.com/ditto-assistant/ditto-subnet/pull/697),
  [`3eb941f`](https://github.com/ditto-assistant/ditto-subnet/commit/3eb941f0106950d0ccc03259bfd12925d8a15b8f))


## v0.56.1 (2026-08-13)

### Bug Fixes

- **platform**: Bound public activity queries
  ([#694](https://github.com/ditto-assistant/ditto-subnet/pull/694),
  [`65af5f8`](https://github.com/ditto-assistant/ditto-subnet/commit/65af5f8305b92cbfdd602c934f567924237f8721))

- **platform**: Serialize dispatch and bound nonce cleanup
  ([#693](https://github.com/ditto-assistant/ditto-subnet/pull/693),
  [`870d12f`](https://github.com/ditto-assistant/ditto-subnet/commit/870d12fcdd8b891e076796d2e81619698772e203))

- **validator**: Recover stalled scorer infrastructure
  ([#695](https://github.com/ditto-assistant/ditto-subnet/pull/695),
  [`c0b4b58`](https://github.com/ditto-assistant/ditto-subnet/commit/c0b4b58362bca40ef31010d1610142ebe4b28e91))


## v0.56.0 (2026-08-13)

### Bug Fixes

- **backroom**: Return whole source manifests and report MCP paging
  ([#669](https://github.com/ditto-assistant/ditto-subnet/pull/669),
  [`e939520`](https://github.com/ditto-assistant/ditto-subnet/commit/e93952050bfa7a8dfd7b01df058d68f161123165))

- **platform**: Keep ranked rows above alternate sorts
  ([#691](https://github.com/ditto-assistant/ditto-subnet/pull/691),
  [`30cff0e`](https://github.com/ditto-assistant/ditto-subnet/commit/30cff0e3e6992d8fd155da2dfbaa8da05eaeecbb))

### Features

- **backroom**: Control submission deposit address
  ([#685](https://github.com/ditto-assistant/ditto-subnet/pull/685),
  [`bd2381b`](https://github.com/ditto-assistant/ditto-subnet/commit/bd2381b029f1e0b53e39a885d50062766b980053))


## v0.55.0 (2026-08-13)

### Chores

- **agent**: Add W&B API operations skill
  ([#680](https://github.com/ditto-assistant/ditto-subnet/pull/680),
  [`2f95c51`](https://github.com/ditto-assistant/ditto-subnet/commit/2f95c51aa22319882b07ac91425daa4e421450f4))

- **platform**: Stop fork PRs failing a migration check that passed
  ([#684](https://github.com/ditto-assistant/ditto-subnet/pull/684),
  [`291fe6c`](https://github.com/ditto-assistant/ditto-subnet/commit/291fe6ca5466a33a3f0bb8aa35b4cb5dd796a810))

### Features

- **scoring**: Add retest-aware bounded v9 efficiency ranking
  ([#675](https://github.com/ditto-assistant/ditto-subnet/pull/675),
  [`6c13112`](https://github.com/ditto-assistant/ditto-subnet/commit/6c1311244bf1bd933694fe9444e586fd677b3a29))


## v0.54.0 (2026-08-13)

### Bug Fixes

- **ci**: Restore GitHub-hosted release runners
  ([`751c56b`](https://github.com/ditto-assistant/ditto-subnet/commit/751c56b2549a18ad152dd66b471c5c0ca9a15e84))

- **platform**: Exclude stock starter-kit files from anti-copy fingerprints
  ([#659](https://github.com/ditto-assistant/ditto-subnet/pull/659),
  [`2e6548d`](https://github.com/ditto-assistant/ditto-subnet/commit/2e6548d9e3050c8a3286fa2fe878b74f4b37b8fd))

- **platform**: Make a held KOTH crown visually obvious on the board
  ([#674](https://github.com/ditto-assistant/ditto-subnet/pull/674),
  [`760517d`](https://github.com/ditto-assistant/ditto-subnet/commit/760517d35d83b81d258328c91b19d9edbb41c2ad))

- **platform**: Make the review queue return the review queue
  ([#678](https://github.com/ditto-assistant/ditto-subnet/pull/678),
  [`14b6dee`](https://github.com/ditto-assistant/ditto-subnet/commit/14b6dee1848d9138946bdcd9dfe91d4e749d7689))

- **platform**: Name the earliest source in a copy hold, not the nearest
  ([#676](https://github.com/ditto-assistant/ditto-subnet/pull/676),
  [`4a9b154`](https://github.com/ditto-assistant/ditto-subnet/commit/4a9b15498e1d5566d2f34f6890496365cb603772))

- **validator**: Pay v9 scores before confirmation enforce
  ([`ef67948`](https://github.com/ditto-assistant/ditto-subnet/commit/ef679489fbdf26f2283350a5bb2ecb489f7aff5a))

- **validator**: Prove source scorer release identity
  ([#673](https://github.com/ditto-assistant/ditto-subnet/pull/673),
  [`27eb11c`](https://github.com/ditto-assistant/ditto-subnet/commit/27eb11c15f1f64e2f72a1b4a11b62abd519968bf))

### Features

- **platform**: Add a no-source-review queue policy mode
  ([#665](https://github.com/ditto-assistant/ditto-subnet/pull/665),
  [`e78b5ba`](https://github.com/ditto-assistant/ditto-subnet/commit/e78b5ba6bb578c80344da1bcfe267e1d2bb0715c))

- **platform**: Search screened source in one request
  ([#677](https://github.com/ditto-assistant/ditto-subnet/pull/677),
  [`e228c3c`](https://github.com/ditto-assistant/ditto-subnet/commit/e228c3c2ef27aeb4015cae6a7cdad7436e7b72bd))


## v0.53.23 (2026-08-13)

### Bug Fixes

- **platform**: Stop holding miners for source the subnet published
  ([#670](https://github.com/ditto-assistant/ditto-subnet/pull/670),
  [`4dc47b8`](https://github.com/ditto-assistant/ditto-subnet/commit/4dc47b82c989f950e83f0d206b896950b8600fc1))


## v0.53.22 (2026-08-13)

### Bug Fixes

- **platform**: Activate rollout on frozen priority cohort
  ([#672](https://github.com/ditto-assistant/ditto-subnet/pull/672),
  [`33cbf8d`](https://github.com/ditto-assistant/ditto-subnet/commit/33cbf8d043ddb21a5f8f26d2252833ac94e3dced))


## v0.53.21 (2026-08-13)

### Bug Fixes

- **platform**: Make direct embeddings primary
  ([#671](https://github.com/ditto-assistant/ditto-subnet/pull/671),
  [`c1693aa`](https://github.com/ditto-assistant/ditto-subnet/commit/c1693aab2168cb494c7572b46b2a083449330076))


## v0.53.20 (2026-08-13)

### Bug Fixes

- **platform**: Add direct embedding gateway fallback
  ([`83aa356`](https://github.com/ditto-assistant/ditto-subnet/commit/83aa3560d1614cd45a46e07d8066458347db5540))


## v0.53.19 (2026-08-12)

### Bug Fixes

- **dittobench**: Retry hosted embedding preflight
  ([#667](https://github.com/ditto-assistant/ditto-subnet/pull/667),
  [`806c1cc`](https://github.com/ditto-assistant/ditto-subnet/commit/806c1cc36b098818cf76ca72b5fce0528c46e438))


## v0.53.18 (2026-08-12)

### Bug Fixes

- **dittobench**: Wait out embedding provider throttle
  ([#666](https://github.com/ditto-assistant/ditto-subnet/pull/666),
  [`3bdf424`](https://github.com/ditto-assistant/ditto-subnet/commit/3bdf424d1d8049fa25839523c95bd6ce30ac3d69))


## v0.53.17 (2026-08-12)

### Bug Fixes

- **platform**: Preserve embedding provider backpressure
  ([#661](https://github.com/ditto-assistant/ditto-subnet/pull/661),
  [`ef6e638`](https://github.com/ditto-assistant/ditto-subnet/commit/ef6e638affce64859c15f1968431b41e9aab85a6))


## v0.53.16 (2026-08-12)

### Bug Fixes

- **dittobench**: Serve versioned practice datasets
  ([#663](https://github.com/ditto-assistant/ditto-subnet/pull/663),
  [`a9c589d`](https://github.com/ditto-assistant/ditto-subnet/commit/a9c589dd6dd95f12bd4f96cdf030d21e7cbc4e4e))

- **validator**: Follow active benchmark authority
  ([#662](https://github.com/ditto-assistant/ditto-subnet/pull/662),
  [`462b7e7`](https://github.com/ditto-assistant/ditto-subnet/commit/462b7e7116bcd3c2e5b66d40d9a99682c9439ff3))


## v0.53.15 (2026-08-12)

### Bug Fixes

- **platform**: Prioritize frozen rollout cohort
  ([#658](https://github.com/ditto-assistant/ditto-subnet/pull/658),
  [`e4d3f4d`](https://github.com/ditto-assistant/ditto-subnet/commit/e4d3f4d623a57c8016dab0002b71ab7f821ec03b))


## v0.53.14 (2026-08-12)

### Bug Fixes

- **dittobench**: Prove v9 zero-model runs
  ([#655](https://github.com/ditto-assistant/ditto-subnet/pull/655),
  [`f3caa9a`](https://github.com/ditto-assistant/ditto-subnet/commit/f3caa9a6f240b6546cf188b4b87ec72a799bc132))

- **platform**: Let completed v9 failures yield authority
  ([#656](https://github.com/ditto-assistant/ditto-subnet/pull/656),
  [`b429102`](https://github.com/ditto-assistant/ditto-subnet/commit/b4291023a372c99641f9dc5704ad19c5ff1b41d8))


## v0.53.13 (2026-08-12)

### Bug Fixes

- **platform**: Fill v9 contract repair slots
  ([#654](https://github.com/ditto-assistant/ditto-subnet/pull/654),
  [`781c95b`](https://github.com/ditto-assistant/ditto-subnet/commit/781c95bc9050b57202863e76d09cc91c3428c545))

- **screener**: Clean up Targon build rentals
  ([#649](https://github.com/ditto-assistant/ditto-subnet/pull/649),
  [`5097b1a`](https://github.com/ditto-assistant/ditto-subnet/commit/5097b1ad1c51366e2d416800fb7d33c10bcee792))


## v0.53.12 (2026-08-12)

### Bug Fixes

- **platform**: Requeue failed v9 score repairs
  ([#653](https://github.com/ditto-assistant/ditto-subnet/pull/653),
  [`2280bfd`](https://github.com/ditto-assistant/ditto-subnet/commit/2280bfda9088e94e768ec7a25917dda52519ce45))


## v0.53.11 (2026-08-12)

### Bug Fixes

- **platform**: Gate every v9 rollout lane
  ([#652](https://github.com/ditto-assistant/ditto-subnet/pull/652),
  [`cb45e86`](https://github.com/ditto-assistant/ditto-subnet/commit/cb45e86e7e1980e1d07ed6b8cef7763181c43537))


## v0.53.10 (2026-08-12)

### Bug Fixes

- **dittobench**: Preserve v9 attribution order
  ([`6402642`](https://github.com/ditto-assistant/ditto-subnet/commit/6402642061b476d029aeca6ce85f332cae79cffd))


## v0.53.9 (2026-08-12)

### Bug Fixes

- **platform**: Require tail-safe v9 scorers
  ([`fdb1132`](https://github.com/ditto-assistant/ditto-subnet/commit/fdb113222b358b4bc87327b8948eef74cad7049d))


## v0.53.8 (2026-08-12)

### Bug Fixes

- **dittobench**: Exclude unfinished v9 attribution tails
  ([#648](https://github.com/ditto-assistant/ditto-subnet/pull/648),
  [`87b9869`](https://github.com/ditto-assistant/ditto-subnet/commit/87b98690120ef921ee7087d3efc09450249b84ab))


## v0.53.7 (2026-08-12)

### Bug Fixes

- **platform**: Require generation-bound v9 scorers
  ([#647](https://github.com/ditto-assistant/ditto-subnet/pull/647),
  [`ad1a55c`](https://github.com/ditto-assistant/ditto-subnet/commit/ad1a55c4addd5a1d9d4b39878d2d85ca96be1657))


## v0.53.6 (2026-08-12)

### Bug Fixes

- **dittobench**: Bind v9 attribution to case generations
  ([#646](https://github.com/ditto-assistant/ditto-subnet/pull/646),
  [`76003c1`](https://github.com/ditto-assistant/ditto-subnet/commit/76003c173ed3e90d54eedbea00600d98516584f9))

- **platform**: Show v8 and v9 memory timeline
  ([#645](https://github.com/ditto-assistant/ditto-subnet/pull/645),
  [`fb922f2`](https://github.com/ditto-assistant/ditto-subnet/commit/fb922f26bc457b96168f0970a2210021e99d4723))

- **platform-dashboard**: Compact operations workspace
  ([#644](https://github.com/ditto-assistant/ditto-subnet/pull/644),
  [`5633557`](https://github.com/ditto-assistant/ditto-subnet/commit/5633557aaebe967022482accc0c2c1aa951f77cf))


## v0.53.5 (2026-08-12)

### Bug Fixes

- **dittobench**: Keep v9 attribution windows open
  ([#641](https://github.com/ditto-assistant/ditto-subnet/pull/641),
  [`84fa379`](https://github.com/ditto-assistant/ditto-subnet/commit/84fa379907cc01fbdeb183e5b88114d02e00a9f0))

- **platform**: Require complete v9 attribution
  ([#642](https://github.com/ditto-assistant/ditto-subnet/pull/642),
  [`bd0bdb8`](https://github.com/ditto-assistant/ditto-subnet/commit/bd0bdb8f8f1d1b47b5b2694cbbc62cccfe4ce787))


## v0.53.4 (2026-08-12)

### Bug Fixes

- **platform**: Bind v9 retests to the v9 era
  ([#640](https://github.com/ditto-assistant/ditto-subnet/pull/640),
  [`422bf22`](https://github.com/ditto-assistant/ditto-subnet/commit/422bf225da3eb1cdbe2f9385b78b295f45159b4e))


## v0.53.3 (2026-08-12)

### Bug Fixes

- **platform**: Retry defective v9 attribution evidence
  ([#639](https://github.com/ditto-assistant/ditto-subnet/pull/639),
  [`973affd`](https://github.com/ditto-assistant/ditto-subnet/commit/973affd482b715165d3b00887aec66db34cdd7f8))


## v0.53.2 (2026-08-11)

### Bug Fixes

- **dittobench**: Settle v9 case attribution
  ([#637](https://github.com/ditto-assistant/ditto-subnet/pull/637),
  [`782a641`](https://github.com/ditto-assistant/ditto-subnet/commit/782a6414b5547b8d410225c6e5988ec7e25c785a))

- **platform**: Require the repaired v9 scorer
  ([#636](https://github.com/ditto-assistant/ditto-subnet/pull/636),
  [`95131df`](https://github.com/ditto-assistant/ditto-subnet/commit/95131dfa194876e1701a7b333460df49e9ef19af))


## v0.53.1 (2026-08-11)

### Bug Fixes

- **dittobench**: Preserve zero v9 score evidence
  ([#633](https://github.com/ditto-assistant/ditto-subnet/pull/633),
  [`c2f3b07`](https://github.com/ditto-assistant/ditto-subnet/commit/c2f3b07e9ca9897358e3f93e6adac0bba0082aaf))

- **platform**: Release ATH holds stranded by a reopened copy review
  ([#634](https://github.com/ditto-assistant/ditto-subnet/pull/634),
  [`f21fa75`](https://github.com/ditto-assistant/ditto-subnet/commit/f21fa754215d5949f2f23f0213330f456b97f8af))

- **screener**: Harden Targon build fallback
  ([#635](https://github.com/ditto-assistant/ditto-subnet/pull/635),
  [`f8544bb`](https://github.com/ditto-assistant/ditto-subnet/commit/f8544bbe2223ed78dc743041fea9fb6db219766d))


## v0.53.0 (2026-08-11)

### Bug Fixes

- **backroom**: Distinguish rollout membership from scoring
  ([#631](https://github.com/ditto-assistant/ditto-subnet/pull/631),
  [`09f1782`](https://github.com/ditto-assistant/ditto-subnet/commit/09f1782d367bf65e82e229c1d1510d8675f7a33c))

- **backroom**: Surface rollout target reviews
  ([#627](https://github.com/ditto-assistant/ditto-subnet/pull/627),
  [`438aa9d`](https://github.com/ditto-assistant/ditto-subnet/commit/438aa9d355e1bfbf58d1cbdea473d18b17df130c))

- **platform**: Keep deferred health failures retryable
  ([#629](https://github.com/ditto-assistant/ditto-subnet/pull/629),
  [`2515fc3`](https://github.com/ditto-assistant/ditto-subnet/commit/2515fc307d267622d29d24659fa892e433f5c964))

- **platform**: Repair held v9 score gates
  ([#632](https://github.com/ditto-assistant/ditto-subnet/pull/632),
  [`18955a1`](https://github.com/ditto-assistant/ditto-subnet/commit/18955a179059631025636d7a72381b672241a35a))

### Features

- **backroom**: Batch recover stuck validation work
  ([#628](https://github.com/ditto-assistant/ditto-subnet/pull/628),
  [`815ad29`](https://github.com/ditto-assistant/ditto-subnet/commit/815ad29ee6234c94f2b50470ef58b94c63f405e4))


## v0.52.1 (2026-08-11)

### Bug Fixes

- **platform**: Dispatch v9 contract retests during rollout
  ([#625](https://github.com/ditto-assistant/ditto-subnet/pull/625),
  [`5959c9c`](https://github.com/ditto-assistant/ditto-subnet/commit/5959c9c5d390d6b7ff17b4452ca43cd94dd510ec))

- **platform**: Reserve concurrent rollout lane positions
  ([#624](https://github.com/ditto-assistant/ditto-subnet/pull/624),
  [`0e197ab`](https://github.com/ditto-assistant/ditto-subnet/commit/0e197abda2daf46c12395bd16a954727d2cf0800))


## v0.52.0 (2026-08-11)

### Bug Fixes

- **dittobench**: Enforce v9 semantic evidence
  ([#621](https://github.com/ditto-assistant/ditto-subnet/pull/621),
  [`b0941c7`](https://github.com/ditto-assistant/ditto-subnet/commit/b0941c711f8e11f3e49f7cc28339dbfea3c158fc))

- **platform**: Gate v9 authority on semantic evidence
  ([#617](https://github.com/ditto-assistant/ditto-subnet/pull/617),
  [`b300619`](https://github.com/ditto-assistant/ditto-subnet/commit/b30061926d41a53c35e5fb33cdb0d88ab8f37de6))

- **platform**: Queue authoritative v9 score retests
  ([#622](https://github.com/ditto-assistant/ditto-subnet/pull/622),
  [`3c28d54`](https://github.com/ditto-assistant/ditto-subnet/commit/3c28d54633bad416e6eb1cdd9c528ef4d3091699))

- **screener**: Give Targon builds a real timeout
  ([#620](https://github.com/ditto-assistant/ditto-subnet/pull/620),
  [`5360d55`](https://github.com/ditto-assistant/ditto-subnet/commit/5360d55f1dbbb39f610f8d31a22b6ec6277db624))

### Features

- Add Backroom submission triage skill
  ([#616](https://github.com/ditto-assistant/ditto-subnet/pull/616),
  [`7682298`](https://github.com/ditto-assistant/ditto-subnet/commit/76822985a21eaa9e59e06158dbe048a919d794f8))

- **platform**: Show Targon submission builds
  ([#618](https://github.com/ditto-assistant/ditto-subnet/pull/618),
  [`49c7e2a`](https://github.com/ditto-assistant/ditto-subnet/commit/49c7e2a942d6b1f0f88825e354e4304aad9ab294))

- **screener**: Control L3 review independently
  ([#619](https://github.com/ditto-assistant/ditto-subnet/pull/619),
  [`d442555`](https://github.com/ditto-assistant/ditto-subnet/commit/d442555b76ae47c247f7e7ffff552c6f16d7e52b))


## v0.51.7 (2026-08-11)

### Bug Fixes

- **dittobench**: Persist v9 private projections
  ([#615](https://github.com/ditto-assistant/ditto-subnet/pull/615),
  [`0beb2c8`](https://github.com/ditto-assistant/ditto-subnet/commit/0beb2c8bfd61ccb980461e618621048b343bbba1))


## v0.51.6 (2026-08-11)

### Bug Fixes

- **platform**: Unblock v8 v9 inference grants
  ([#614](https://github.com/ditto-assistant/ditto-subnet/pull/614),
  [`775a4af`](https://github.com/ditto-assistant/ditto-subnet/commit/775a4af47eea30cdde08e724ec66fb032dbb3524))


## v0.51.5 (2026-08-11)

### Bug Fixes

- **platform**: Make v9 rollout preflight truthful
  ([#613](https://github.com/ditto-assistant/ditto-subnet/pull/613),
  [`ee967a6`](https://github.com/ditto-assistant/ditto-subnet/commit/ee967a642b0dc362067bfd5d17e564f4472f23d4))


## v0.51.4 (2026-08-11)

### Bug Fixes

- **dittobench**: Inherit v9 execution boundaries
  ([#612](https://github.com/ditto-assistant/ditto-subnet/pull/612),
  [`34d4fa7`](https://github.com/ditto-assistant/ditto-subnet/commit/34d4fa74bc2737c4d07ddbc0f2d56ca797054c62))


## v0.51.3 (2026-08-11)

### Bug Fixes

- **dittobench**: Restore versioned platform embeddings
  ([#610](https://github.com/ditto-assistant/ditto-subnet/pull/610),
  [`59be721`](https://github.com/ditto-assistant/ditto-subnet/commit/59be721fae127aada5ec332d2a888a3cd59e2baf))


## v0.51.2 (2026-08-11)

### Bug Fixes

- **dittobench**: Preserve v8 tool observation
  ([#607](https://github.com/ditto-assistant/ditto-subnet/pull/607),
  [`d7cd56b`](https://github.com/ditto-assistant/ditto-subnet/commit/d7cd56b9322f7762b7920f4a8e5df23b924c3ff4))

- **release**: Serialize Targon builder rollout
  ([`81cf41e`](https://github.com/ditto-assistant/ditto-subnet/commit/81cf41e5b4ecbfe913ee769c065c33d735f8c18f))


## v0.51.1 (2026-08-11)

### Bug Fixes

- **platform**: Install remote build protocol
  ([#609](https://github.com/ditto-assistant/ditto-subnet/pull/609),
  [`09ce1f9`](https://github.com/ditto-assistant/ditto-subnet/commit/09ce1f961c5c10256bfb63b49282b3a5b1b2020c))


## v0.51.0 (2026-08-11)

### Features

- **screener**: Build miner submissions on Targon
  ([#608](https://github.com/ditto-assistant/ditto-subnet/pull/608),
  [`a52462f`](https://github.com/ditto-assistant/ditto-subnet/commit/a52462f34e150d9ed08c9d5655314787b216836e))


## v0.50.1 (2026-08-11)

### Bug Fixes

- **platform**: Ship benchmark v9 rollout contract
  ([#606](https://github.com/ditto-assistant/ditto-subnet/pull/606),
  [`626521a`](https://github.com/ditto-assistant/ditto-subnet/commit/626521a66a21d7f8f7634c2b2fd788bf64a17113))


## v0.50.0 (2026-08-10)

### Features

- **dittobench**: Add one-command v9 practice
  ([#605](https://github.com/ditto-assistant/ditto-subnet/pull/605),
  [`08dd75d`](https://github.com/ditto-assistant/ditto-subnet/commit/08dd75db09bef9cf90b36630d67c901a18334b7b))


## v0.49.2 (2026-08-10)

### Bug Fixes

- **dittobench**: Advertise executable bench v9
  ([#601](https://github.com/ditto-assistant/ditto-subnet/pull/601),
  [`daee4eb`](https://github.com/ditto-assistant/ditto-subnet/commit/daee4eb7c1e86b41b08641fe6231249523f9b043))

- **platform**: Ship self-contained relay releases
  ([#602](https://github.com/ditto-assistant/ditto-subnet/pull/602),
  [`778a2c3`](https://github.com/ditto-assistant/ditto-subnet/commit/778a2c3581d639028ca9df5e2180c802cc6d3c5d))


## v0.49.1 (2026-08-10)

### Bug Fixes

- **backroom**: Bind OAuth KV before Vite build
  ([#598](https://github.com/ditto-assistant/ditto-subnet/pull/598),
  [`cdde10e`](https://github.com/ditto-assistant/ditto-subnet/commit/cdde10efaefe6f707fbebd826f970767b870cb7a))

- **infra**: Permit autoscaler MIG handoff
  ([#597](https://github.com/ditto-assistant/ditto-subnet/pull/597),
  [`79428c6`](https://github.com/ditto-assistant/ditto-subnet/commit/79428c6ea426a1e4606604f025f3ccd828aacd04))


## v0.49.0 (2026-08-10)

### Bug Fixes

- **datagen**: Vary and floor the v9 scored family mix
  ([#577](https://github.com/ditto-assistant/ditto-subnet/pull/577),
  [`0b6d7e7`](https://github.com/ditto-assistant/ditto-subnet/commit/0b6d7e7968dc12faf10e52705917cf899500d793))

- **dittobench**: Keep adaptive ablations fail closed
  ([#589](https://github.com/ditto-assistant/ditto-subnet/pull/589),
  [`99f5c2d`](https://github.com/ditto-assistant/ditto-subnet/commit/99f5c2da92fa5b5628bad59536255a3b36a46169))

- **dittobench**: Keep zero-inference runs out of no-fault retries
  ([#574](https://github.com/ditto-assistant/ditto-subnet/pull/574),
  [`6327e39`](https://github.com/ditto-assistant/ditto-subnet/commit/6327e3971a8bf8effe153b216ab090818aeb83fc))

- **grade**: Harden canned-answer scoring for v9
  ([#578](https://github.com/ditto-assistant/ditto-subnet/pull/578),
  [`3421271`](https://github.com/ditto-assistant/ditto-subnet/commit/342127112311478afc6bc10a34b961cca0446904))

- **infra**: Grant autoscaler read for updates
  ([#596](https://github.com/ditto-assistant/ditto-subnet/pull/596),
  [`448e4a7`](https://github.com/ditto-assistant/ditto-subnet/commit/448e4a7b822fa6cbb5ee9f5a38c72706ef74c5da))

- **screener**: Require causal reachability for malicious preflight
  ([#575](https://github.com/ditto-assistant/ditto-subnet/pull/575),
  [`c18518f`](https://github.com/ditto-assistant/ditto-subnet/commit/c18518feaf3469b8dbbb534be4a23d339803c486))

- **screener**: Require causal role proof for benchmark findings
  ([#576](https://github.com/ditto-assistant/ditto-subnet/pull/576),
  [`cb6d968`](https://github.com/ditto-assistant/ditto-subnet/commit/cb6d968127b5d6fe6c38e6f43bf6593ea5887f24))

### Features

- **dittobench**: Add bounded v9 LongMemEval confirmation
  ([#581](https://github.com/ditto-assistant/ditto-subnet/pull/581),
  [`a4ff6ca`](https://github.com/ditto-assistant/ditto-subnet/commit/a4ff6cae7acc17d36a5cbcfe6049bc452c0b34dd))

- **dittobench**: Add v9 trusted inference and embedding ablations
  ([#582](https://github.com/ditto-assistant/ditto-subnet/pull/582),
  [`e2b7b14`](https://github.com/ditto-assistant/ditto-subnet/commit/e2b7b140ef18c81209e7a0e032a715a8cd82bdb6))

- **dittobench**: Blind v9 harness metadata
  ([#579](https://github.com/ditto-assistant/ditto-subnet/pull/579),
  [`c934abe`](https://github.com/ditto-assistant/ditto-subnet/commit/c934abea408887ff0dce8d1d9dbf1ac339ad569d))

- **dittobench**: Publish v9 model, tool, and reasoning gates
  ([#580](https://github.com/ditto-assistant/ditto-subnet/pull/580),
  [`c7b0f27`](https://github.com/ditto-assistant/ditto-subnet/commit/c7b0f274455c6e7ab23fcd552e0c21260a7b43fd))

- **dittobench**: Run bounded v9 top-N confirmation
  ([#583](https://github.com/ditto-assistant/ditto-subnet/pull/583),
  [`e19d3b4`](https://github.com/ditto-assistant/ditto-subnet/commit/e19d3b4b09b55069eef77a7b036aa6e033b72481))


## v0.48.6 (2026-08-10)

### Bug Fixes

- **screener**: Deploy checkout as owner
  ([#595](https://github.com/ditto-assistant/ditto-subnet/pull/595),
  [`2341743`](https://github.com/ditto-assistant/ditto-subnet/commit/2341743f96f23d755f23d51644a1c7e78623e113))


## v0.48.5 (2026-08-10)

### Bug Fixes

- **infra**: Permit MIG autoscaler read
  ([#592](https://github.com/ditto-assistant/ditto-subnet/pull/592),
  [`5e2bfb1`](https://github.com/ditto-assistant/ditto-subnet/commit/5e2bfb1ba259d87e2b8ca9b0bcef7ef6b77d6f60))

- **infra**: Separate autoscaler list binding
  ([#593](https://github.com/ditto-assistant/ditto-subnet/pull/593),
  [`2c27048`](https://github.com/ditto-assistant/ditto-subnet/commit/2c27048e1251a0b10f4325bb2c60ec6268914062))

- **infra**: Use valid MIG permissions
  ([#590](https://github.com/ditto-assistant/ditto-subnet/pull/590),
  [`20f9c7e`](https://github.com/ditto-assistant/ditto-subnet/commit/20f9c7eebd090527116afa607409b1f462d8ff47))

- **platform**: Inject controller bearer
  ([#591](https://github.com/ditto-assistant/ditto-subnet/pull/591),
  [`8ab4c4b`](https://github.com/ditto-assistant/ditto-subnet/commit/8ab4c4b9bf394e3e0f505d5c738e9c1dbba92498))

- **screener**: Hand off autoscaler during resize
  ([#594](https://github.com/ditto-assistant/ditto-subnet/pull/594),
  [`22825a7`](https://github.com/ditto-assistant/ditto-subnet/commit/22825a7836c9ddbb883fe00ef3e2f9ccdbfe8a82))


## v0.48.4 (2026-08-10)

### Bug Fixes

- **screener**: Activate Targon v3 controller
  ([#588](https://github.com/ditto-assistant/ditto-subnet/pull/588),
  [`849b7a9`](https://github.com/ditto-assistant/ditto-subnet/commit/849b7a97385e8620b7d8dafe984862c839f4b26e))


## v0.48.3 (2026-08-10)

### Bug Fixes

- **release**: Bridge frozen validator updaters
  ([#587](https://github.com/ditto-assistant/ditto-subnet/pull/587),
  [`1d56aae`](https://github.com/ditto-assistant/ditto-subnet/commit/1d56aae26a6153f7fdb094cc23700a44dcd96762))


## v0.48.2 (2026-08-10)

### Bug Fixes

- **release**: Publish Docker-compatible runtime indexes
  ([#586](https://github.com/ditto-assistant/ditto-subnet/pull/586),
  [`aca43fc`](https://github.com/ditto-assistant/ditto-subnet/commit/aca43fca9d43f9bccd67d1dfe118476474d22f6d))


## v0.48.1 (2026-08-10)

### Bug Fixes

- Preserve frozen updater release compatibility
  ([#553](https://github.com/ditto-assistant/ditto-subnet/pull/553),
  [`e19daf5`](https://github.com/ditto-assistant/ditto-subnet/commit/e19daf5daeae28c7014baa9d55065eb9be83dc0c))


## v0.48.0 (2026-08-09)

### Features

- **dittobench**: Add local v8 rehearsal command
  ([#585](https://github.com/ditto-assistant/ditto-subnet/pull/585),
  [`c63093c`](https://github.com/ditto-assistant/ditto-subnet/commit/c63093c5221f2b34ce717aa9ab0425488b55f279))


## v0.47.0 (2026-08-09)

### Chores

- **deps**: Bump pnpm/action-setup from 6.0.9 to 6.0.10 in the actions group
  ([#565](https://github.com/ditto-assistant/ditto-subnet/pull/565),
  [`76eb1ed`](https://github.com/ditto-assistant/ditto-subnet/commit/76eb1ed8bece98244cb1f0c010c7a1184df82e3d))

- **deps-dev**: Bump oxlint from 1.76.0 to 1.77.0 in /apps/platform/dashboard
  ([#564](https://github.com/ditto-assistant/ditto-subnet/pull/564),
  [`3de5acd`](https://github.com/ditto-assistant/ditto-subnet/commit/3de5acdab7a20d4c74f43d6fde6a764d679bc8b9))

### Features

- **backroom**: Ship the MCP agent-access page
  ([#572](https://github.com/ditto-assistant/ditto-subnet/pull/572),
  [`9c7cad5`](https://github.com/ditto-assistant/ditto-subnet/commit/9c7cad54899fca4f0d3003cbce7d4ea46bf52ada))


## v0.46.0 (2026-08-08)

### Bug Fixes

- **infra**: Make the ansible converge runnable as documented
  ([#567](https://github.com/ditto-assistant/ditto-subnet/pull/567),
  [`61695e2`](https://github.com/ditto-assistant/ditto-subnet/commit/61695e293b5167f666ca50816b5653b5bf040138))

- **infra**: Pin the compose project and stop reading a secret that does not exist
  ([#568](https://github.com/ditto-assistant/ditto-subnet/pull/568),
  [`5a347c9`](https://github.com/ditto-assistant/ditto-subnet/commit/5a347c90956ababc2029a32a8006e5cc27969ca4))

- **platform**: Reject a half-provisioned checkout before deploying
  ([#569](https://github.com/ditto-assistant/ditto-subnet/pull/569),
  [`b1284a4`](https://github.com/ditto-assistant/ditto-subnet/commit/b1284a4e7a67f9df1a55c2f388f7ad7819bda0c2))

### Features

- **platform**: Operator-controlled emission burn, and port the SN118 Backroom MCP
  ([#571](https://github.com/ditto-assistant/ditto-subnet/pull/571),
  [`b0f6ee8`](https://github.com/ditto-assistant/ditto-subnet/commit/b0f6ee814fe6137dba25ef61f016e2f107a82d9a))


## v0.45.4 (2026-08-07)

### Bug Fixes

- **release**: Mint datagen smoke token in auth action
  ([#552](https://github.com/ditto-assistant/ditto-subnet/pull/552),
  [`241c5cc`](https://github.com/ditto-assistant/ditto-subnet/commit/241c5ccaabba2d224624073d968bc3592c1ce473))


## v0.45.3 (2026-08-07)

### Bug Fixes

- **dittobench**: Restore the hosted v8 practice endpoint
  ([#555](https://github.com/ditto-assistant/ditto-subnet/pull/555),
  [`a71c940`](https://github.com/ditto-assistant/ditto-subnet/commit/a71c9403970b68ed6ed5304200a1a12d13510b0c))

- **platform**: Anchor the KOTH crown on the lineage, not the submission
  ([#561](https://github.com/ditto-assistant/ditto-subnet/pull/561),
  [`ab8f7c9`](https://github.com/ditto-assistant/ditto-subnet/commit/ab8f7c90046d5a2596f8ef7ff7329244e9ff0791))

- **platform**: Keep emission catch-up running between waves
  ([#554](https://github.com/ditto-assistant/ditto-subnet/pull/554),
  [`2a33637`](https://github.com/ditto-assistant/ditto-subnet/commit/2a336376e19123e0148050c335c79ef962c2e17f))

- **platform**: Preflight the monorepo checkout before deploying
  ([#556](https://github.com/ditto-assistant/ditto-subnet/pull/556),
  [`cda1972`](https://github.com/ditto-assistant/ditto-subnet/commit/cda19722defd8a81d2854f57e99e80c424e6cd6f))

- **platform**: Project only the KOTH detail keys the retest path reads
  ([#563](https://github.com/ditto-assistant/ditto-subnet/pull/563),
  [`b77a740`](https://github.com/ditto-assistant/ditto-subnet/commit/b77a740e02ef8f1d4178003216e199d75cd0dc55))

- **platform**: Serve legacy leaderboard families with zero-score children
  ([#557](https://github.com/ditto-assistant/ditto-subnet/pull/557),
  [`c21a1c8`](https://github.com/ditto-assistant/ditto-subnet/commit/c21a1c86fcaa389c398877d2ebb6bd9c47c8b1e5))

- **platform**: Stop hydrating score telemetry in the allocator floor read
  ([#559](https://github.com/ditto-assistant/ditto-subnet/pull/559),
  [`a64dc8b`](https://github.com/ditto-assistant/ditto-subnet/commit/a64dc8bfa182333bcaba503fb8be3d2537b008f7))


## v0.45.2 (2026-08-06)

### Bug Fixes

- **infra**: Import existing validator Pylon secrets
  ([#538](https://github.com/ditto-assistant/ditto-subnet/pull/538),
  [`220c095`](https://github.com/ditto-assistant/ditto-subnet/commit/220c0951b079bc93e361d4cb3911f7282c173fe3))

- **release**: Deploy datagen semantic releases
  ([#551](https://github.com/ditto-assistant/ditto-subnet/pull/551),
  [`6ad56f5`](https://github.com/ditto-assistant/ditto-subnet/commit/6ad56f56c5ced59efb9d408fcb941ffd72e3475b))


## v0.45.1 (2026-08-06)

### Bug Fixes

- **release**: Verify vendored relay source label
  ([#413](https://github.com/ditto-assistant/ditto-subnet/pull/413),
  [`8b04361`](https://github.com/ditto-assistant/ditto-subnet/commit/8b04361663f4731733094a90c9dbe71d12787626))

### Chores

- **ci**: Scope docker layer caches per release image
  ([#415](https://github.com/ditto-assistant/ditto-subnet/pull/415),
  [`c7c226a`](https://github.com/ditto-assistant/ditto-subnet/commit/c7c226ad448c9a4fff3360c9ec24d5a94bf77ed4))


## v0.45.0 (2026-08-06)

### Bug Fixes

- **ci**: Pass OIDC permission to reusable deploys
  ([#406](https://github.com/ditto-assistant/ditto-subnet/pull/406),
  [`27b6c02`](https://github.com/ditto-assistant/ditto-subnet/commit/27b6c02105064f1da68fdacec10c18f44d49acf3))

- **infra**: Fetch release ancestry before planning
  ([#407](https://github.com/ditto-assistant/ditto-subnet/pull/407),
  [`b568681`](https://github.com/ditto-assistant/ditto-subnet/commit/b5686810397aa0fe47ab49078431cc2190936fed))

- **release**: Harden screener capacity activation
  ([#382](https://github.com/ditto-assistant/ditto-subnet/pull/382),
  [`512067f`](https://github.com/ditto-assistant/ditto-subnet/commit/512067f1c5842f456f67ce3d42065bb648a02599))

- **release**: Require Python 3.12 for root package
  ([#412](https://github.com/ditto-assistant/ditto-subnet/pull/412),
  [`382b215`](https://github.com/ditto-assistant/ditto-subnet/commit/382b2153728229283c29bbf30cc3b2d49cd1cf8c))

- **release**: Reuse plan history for version gate
  ([#410](https://github.com/ditto-assistant/ditto-subnet/pull/410),
  [`3cc77ce`](https://github.com/ditto-assistant/ditto-subnet/commit/3cc77ceaabdb1ada2506adb0248e42df43a4f10b))

### Chores

- Refresh starter-kit anti-copy reference
  ([#405](https://github.com/ditto-assistant/ditto-subnet/pull/405),
  [`e234341`](https://github.com/ditto-assistant/ditto-subnet/commit/e234341daf56e09880b211886c72d6a9ba63c421))

- Simplify CODEOWNERS ownership assignments
  ([#409](https://github.com/ditto-assistant/ditto-subnet/pull/409),
  [`73ae003`](https://github.com/ditto-assistant/ditto-subnet/commit/73ae0036b9af4181c7cbccb5cc117620877cfa7b))

- **ci**: Migrate Platform operational workflows
  ([#352](https://github.com/ditto-assistant/ditto-subnet/pull/352),
  [`51467b4`](https://github.com/ditto-assistant/ditto-subnet/commit/51467b41d0fe5d2c049622e3862a149897e143e7))

- **deps**: Bump @tanstack/solid-query from 5.100.11 to 5.101.4 in /apps/platform/dashboard
  ([#400](https://github.com/ditto-assistant/ditto-subnet/pull/400),
  [`5cb2302`](https://github.com/ditto-assistant/ditto-subnet/commit/5cb2302e35d85291565c891d9486e32d5e97908c))

- **deps**: Bump golang from 1.23-alpine to 1.26-alpine in /services/dittobench-api
  ([#396](https://github.com/ditto-assistant/ditto-subnet/pull/396),
  [`9191ee1`](https://github.com/ditto-assistant/ditto-subnet/commit/9191ee1a13c784d5059f30fdaf64fb66dc179b63))

- **deps**: Bump huggingface/text-embeddings-inference from cpu-1.8.2 to cpu-1.9.3 in
  /apps/platform/docker/embedder ([#403](https://github.com/ditto-assistant/ditto-subnet/pull/403),
  [`411cdff`](https://github.com/ditto-assistant/ditto-subnet/commit/411cdff92f79a07bfb0f45f44dc7bdae4088b5fc))

- **deps**: Bump the actions group with 14 updates
  ([#404](https://github.com/ditto-assistant/ditto-subnet/pull/404),
  [`42d8a39`](https://github.com/ditto-assistant/ditto-subnet/commit/42d8a39a2cf546215deaf4aaf88d6fd59584b41b))

- **deps-dev**: Bump @types/node from 24.10.14 to 26.1.2 in /apps/platform/dashboard
  ([#398](https://github.com/ditto-assistant/ditto-subnet/pull/398),
  [`110bf16`](https://github.com/ditto-assistant/ditto-subnet/commit/110bf1600f9ec3e874da53aa2b6dcdb0371222c7))

- **deps-dev**: Bump typescript from 5.9.3 to 7.0.2 in /apps/platform/dashboard
  ([#402](https://github.com/ditto-assistant/ditto-subnet/pull/402),
  [`17baf7a`](https://github.com/ditto-assistant/ditto-subnet/commit/17baf7a35f602e33770392c07354cf30e375aa4e))

- **deps-dev**: Update setuptools requirement from <83,>=77 to >=77,<84 in
  /services/dittobench-api/integrations/hermes
  ([#399](https://github.com/ditto-assistant/ditto-subnet/pull/399),
  [`31cd7f4`](https://github.com/ditto-assistant/ditto-subnet/commit/31cd7f4812b06ebbd47e63fca352cd7393fe909e))

### Features

- Bring miner starter kit into monorepo
  ([#354](https://github.com/ditto-assistant/ditto-subnet/pull/354),
  [`2c9528b`](https://github.com/ditto-assistant/ditto-subnet/commit/2c9528bf6024499ab82892a1340c72f7efb5b9ab))

- Federate screener capacity across providers
  ([#349](https://github.com/ditto-assistant/ditto-subnet/pull/349),
  [`630e8a2`](https://github.com/ditto-assistant/ditto-subnet/commit/630e8a2428d3eacce1cb9166e5cd5df89dac165e))

- Gate hosted deploys and trusted builds
  ([#351](https://github.com/ditto-assistant/ditto-subnet/pull/351),
  [`5e9c319`](https://github.com/ditto-assistant/ditto-subnet/commit/5e9c31959c60ff82f7c7605919cc0450f3df514c))

- Migrate DittoBench datagen research
  ([#372](https://github.com/ditto-assistant/ditto-subnet/pull/372),
  [`b3f8080`](https://github.com/ditto-assistant/ditto-subnet/commit/b3f808089747f7cb2e93d80c88dec0cf864f3ea4))

- Migrate DittoBench into subnet monorepo
  ([#346](https://github.com/ditto-assistant/ditto-subnet/pull/346),
  [`21dbf7f`](https://github.com/ditto-assistant/ditto-subnet/commit/21dbf7f09260fbe1dd4d27114329115109417b57))

- Migrate Platform into subnet monorepo
  ([#347](https://github.com/ditto-assistant/ditto-subnet/pull/347),
  [`de8ab60`](https://github.com/ditto-assistant/ditto-subnet/commit/de8ab60f195a18d5d837c87c0e1da71c4dd7aec7))

- Migrate screener into subnet monorepo
  ([#348](https://github.com/ditto-assistant/ditto-subnet/pull/348),
  [`da8d767`](https://github.com/ditto-assistant/ditto-subnet/commit/da8d76785f67cb451ad8c771ef473b049d3eced5))

- Migrate subnet Backroom controls
  ([#350](https://github.com/ditto-assistant/ditto-subnet/pull/350),
  [`2b02d6c`](https://github.com/ditto-assistant/ditto-subnet/commit/2b02d6c1a3035d7840d8816aeb9989c6753498fe))

- **agent**: Add indexed monorepo skills
  ([#381](https://github.com/ditto-assistant/ditto-subnet/pull/381),
  [`91eb3df`](https://github.com/ditto-assistant/ditto-subnet/commit/91eb3df2e91c046d2470b946cb1866709634e248))

- **infra**: Migrate subnet runtime ownership
  ([#371](https://github.com/ditto-assistant/ditto-subnet/pull/371),
  [`08a9c93`](https://github.com/ditto-assistant/ditto-subnet/commit/08a9c93dfd6e2bdd967bb9961e516e51e4c4076f))

- **release**: Automate subnet runtime delivery
  ([#380](https://github.com/ditto-assistant/ditto-subnet/pull/380),
  [`5e00eb9`](https://github.com/ditto-assistant/ditto-subnet/commit/5e00eb95fddd74b616f27ae31a07065ef8de82be))

- **release**: Gate artifacts by affected component
  ([#345](https://github.com/ditto-assistant/ditto-subnet/pull/345),
  [`cb541e9`](https://github.com/ditto-assistant/ditto-subnet/commit/cb541e95c35d6d6e4eac0813164a2ea55a52eea7))


## v0.44.4 (2026-08-05)

### Bug Fixes

- Repin dittobench api ([#379](https://github.com/ditto-assistant/ditto-subnet/pull/379),
  [`a434630`](https://github.com/ditto-assistant/ditto-subnet/commit/a434630fe6525632ef4b7bff75df9c17cb61ade9))


## v0.44.3 (2026-08-05)

### Bug Fixes

- Preserve inference allowance failures
  ([#369](https://github.com/ditto-assistant/ditto-subnet/pull/369),
  [`3407969`](https://github.com/ditto-assistant/ditto-subnet/commit/340796919e4c460fe8b9c776ff6fb6560042ff80))


## v0.44.2 (2026-08-05)

### Bug Fixes

- Repin dittobench api ([#378](https://github.com/ditto-assistant/ditto-subnet/pull/378),
  [`a7b9685`](https://github.com/ditto-assistant/ditto-subnet/commit/a7b9685068fb88e200b1c8587aaa09661d8a33be))


## v0.44.1 (2026-08-04)

### Bug Fixes

- Restore v8 scorer negotiation on rolling upgrades
  ([#374](https://github.com/ditto-assistant/ditto-subnet/pull/374),
  [`fc694c7`](https://github.com/ditto-assistant/ditto-subnet/commit/fc694c7908817019018fa993a34f93110afc374d))


## v0.44.0 (2026-08-04)

### Chores

- **docs**: Align owner-link copy policy
  ([#307](https://github.com/ditto-assistant/ditto-subnet/pull/307),
  [`ffefa97`](https://github.com/ditto-assistant/ditto-subnet/commit/ffefa976962a64884cd39ab2501a0e9e02a70079))

- **security**: Lock CI supply chain inputs
  ([#324](https://github.com/ditto-assistant/ditto-subnet/pull/324),
  [`41b2926`](https://github.com/ditto-assistant/ditto-subnet/commit/41b29262988e564cfe64757e6422e475f6d7a548))

### Features

- **validator**: Retire pre-v8 scoring paths
  ([#370](https://github.com/ditto-assistant/ditto-subnet/pull/370),
  [`83a2c6b`](https://github.com/ditto-assistant/ditto-subnet/commit/83a2c6b61ce2b5ffd020e67a44cdc536683418f7))


## v0.43.17 (2026-08-04)

### Bug Fixes

- Repin dittobench api ([#368](https://github.com/ditto-assistant/ditto-subnet/pull/368),
  [`20b2a56`](https://github.com/ditto-assistant/ditto-subnet/commit/20b2a56a3c02de81178511e4eeccaac508b05361))


## v0.43.16 (2026-08-04)

### Bug Fixes

- **validator**: Recover orphaned bootstrap drains
  ([#366](https://github.com/ditto-assistant/ditto-subnet/pull/366),
  [`863011c`](https://github.com/ditto-assistant/ditto-subnet/commit/863011c830832f2e8c12dad5603578af6a2d822b))


## v0.43.15 (2026-08-03)

### Bug Fixes

- Ship patched Pylon weight transport
  ([#367](https://github.com/ditto-assistant/ditto-subnet/pull/367),
  [`75c5f02`](https://github.com/ditto-assistant/ditto-subnet/commit/75c5f023cfe86d17a75da29fb71452423772d5a0))


## v0.43.14 (2026-08-03)

### Bug Fixes

- Recover verified interrupted source updates
  ([#365](https://github.com/ditto-assistant/ditto-subnet/pull/365),
  [`09c2e06`](https://github.com/ditto-assistant/ditto-subnet/commit/09c2e06322f2f7a1d877aee99606292312090aad))


## v0.43.13 (2026-08-03)

### Bug Fixes

- **validator**: Keep idle slots through concurrent claim races
  ([#364](https://github.com/ditto-assistant/ditto-subnet/pull/364),
  [`3edf478`](https://github.com/ditto-assistant/ditto-subnet/commit/3edf4782f4c03fad9edc7257169024cd778be40c))


## v0.43.12 (2026-08-03)

### Bug Fixes

- Route hardcoded OpenRouter through scorer shim
  ([#362](https://github.com/ditto-assistant/ditto-subnet/pull/362),
  [`8cb5817`](https://github.com/ditto-assistant/ditto-subnet/commit/8cb58176168243bf5263727f5c7f5a62666d06c6))


## v0.43.11 (2026-08-03)

### Bug Fixes

- Repin dittobench api ([#361](https://github.com/ditto-assistant/ditto-subnet/pull/361),
  [`42b04fa`](https://github.com/ditto-assistant/ditto-subnet/commit/42b04fa598c76310dbccfd96d4eccc8fc9fe9b49))


## v0.43.10 (2026-08-03)

### Bug Fixes

- Drain source stack before scorer replacement
  ([#359](https://github.com/ditto-assistant/ditto-subnet/pull/359),
  [`81775cc`](https://github.com/ditto-assistant/ditto-subnet/commit/81775cc021b79ab3215253d46e6902b185874d1f))


## v0.43.9 (2026-08-03)

### Bug Fixes

- Honor elastic continual retest ceiling
  ([#360](https://github.com/ditto-assistant/ditto-subnet/pull/360),
  [`2ce7d7b`](https://github.com/ditto-assistant/ditto-subnet/commit/2ce7d7ba90da765bbdb06efe60050eff13e9ffff))


## v0.43.8 (2026-08-03)

### Bug Fixes

- Repin dittobench api ([#358](https://github.com/ditto-assistant/ditto-subnet/pull/358),
  [`b9e7314`](https://github.com/ditto-assistant/ditto-subnet/commit/b9e7314aa66714f1e983db83197c2ae6be5cae2f))


## v0.43.7 (2026-08-03)

### Bug Fixes

- Report relay recovery waits in validator heartbeats
  ([#357](https://github.com/ditto-assistant/ditto-subnet/pull/357),
  [`cee7aac`](https://github.com/ditto-assistant/ditto-subnet/commit/cee7aac1a0c856cccf95a5e1f6e729e39feca9e8))


## v0.43.6 (2026-08-02)

### Bug Fixes

- Repin dittobench api ([#356](https://github.com/ditto-assistant/ditto-subnet/pull/356),
  [`cb28f10`](https://github.com/ditto-assistant/ditto-subnet/commit/cb28f104c6b5094a9d3591d8651e0cdb74e0a27a))


## v0.43.5 (2026-08-02)

### Bug Fixes

- Route inference over the direct platform origin
  ([#355](https://github.com/ditto-assistant/ditto-subnet/pull/355),
  [`468e513`](https://github.com/ditto-assistant/ditto-subnet/commit/468e5131b80238ed4a1c608af484eeb77a4acf1c))


## v0.43.4 (2026-08-02)

### Bug Fixes

- Repin dittobench api ([#344](https://github.com/ditto-assistant/ditto-subnet/pull/344),
  [`b8611b7`](https://github.com/ditto-assistant/ditto-subnet/commit/b8611b74b93bbe81226482bdf8ba6c48fcf1486b))


## v0.43.3 (2026-08-02)

### Bug Fixes

- Retry transient inference exchange failures
  ([#343](https://github.com/ditto-assistant/ditto-subnet/pull/343),
  [`5f6f012`](https://github.com/ditto-assistant/ditto-subnet/commit/5f6f012be7c457872e131e20141d2d206216dd46))


## v0.43.2 (2026-08-02)

### Bug Fixes

- Let miners replace rejected payments
  ([#342](https://github.com/ditto-assistant/ditto-subnet/pull/342),
  [`9e01fcc`](https://github.com/ditto-assistant/ditto-subnet/commit/9e01fccc45a917558542f5b684e54ae977916829))


## v0.43.1 (2026-08-01)

### Bug Fixes

- Clarify TAO fee and paid retry windows
  ([#340](https://github.com/ditto-assistant/ditto-subnet/pull/340),
  [`dc8dc34`](https://github.com/ditto-assistant/ditto-subnet/commit/dc8dc34aa88c0f53e1f920d5f9ad87fa99978888))


## v0.43.0 (2026-08-01)

### Chores

- **tests**: Allow repeated monotonic progress heartbeats
  ([#338](https://github.com/ditto-assistant/ditto-subnet/pull/338),
  [`333f83b`](https://github.com/ditto-assistant/ditto-subnet/commit/333f83bb362d07681ec6fca6a28d84de6eca47f2))

### Features

- Fold relative efficiency after continual scores
  ([#318](https://github.com/ditto-assistant/ditto-subnet/pull/318),
  [`e71b25b`](https://github.com/ditto-assistant/ditto-subnet/commit/e71b25b5058c55cb146cafcd0b5690d41a5defb7))


## v0.42.18 (2026-08-01)

### Bug Fixes

- Repin dittobench api ([#337](https://github.com/ditto-assistant/ditto-subnet/pull/337),
  [`04c3dd3`](https://github.com/ditto-assistant/ditto-subnet/commit/04c3dd3d438ddedca292844848912d3a631a531a))


## v0.42.17 (2026-08-01)

### Bug Fixes

- **validator**: Retain delayed progress heartbeats
  ([#336](https://github.com/ditto-assistant/ditto-subnet/pull/336),
  [`e8446ae`](https://github.com/ditto-assistant/ditto-subnet/commit/e8446ae6417e362e30dbaee33bd9df699fd592cd))


## v0.42.16 (2026-07-31)

### Bug Fixes

- Repin dittobench api ([#335](https://github.com/ditto-assistant/ditto-subnet/pull/335),
  [`bf4d940`](https://github.com/ditto-assistant/ditto-subnet/commit/bf4d9402e356f0f4fad3c30f5bb6b31734a05f5d))


## v0.42.15 (2026-07-31)

### Bug Fixes

- Retain portable screened image identity
  ([#331](https://github.com/ditto-assistant/ditto-subnet/pull/331),
  [`21affa3`](https://github.com/ditto-assistant/ditto-subnet/commit/21affa38f79d7089c9713e0bed1f5f16e4b100e1))

- **validator**: Restore v0.41 sandbox compatibility
  ([#332](https://github.com/ditto-assistant/ditto-subnet/pull/332),
  [`3c876d6`](https://github.com/ditto-assistant/ditto-subnet/commit/3c876d655e95aec67d06f54aebe0e4d253082132))


## v0.42.14 (2026-07-31)

### Bug Fixes

- Poll validator updates every five minutes
  ([#330](https://github.com/ditto-assistant/ditto-subnet/pull/330),
  [`3209b00`](https://github.com/ditto-assistant/ditto-subnet/commit/3209b000776f791c78f875c40bf9778a58976b24))


## v0.42.13 (2026-07-31)

### Bug Fixes

- Defer unreadable AppArmor state to Docker
  ([#329](https://github.com/ditto-assistant/ditto-subnet/pull/329),
  [`762a9a9`](https://github.com/ditto-assistant/ditto-subnet/commit/762a9a9f31add31f6f518cf250ba0adea2bd9141))


## v0.42.12 (2026-07-31)

### Bug Fixes

- Allow non-root AppArmor preflight
  ([#328](https://github.com/ditto-assistant/ditto-subnet/pull/328),
  [`1ec4fbd`](https://github.com/ditto-assistant/ditto-subnet/commit/1ec4fbd30d689049a0fb7eb90bb141c5e6f80e95))


## v0.42.11 (2026-07-31)

### Bug Fixes

- Repin dittobench api ([#327](https://github.com/ditto-assistant/ditto-subnet/pull/327),
  [`5aded57`](https://github.com/ditto-assistant/ditto-subnet/commit/5aded57cd864946ca9e358b3563ad4e32422e90f))


## v0.42.10 (2026-07-31)

### Bug Fixes

- Reacquire pruned stack descriptors
  ([#326](https://github.com/ditto-assistant/ditto-subnet/pull/326),
  [`8254066`](https://github.com/ditto-assistant/ditto-subnet/commit/8254066c44028d402c0f23499788838494e4fe73))


## v0.42.9 (2026-07-31)

### Bug Fixes

- Support restricted AppArmor user namespaces
  ([#325](https://github.com/ditto-assistant/ditto-subnet/pull/325),
  [`c2946cb`](https://github.com/ditto-assistant/ditto-subnet/commit/c2946cb9415fdd82f60259e772f42e649bb3fab4))


## v0.42.8 (2026-07-31)

### Bug Fixes

- **release**: Preserve sandbox security defaults
  ([#323](https://github.com/ditto-assistant/ditto-subnet/pull/323),
  [`7e630c1`](https://github.com/ditto-assistant/ditto-subnet/commit/7e630c1964c9123cd44334e2a91fcc7884d7d0bb))


## v0.42.7 (2026-07-31)

### Bug Fixes

- **release**: Validate runtime dependencies
  ([#321](https://github.com/ditto-assistant/ditto-subnet/pull/321),
  [`f785da3`](https://github.com/ditto-assistant/ditto-subnet/commit/f785da3d76d96f13b5cf70cc3273aaa041f5b2f6))


## v0.42.6 (2026-07-31)

### Bug Fixes

- Retry transient stack dependency readiness
  ([#319](https://github.com/ditto-assistant/ditto-subnet/pull/319),
  [`84801a5`](https://github.com/ditto-assistant/ditto-subnet/commit/84801a504097406164f0dd672e763eca2823d252))


## v0.42.5 (2026-07-31)

### Bug Fixes

- Gate validator releases with updater e2e
  ([#317](https://github.com/ditto-assistant/ditto-subnet/pull/317),
  [`10f47ab`](https://github.com/ditto-assistant/ditto-subnet/commit/10f47ab9b87ec4b2943403c647096142440980e2))


## v0.42.4 (2026-07-31)

### Bug Fixes

- Support four-core validator hosts
  ([#316](https://github.com/ditto-assistant/ditto-subnet/pull/316),
  [`1b2925d`](https://github.com/ditto-assistant/ditto-subnet/commit/1b2925d5861f4ecffbb1f9f45fb827d1a1b22240))


## v0.42.3 (2026-07-31)

### Bug Fixes

- **release**: Restore frozen updater compatibility
  ([#315](https://github.com/ditto-assistant/ditto-subnet/pull/315),
  [`adeb108`](https://github.com/ditto-assistant/ditto-subnet/commit/adeb108ebcdcc8334d0728e011c4323edf0a6073))


## v0.42.2 (2026-07-31)

### Bug Fixes

- Repin dittobench api ([#314](https://github.com/ditto-assistant/ditto-subnet/pull/314),
  [`5d0553d`](https://github.com/ditto-assistant/ditto-subnet/commit/5d0553d7f4ded1c8c0a41a73323e85db58bf890b))

### Chores

- **v8**: Repin ordered world evidence
  ([#313](https://github.com/ditto-assistant/ditto-subnet/pull/313),
  [`b5a953a`](https://github.com/ditto-assistant/ditto-subnet/commit/b5a953a1e734200aba31ec1207731a246c0ff23d))


## v0.42.1 (2026-07-31)

### Bug Fixes

- **release**: Preserve the retired relay bridge
  ([#312](https://github.com/ditto-assistant/ditto-subnet/pull/312),
  [`f7a9b86`](https://github.com/ditto-assistant/ditto-subnet/commit/f7a9b8678bf99aa880273692a3a68834228656d4))


## v0.42.0 (2026-07-31)

### Features

- **validator**: Isolate v8 harness execution
  ([#304](https://github.com/ditto-assistant/ditto-subnet/pull/304),
  [`6dc3911`](https://github.com/ditto-assistant/ditto-subnet/commit/6dc3911393fa71f018e37a3149628e765f4d8090))


## v0.41.0 (2026-07-31)

### Features

- **docs**: Make harness submissions language-neutral
  ([#303](https://github.com/ditto-assistant/ditto-subnet/pull/303),
  [`b148951`](https://github.com/ditto-assistant/ditto-subnet/commit/b148951f9fe2a4f66293fcab0586c16f25c6adff))


## v0.40.5 (2026-07-31)

### Bug Fixes

- Dispatch retests from idle validator slots
  ([#311](https://github.com/ditto-assistant/ditto-subnet/pull/311),
  [`1d69300`](https://github.com/ditto-assistant/ditto-subnet/commit/1d693003ab4776aa8efb5d4c13ed37a05da25153))

- Probe live sandbox namespace health
  ([#309](https://github.com/ditto-assistant/ditto-subnet/pull/309),
  [`60c28b1`](https://github.com/ditto-assistant/ditto-subnet/commit/60c28b173c3f38a0a4ad0f9127904864c9e480c3))


## v0.40.4 (2026-07-30)

### Bug Fixes

- Keep continual retests using idle slots
  ([#310](https://github.com/ditto-assistant/ditto-subnet/pull/310),
  [`f9bfcfc`](https://github.com/ditto-assistant/ditto-subnet/commit/f9bfcfcb8d49c2253e1e341f466b7a22a879a863))


## v0.40.3 (2026-07-30)

### Bug Fixes

- **release**: Bridge frozen managed updaters
  ([#308](https://github.com/ditto-assistant/ditto-subnet/pull/308),
  [`7defe30`](https://github.com/ditto-assistant/ditto-subnet/commit/7defe30b8f098df5a30f4565c82501298d567a4f))


## v0.40.2 (2026-07-30)

### Bug Fixes

- **validator**: Fill idle slots with retest catchup
  ([#306](https://github.com/ditto-assistant/ditto-subnet/pull/306),
  [`a358e52`](https://github.com/ditto-assistant/ditto-subnet/commit/a358e52ec5ed52821d52a4a51d54c82dd2053dc9))

### Chores

- **docs**: Add owner-link signing runbook
  ([#301](https://github.com/ditto-assistant/ditto-subnet/pull/301),
  [`4d7f975`](https://github.com/ditto-assistant/ditto-subnet/commit/4d7f975070e2b9e4c6858dbc511cd12389cf960e))

- **weights**: Lock retained-sample fold semantics
  ([#302](https://github.com/ditto-assistant/ditto-subnet/pull/302),
  [`feddc78`](https://github.com/ditto-assistant/ditto-subnet/commit/feddc7824bad4ad101ff1b0771b64e954eb98858))


## v0.40.1 (2026-07-30)

### Bug Fixes

- Reuse payment for replacement uploads
  ([#300](https://github.com/ditto-assistant/ditto-subnet/pull/300),
  [`173500b`](https://github.com/ditto-assistant/ditto-subnet/commit/173500be7fe3d1ee4c018cf8cc68353e90f3490b))


## v0.40.0 (2026-07-29)

### Chores

- **miner**: Name the model v7 actually locks harnesses to
  ([#276](https://github.com/ditto-assistant/ditto-subnet/pull/276),
  [`d930817`](https://github.com/ditto-assistant/ditto-subnet/commit/d930817b6d6e0f43a57005fc58b68ef1d281a8d8))

### Features

- **miner-cli**: Offer owner link on wallet rotation
  ([#298](https://github.com/ditto-assistant/ditto-subnet/pull/298),
  [`e0bc188`](https://github.com/ditto-assistant/ditto-subnet/commit/e0bc1888a7f6e6bd2af58af1adbba3c8aa1d2296))

- **validator**: Decline to claim tickets when the host is out of headroom
  ([#283](https://github.com/ditto-assistant/ditto-subnet/pull/283),
  [`fd3cac1`](https://github.com/ditto-assistant/ditto-subnet/commit/fd3cac1ab0dd568c5f455cb69fe7c3f0f066c2bc))


## v0.39.1 (2026-07-29)

### Bug Fixes

- **miner**: Stop reporting a banked payment credit as a new submission
  ([#275](https://github.com/ditto-assistant/ditto-subnet/pull/275),
  [`63e5334`](https://github.com/ditto-assistant/ditto-subnet/commit/63e5334878cbc60c60ebe0fb75895a2d5f7e2c81))


## v0.39.0 (2026-07-29)

### Chores

- **miner**: Drop the preflight handler requirement
  ([#293](https://github.com/ditto-assistant/ditto-subnet/pull/293),
  [`5bf5f6c`](https://github.com/ditto-assistant/ditto-subnet/commit/5bf5f6c60b272b93282e7be8627c30e278fe31b7))

### Features

- Retire validator-local inference sidecars
  ([#295](https://github.com/ditto-assistant/ditto-subnet/pull/295),
  [`6ecfbe2`](https://github.com/ditto-assistant/ditto-subnet/commit/6ecfbe2d94c3bb1ee9143ad04c101b20164fe87e))

- **validator**: Negotiate gated benchmark v8
  ([#294](https://github.com/ditto-assistant/ditto-subnet/pull/294),
  [`6182108`](https://github.com/ditto-assistant/ditto-subnet/commit/618210842c7913bd3e49ccb973db4b0f031db254))


## v0.38.0 (2026-07-29)

### Features

- **miner-cli**: Add `ditto attest` for symmetric owner links
  ([#278](https://github.com/ditto-assistant/ditto-subnet/pull/278),
  [`9c88790`](https://github.com/ditto-assistant/ditto-subnet/commit/9c8879004e49c5d15189140bd912b53484ec8b5e))


## v0.37.6 (2026-07-29)

### Bug Fixes

- Repin dittobench api ([#297](https://github.com/ditto-assistant/ditto-subnet/pull/297),
  [`695599e`](https://github.com/ditto-assistant/ditto-subnet/commit/695599eb76d5f7e56d392f4ba58ea7eadac2e7f1))


## v0.37.5 (2026-07-28)

### Bug Fixes

- Repin dittobench api ([#292](https://github.com/ditto-assistant/ditto-subnet/pull/292),
  [`a89da6f`](https://github.com/ditto-assistant/ditto-subnet/commit/a89da6fe951b58657c27f212632b09a5cacf4087))


## v0.37.4 (2026-07-28)

### Bug Fixes

- **validator**: Widen failure_detail from 200 to 4096 chars
  ([#291](https://github.com/ditto-assistant/ditto-subnet/pull/291),
  [`1b4f031`](https://github.com/ditto-assistant/ditto-subnet/commit/1b4f031f3371bcd858971c064fe02e73a8039260))


## v0.37.3 (2026-07-28)

### Bug Fixes

- **validator**: Keep agent-attributable inference declines out of no-fault
  ([#288](https://github.com/ditto-assistant/ditto-subnet/pull/288),
  [`77613de`](https://github.com/ditto-assistant/ditto-subnet/commit/77613de223e76f3191f06770809626fdf1e5782b))

- **validator**: Stop charging screened-image acquisition to the miner
  ([#286](https://github.com/ditto-assistant/ditto-subnet/pull/286),
  [`b7a0e0c`](https://github.com/ditto-assistant/ditto-subnet/commit/b7a0e0c207925ea32f6bbceda047bfb2cba29322))


## v0.37.2 (2026-07-28)

### Bug Fixes

- Repin dittobench api ([#289](https://github.com/ditto-assistant/ditto-subnet/pull/289),
  [`da4bc6a`](https://github.com/ditto-assistant/ditto-subnet/commit/da4bc6a064ef20092988e56830a598c34579ef5d))


## v0.37.1 (2026-07-28)

### Bug Fixes

- **validator**: Stop retiring idle slots for the rest of the sweep
  ([#287](https://github.com/ditto-assistant/ditto-subnet/pull/287),
  [`d848a27`](https://github.com/ditto-assistant/ditto-subnet/commit/d848a271b381921d241e55ff2a6abe03cc5bbfc9))


## v0.37.0 (2026-07-28)

### Bug Fixes

- Repin dittobench api ([#285](https://github.com/ditto-assistant/ditto-subnet/pull/285),
  [`e908e86`](https://github.com/ditto-assistant/ditto-subnet/commit/e908e862f47ce71a8941825b17441fc2aeccbf6a))

### Features

- **validator**: Cancel a run whose lease the platform revoked (heartbeat v17)
  ([#284](https://github.com/ditto-assistant/ditto-subnet/pull/284),
  [`de6b893`](https://github.com/ditto-assistant/ditto-subnet/commit/de6b8934a7a96426a8210224d4345ad49a712cfe))


## v0.36.0 (2026-07-27)

### Features

- **validator**: Advertise the protocol maximum eight slots
  ([#280](https://github.com/ditto-assistant/ditto-subnet/pull/280),
  [`08959ec`](https://github.com/ditto-assistant/ditto-subnet/commit/08959ecd14bd82af41f03425c5c475502a4978c7))


## v0.35.2 (2026-07-27)

### Bug Fixes

- **validator**: Report which failure killed the run, not just its class
  ([#282](https://github.com/ditto-assistant/ditto-subnet/pull/282),
  [`7f81e0f`](https://github.com/ditto-assistant/ditto-subnet/commit/7f81e0f177ca704ba63cfb2ed135a0c24a36376d))


## v0.35.1 (2026-07-27)

### Bug Fixes

- **validator**: Resolve every ticket before its lease expires
  ([#279](https://github.com/ditto-assistant/ditto-subnet/pull/279),
  [`f9e9ccf`](https://github.com/ditto-assistant/ditto-subnet/commit/f9e9ccfb918791f45ff54d072770892406a83dcd))


## v0.35.0 (2026-07-27)

### Chores

- **miner**: Publish the score-to-incentive propagation SLA
  ([#272](https://github.com/ditto-assistant/ditto-subnet/pull/272),
  [`50a69ec`](https://github.com/ditto-assistant/ditto-subnet/commit/50a69ecdde04a9a91e03be04800156ef3a240d6a))

### Features

- **validator**: Report a leased slot from the moment it is claimed (v16)
  ([#274](https://github.com/ditto-assistant/ditto-subnet/pull/274),
  [`445e8a1`](https://github.com/ditto-assistant/ditto-subnet/commit/445e8a19ba77cb3820fa5f7947374362b3d7aadf))


## v0.34.1 (2026-07-26)

### Bug Fixes

- **validator**: Resume weights on the epoch remainder after a drain
  ([#273](https://github.com/ditto-assistant/ditto-subnet/pull/273),
  [`f209378`](https://github.com/ditto-assistant/ditto-subnet/commit/f20937889a06b91c5dfdc0673117d7ec126f8ca3))


## v0.34.0 (2026-07-26)

### Features

- **validator**: Plan the retest round the operator asked for
  ([#269](https://github.com/ditto-assistant/ditto-subnet/pull/269),
  [`db9daff`](https://github.com/ditto-assistant/ditto-subnet/commit/db9daff5678da40e837718cfb4582d340f8da26c))


## v0.33.3 (2026-07-26)

### Bug Fixes

- **validator**: Report retest progress on the slot the platform leased
  ([#270](https://github.com/ditto-assistant/ditto-subnet/pull/270),
  [`76a8fea`](https://github.com/ditto-assistant/ditto-subnet/commit/76a8fea3b18d6aaa02418c24ab1f2ec46e1a1b0d))


## v0.33.2 (2026-07-26)

### Bug Fixes

- Repin dittobench api ([#268](https://github.com/ditto-assistant/ditto-subnet/pull/268),
  [`340aef2`](https://github.com/ditto-assistant/ditto-subnet/commit/340aef2a5bf89d43e283f5a4970fdb42a3c39fde))


## v0.33.1 (2026-07-26)

### Bug Fixes

- Repin dittobench api ([#267](https://github.com/ditto-assistant/ditto-subnet/pull/267),
  [`a70c6b8`](https://github.com/ditto-assistant/ditto-subnet/commit/a70c6b8d9cbf15eb60e1e7158f80f069b4b3e68f))


## v0.33.0 (2026-07-25)

### Chores

- Sync LedgerEntry wire copy with the platform contract
  ([#266](https://github.com/ditto-assistant/ditto-subnet/pull/266),
  [`a00603f`](https://github.com/ditto-assistant/ditto-subnet/commit/a00603f2dedad28cd35dd88f59b6e956967aa7db))

### Features

- Retire the legacy validator-only auto-updater
  ([#172](https://github.com/ditto-assistant/ditto-subnet/pull/172),
  [`ab6542b`](https://github.com/ditto-assistant/ditto-subnet/commit/ab6542bb92401ce1cc6540c4dfaf3525a658792c))


## v0.32.3 (2026-07-25)

### Bug Fixes

- Repin dittobench api ([#263](https://github.com/ditto-assistant/ditto-subnet/pull/263),
  [`2e79d4d`](https://github.com/ditto-assistant/ditto-subnet/commit/2e79d4d40aab0a7302d0fc6c2f11e217d3082312))


## v0.32.2 (2026-07-25)

### Bug Fixes

- **validator**: Claim a benchmark slot before the inference hand-off
  ([#265](https://github.com/ditto-assistant/ditto-subnet/pull/265),
  [`31e5d2c`](https://github.com/ditto-assistant/ditto-subnet/commit/31e5d2cab5ee55f41ee07af18be0097bb4852a30))


## v0.32.1 (2026-07-25)

### Bug Fixes

- **validator**: Stop a dead local Ollama from blocking benchmark v7
  ([#264](https://github.com/ditto-assistant/ditto-subnet/pull/264),
  [`9c9e3f1`](https://github.com/ditto-assistant/ditto-subnet/commit/9c9e3f1f87327ed7bbe165d7906ad94189e678a7))


## v0.32.0 (2026-07-25)

### Features

- **validator**: Ship real bench-slot concurrency and stop failing closed on it
  ([#258](https://github.com/ditto-assistant/ditto-subnet/pull/258),
  [`64ed758`](https://github.com/ditto-assistant/ditto-subnet/commit/64ed758923c45f09a34238168bd85b388d5457a6))


## v0.31.0 (2026-07-25)

### Bug Fixes

- Repin dittobench api ([#262](https://github.com/ditto-assistant/ditto-subnet/pull/262),
  [`8cf05f1`](https://github.com/ditto-assistant/ditto-subnet/commit/8cf05f178e8956d3dc6097030e69e29feef43250))

### Features

- **validator**: Expose hosted v7 parallelism per host, keep the Ollama lane pinned
  ([#261](https://github.com/ditto-assistant/ditto-subnet/pull/261),
  [`2864af0`](https://github.com/ditto-assistant/ditto-subnet/commit/2864af0041f3193f940e971827ef1edf31ad59a2))


## v0.30.5 (2026-07-25)

### Bug Fixes

- **validator**: Publish the generating_dataset progress stage
  ([#260](https://github.com/ditto-assistant/ditto-subnet/pull/260),
  [`b7d8555`](https://github.com/ditto-assistant/ditto-subnet/commit/b7d85557759cdece5439c83b1f993425007d43b2))


## v0.30.4 (2026-07-25)

### Bug Fixes

- Repin dittobench api ([#259](https://github.com/ditto-assistant/ditto-subnet/pull/259),
  [`426274e`](https://github.com/ditto-assistant/ditto-subnet/commit/426274e6edd2a40905156e6ebc98a925a4a7231d))


## v0.30.3 (2026-07-25)

### Bug Fixes

- **validator**: Default the inference base URL to the host production mints
  ([#257](https://github.com/ditto-assistant/ditto-subnet/pull/257),
  [`78da2ff`](https://github.com/ditto-assistant/ditto-subnet/commit/78da2ffe95fa84b5f13d4158388fefdcd367690b))


## v0.30.2 (2026-07-25)

### Bug Fixes

- Repin dittobench api ([#256](https://github.com/ditto-assistant/ditto-subnet/pull/256),
  [`bd3ca0b`](https://github.com/ditto-assistant/ditto-subnet/commit/bd3ca0bf6af4c78433653bd258e684c1f1daeb77))

- **miner**: Make paid uploads resilient
  ([#249](https://github.com/ditto-assistant/ditto-subnet/pull/249),
  [`e3bb24c`](https://github.com/ditto-assistant/ditto-subnet/commit/e3bb24c12381d1d2c714cf0e10f93b11d802bab3))


## v0.30.1 (2026-07-25)

### Bug Fixes

- **validator**: Allow the platform's inference host to differ from its API host
  ([#255](https://github.com/ditto-assistant/ditto-subnet/pull/255),
  [`a5e8be8`](https://github.com/ditto-assistant/ditto-subnet/commit/a5e8be8ff3cdebbffd7b8430244b9a47c94da1fc))


## v0.30.0 (2026-07-25)

### Features

- Report whether the scorer is serving, not just what it concluded
  ([#251](https://github.com/ditto-assistant/ditto-subnet/pull/251),
  [`24abdaf`](https://github.com/ditto-assistant/ditto-subnet/commit/24abdafda56b0115bf23ce96e8c273c86c7d616b))


## v0.29.8 (2026-07-25)

### Bug Fixes

- **sandbox**: Reject denied egress instead of dropping it
  ([#253](https://github.com/ditto-assistant/ditto-subnet/pull/253),
  [`46d6770`](https://github.com/ditto-assistant/ditto-subnet/commit/46d67703c89be054576f05def8865d7110e50a0e))


## v0.29.7 (2026-07-25)

### Bug Fixes

- Set the scorer's platform inference proxy URL (and move it to dittobench.ai)
  ([#252](https://github.com/ditto-assistant/ditto-subnet/pull/252),
  [`5a5123c`](https://github.com/ditto-assistant/ditto-subnet/commit/5a5123cf41d520b59b5f41f383180260f1863c48))


## v0.29.6 (2026-07-25)

### Bug Fixes

- Authorize the validator on the scorer inference control plane
  ([#250](https://github.com/ditto-assistant/ditto-subnet/pull/250),
  [`c7f64d7`](https://github.com/ditto-assistant/ditto-subnet/commit/c7f64d7f1f84a24f532e875ad2416da55e4e6d47))


## v0.29.5 (2026-07-25)

### Bug Fixes

- Stop descriptive scorer metadata from disabling bench v7
  ([#248](https://github.com/ditto-assistant/ditto-subnet/pull/248),
  [`7f8fa49`](https://github.com/ditto-assistant/ditto-subnet/commit/7f8fa49901da26c7d0236525eef679956910d189))


## v0.29.4 (2026-07-25)

### Bug Fixes

- Stop a stale scorer image from persisting or misreporting itself
  ([#247](https://github.com/ditto-assistant/ditto-subnet/pull/247),
  [`59adabc`](https://github.com/ditto-assistant/ditto-subnet/commit/59adabc37b19f31e56a01e28e7b733fe71175dfe))


## v0.29.3 (2026-07-24)

### Bug Fixes

- Repin dittobench api ([#224](https://github.com/ditto-assistant/ditto-subnet/pull/224),
  [`ed2cccf`](https://github.com/ditto-assistant/ditto-subnet/commit/ed2cccfef70e6aa1fac824c45c40395985b901b3))


## v0.29.2 (2026-07-24)

### Bug Fixes

- Require admission before upload payment
  ([#245](https://github.com/ditto-assistant/ditto-subnet/pull/245),
  [`9006bcd`](https://github.com/ditto-assistant/ditto-subnet/commit/9006bcd19c50e63fb9c00def030bb4b4d8ab762f))


## v0.29.1 (2026-07-24)

### Bug Fixes

- Retry hosted embedding outages ([#239](https://github.com/ditto-assistant/ditto-subnet/pull/239),
  [`f47e3c4`](https://github.com/ditto-assistant/ditto-subnet/commit/f47e3c4663bb1700b59930108d5c81112e4f731f))


## v0.29.0 (2026-07-24)

### Features

- Fold completed retest waves into miner scores
  ([#244](https://github.com/ditto-assistant/ditto-subnet/pull/244),
  [`34e4a1e`](https://github.com/ditto-assistant/ditto-subnet/commit/34e4a1ee7cd855efdda34f33127cc912b2fc4d83))


## v0.28.0 (2026-07-24)

### Chores

- **skills**: Add GitHub Stacks workflow
  ([#241](https://github.com/ditto-assistant/ditto-subnet/pull/241),
  [`1eff7af`](https://github.com/ditto-assistant/ditto-subnet/commit/1eff7af6757f45118946968a7385687799aeb3ae))

### Features

- **miner**: Handle coldkey submission cooldown
  ([#242](https://github.com/ditto-assistant/ditto-subnet/pull/242),
  [`2b1ff67`](https://github.com/ditto-assistant/ditto-subnet/commit/2b1ff67916f7a37deceb9979d9224163016f0459))


## v0.27.0 (2026-07-24)

### Features

- Support single-seed top-five retest waves
  ([#240](https://github.com/ditto-assistant/ditto-subnet/pull/240),
  [`1a5e30c`](https://github.com/ditto-assistant/ditto-subnet/commit/1a5e30cdde42cafef5b574c908cc7b9869ded1db))


## v0.26.0 (2026-07-23)

### Features

- Verify signed kings and react to changes
  ([#188](https://github.com/ditto-assistant/ditto-subnet/pull/188),
  [`de1f8f0`](https://github.com/ditto-assistant/ditto-subnet/commit/de1f8f0b907115b9b0c40e861ea4f4bf98017bbe))


## v0.25.3 (2026-07-23)

### Bug Fixes

- Poll for benchmark jobs every 30 seconds
  ([#227](https://github.com/ditto-assistant/ditto-subnet/pull/227),
  [`01d467b`](https://github.com/ditto-assistant/ditto-subnet/commit/01d467b65de73fcbea2c9bad43cac1e86224a28f))


## v0.25.2 (2026-07-23)

### Bug Fixes

- Advance validator slots after sandbox OOM
  ([#223](https://github.com/ditto-assistant/ditto-subnet/pull/223),
  [`f94e76e`](https://github.com/ditto-assistant/ditto-subnet/commit/f94e76e7404cfdc8e6de2817952afd87ba948112))


## v0.25.1 (2026-07-23)

### Bug Fixes

- Consume pinned continual retest datasets
  ([#226](https://github.com/ditto-assistant/ditto-subnet/pull/226),
  [`89aaeb6`](https://github.com/ditto-assistant/ditto-subnet/commit/89aaeb657439adba443113270e9cc1bffe7d9f91))


## v0.25.0 (2026-07-23)

### Features

- Decay high-score dethrone bands from bench v6
  ([#225](https://github.com/ditto-assistant/ditto-subnet/pull/225),
  [`995a73e`](https://github.com/ditto-assistant/ditto-subnet/commit/995a73e0b3a7c5886ff37b351a11e53bb7416ffa))


## v0.24.1 (2026-07-23)

### Bug Fixes

- Repin dittobench api ([#217](https://github.com/ditto-assistant/ditto-subnet/pull/217),
  [`25e1922`](https://github.com/ditto-assistant/ditto-subnet/commit/25e1922e2b398d667068d3f9cb85930a47ee9c7f))


## v0.24.0 (2026-07-23)

### Features

- Rebalance KOTH rewards and dethrone hysteresis
  ([#216](https://github.com/ditto-assistant/ditto-subnet/pull/216),
  [`491c9b2`](https://github.com/ditto-assistant/ditto-subnet/commit/491c9b2d45384e31792b57bc45959ba4d61d1d19))


## v0.23.1 (2026-07-23)

### Bug Fixes

- Preserve v6 ticket compatibility
  ([#222](https://github.com/ditto-assistant/ditto-subnet/pull/222),
  [`389e4f5`](https://github.com/ditto-assistant/ditto-subnet/commit/389e4f59ca5a83ef8b926ba92c118d5798cd6031))


## v0.23.0 (2026-07-23)

### Features

- Run benchmark v7 on ticket-bound routes
  ([#214](https://github.com/ditto-assistant/ditto-subnet/pull/214),
  [`88fed20`](https://github.com/ditto-assistant/ditto-subnet/commit/88fed20e062795d3f1998c4b6f0fbdb723cec379))

- Verify validator-scoped dataset seeds
  ([#220](https://github.com/ditto-assistant/ditto-subnet/pull/220),
  [`b63ee22`](https://github.com/ditto-assistant/ditto-subnet/commit/b63ee22670dda0d08193ca50971c6af665b8816c))


## v0.22.1 (2026-07-23)

### Bug Fixes

- Negotiate scorer benchmark version overlap
  ([#219](https://github.com/ditto-assistant/ditto-subnet/pull/219),
  [`0a1ca37`](https://github.com/ditto-assistant/ditto-subnet/commit/0a1ca3747745d60b4cbd4e6c71892158fd0a1f53))


## v0.22.0 (2026-07-22)

### Chores

- **docs**: Link the public platform repository
  ([#215](https://github.com/ditto-assistant/ditto-subnet/pull/215),
  [`a7fd5f0`](https://github.com/ditto-assistant/ditto-subnet/commit/a7fd5f0e5670166d16637831fdd1af1903b8c1a3))

### Features

- Continually re-benchmark the KOTH top five
  ([#202](https://github.com/ditto-assistant/ditto-subnet/pull/202),
  [`c2619a2`](https://github.com/ditto-assistant/ditto-subnet/commit/c2619a28c58520f278aebbe2349db04cc6836f8a))

- Run bounded parallel validator slots
  ([#211](https://github.com/ditto-assistant/ditto-subnet/pull/211),
  [`ca2cc99`](https://github.com/ditto-assistant/ditto-subnet/commit/ca2cc9938d9f12135ee04a80c2dfe55437e27b8e))


## v0.21.6 (2026-07-21)

### Bug Fixes

- Repin dittobench api ([#213](https://github.com/ditto-assistant/ditto-subnet/pull/213),
  [`f5c0309`](https://github.com/ditto-assistant/ditto-subnet/commit/f5c030999e307ec5278c9a69e9662cdb938e45de))


## v0.21.5 (2026-07-21)

### Bug Fixes

- Accept DittoBench v6 capability ([#212](https://github.com/ditto-assistant/ditto-subnet/pull/212),
  [`f1ffd1b`](https://github.com/ditto-assistant/ditto-subnet/commit/f1ffd1b4da8f25e8c45c7588448450430d544a97))

### Chores

- Remove Python 3.11 tests ([#210](https://github.com/ditto-assistant/ditto-subnet/pull/210),
  [`239b74e`](https://github.com/ditto-assistant/ditto-subnet/commit/239b74ec4fa85d1d721480d7c733f1bd5ed82c7a))


## v0.21.4 (2026-07-21)

### Bug Fixes

- Repin dittobench api ([#209](https://github.com/ditto-assistant/ditto-subnet/pull/209),
  [`2e30f40`](https://github.com/ditto-assistant/ditto-subnet/commit/2e30f407f753b7d81720c927dd20dd7e67c00838))


## v0.21.3 (2026-07-21)

### Bug Fixes

- Repin dittobench api ([#207](https://github.com/ditto-assistant/ditto-subnet/pull/207),
  [`2385b05`](https://github.com/ditto-assistant/ditto-subnet/commit/2385b05cced7947ec731badc581f7f76bedc5dd5))


## v0.21.2 (2026-07-21)

### Bug Fixes

- Repin dittobench api ([#206](https://github.com/ditto-assistant/ditto-subnet/pull/206),
  [`9c6eafa`](https://github.com/ditto-assistant/ditto-subnet/commit/9c6eafa21a00b71aa3c52f6f1cc0e3a24a78f775))


## v0.21.1 (2026-07-21)

### Bug Fixes

- Repin dittobench api ([#204](https://github.com/ditto-assistant/ditto-subnet/pull/204),
  [`9677d09`](https://github.com/ditto-assistant/ditto-subnet/commit/9677d09f7b37ab9c8ec4b8f26e66f7e9e318314b))

- **validator**: Relay preflight so a broken stack self-excludes instead of wedging agents
  ([#205](https://github.com/ditto-assistant/ditto-subnet/pull/205),
  [`f9f3e77`](https://github.com/ditto-assistant/ditto-subnet/commit/f9f3e77369d9b3320472f84aab22cdeba1c614aa))


## v0.21.0 (2026-07-21)

### Features

- Support benchmark v5 waste-penalty reports
  ([#194](https://github.com/ditto-assistant/ditto-subnet/pull/194),
  [`1b5f9c4`](https://github.com/ditto-assistant/ditto-subnet/commit/1b5f9c4798c47c405cb9b951e70012e5dd79bac5))


## v0.20.2 (2026-07-21)

### Bug Fixes

- Repin dittobench api ([#203](https://github.com/ditto-assistant/ditto-subnet/pull/203),
  [`d6ec1d7`](https://github.com/ditto-assistant/ditto-subnet/commit/d6ec1d7e63fee5d2a81d94c52121623cf65a903d))

### Chores

- **docs**: Define coldkey-level emission contract
  ([#201](https://github.com/ditto-assistant/ditto-subnet/pull/201),
  [`b78996a`](https://github.com/ditto-assistant/ditto-subnet/commit/b78996a4f59d085f0a9863aeeda8bacf9bb34773))


## v0.20.1 (2026-07-21)

### Bug Fixes

- Repin dittobench api ([#200](https://github.com/ditto-assistant/ditto-subnet/pull/200),
  [`306e30a`](https://github.com/ditto-assistant/ditto-subnet/commit/306e30a1b69db7bb9e1dd9e2b24545da2e7db75b))

### Chores

- **ci**: Pipeline the release + add a deploy-time compose gate
  ([#193](https://github.com/ditto-assistant/ditto-subnet/pull/193),
  [`200dea4`](https://github.com/ditto-assistant/ditto-subnet/commit/200dea40acf89082b07dd82c9ec4d9d808f1ab76))

- **tests**: Correct dns_opt terminology
  ([#199](https://github.com/ditto-assistant/ditto-subnet/pull/199),
  [`353f372`](https://github.com/ditto-assistant/ditto-subnet/commit/353f372f911ad0aacff505be87c52f760327972d))


## v0.20.0 (2026-07-20)

### Features

- **validator**: Report failed tickets for reissue + per-run token progress
  ([#197](https://github.com/ditto-assistant/ditto-subnet/pull/197),
  [`f9ddd09`](https://github.com/ditto-assistant/ditto-subnet/commit/f9ddd09f33036071d9fac6537693ef4d0efa1567))


## v0.19.4 (2026-07-20)

### Bug Fixes

- Repin dittobench api ([#196](https://github.com/ditto-assistant/ditto-subnet/pull/196),
  [`7565620`](https://github.com/ditto-assistant/ditto-subnet/commit/75656201927aa86acca79d7c37741d95d8ae60a0))


## v0.19.3 (2026-07-20)

### Bug Fixes

- **deps**: Require bittensor >= 10.3.0
  ([#195](https://github.com/ditto-assistant/ditto-subnet/pull/195),
  [`2421378`](https://github.com/ditto-assistant/ditto-subnet/commit/24213789e2ef643a806a3b8b95b1cacba1ae4b05))


## v0.19.2 (2026-07-20)

### Bug Fixes

- **compose**: Move host.docker.internal mapping to sandbox-docker (netns owner)
  ([#192](https://github.com/ditto-assistant/ditto-subnet/pull/192),
  [`67eb397`](https://github.com/ditto-assistant/ditto-subnet/commit/67eb397485328b9a647353902ea3c1ff285f9753))


## v0.19.1 (2026-07-20)

### Bug Fixes

- **scorer**: Resolve host.docker.internal so the relay preflight reaches the model relay
  ([#191](https://github.com/ditto-assistant/ditto-subnet/pull/191),
  [`0a74ab3`](https://github.com/ditto-assistant/ditto-subnet/commit/0a74ab351da9b7dd77bd39c49180ed465286348e))


## v0.19.0 (2026-07-20)

### Features

- **progress**: Granular generating_dataset / starting_harness stages
  ([#190](https://github.com/ditto-assistant/ditto-subnet/pull/190),
  [`03d4f0e`](https://github.com/ditto-assistant/ditto-subnet/commit/03d4f0e60b2e6acb0808ff33139380e1f6ba5966))


## v0.18.4 (2026-07-20)

### Bug Fixes

- Repin dittobench api ([#187](https://github.com/ditto-assistant/ditto-subnet/pull/187),
  [`0e8bca9`](https://github.com/ditto-assistant/ditto-subnet/commit/0e8bca982049a488cc390bc52eca4990e3d04e92))


## v0.18.3 (2026-07-20)

### Bug Fixes

- **sandbox**: Stop the maintenance loop from deleting the ditto-sandbox network
  ([#186](https://github.com/ditto-assistant/ditto-subnet/pull/186),
  [`89346bd`](https://github.com/ditto-assistant/ditto-subnet/commit/89346bd227253fb2848b848098862e6d9d6f450b))


## v0.18.2 (2026-07-20)

### Bug Fixes

- Recover transient paid uploads ([#184](https://github.com/ditto-assistant/ditto-subnet/pull/184),
  [`5213165`](https://github.com/ditto-assistant/ditto-subnet/commit/5213165436a0dd4e1935c872cf07d22534cb9d9d))


## v0.18.1 (2026-07-20)

### Bug Fixes

- Repin dittobench api ([#185](https://github.com/ditto-assistant/ditto-subnet/pull/185),
  [`ffa9b43`](https://github.com/ditto-assistant/ditto-subnet/commit/ffa9b436b9d45d8becf82af42071b290e89876fe))


## v0.18.0 (2026-07-19)

### Chores

- Describe the threshold-gated single-version ledger in weights
  ([#181](https://github.com/ditto-assistant/ditto-subnet/pull/181),
  [`7131cc9`](https://github.com/ditto-assistant/ditto-subnet/commit/7131cc93fe9027168284b1936619e323878db202))

- **docs**: Compress FULL-STACK-UPDATES.md into a trust/transaction reference
  ([#182](https://github.com/ditto-assistant/ditto-subnet/pull/182),
  [`a79b5e2`](https://github.com/ditto-assistant/ditto-subnet/commit/a79b5e2f23d4a7488f03acca46d90afe857fc320))

### Features

- **validator**: Release 100% of miner emission (remove the 80% burn)
  ([#183](https://github.com/ditto-assistant/ditto-subnet/pull/183),
  [`f56ef83`](https://github.com/ditto-assistant/ditto-subnet/commit/f56ef838f1df47fd73edd3a0398d3767cf58b158))


## v0.17.0 (2026-07-19)

### Features

- Accept bench_version 4 and repin the scorer
  ([#180](https://github.com/ditto-assistant/ditto-subnet/pull/180),
  [`6d5eddc`](https://github.com/ditto-assistant/ditto-subnet/commit/6d5eddc91407e84b797e109839fcf3fccf9bd813))


## v0.16.3 (2026-07-19)

### Bug Fixes

- Fold platform-authoritative hybrid scores
  ([#178](https://github.com/ditto-assistant/ditto-subnet/pull/178),
  [`ac9b6d6`](https://github.com/ditto-assistant/ditto-subnet/commit/ac9b6d6d7251ffb3fa79d5237d1064679257725b))


## v0.16.2 (2026-07-19)

### Bug Fixes

- Repin dittobench runtime hotfix ([#177](https://github.com/ditto-assistant/ditto-subnet/pull/177),
  [`dd9fddc`](https://github.com/ditto-assistant/ditto-subnet/commit/dd9fddc2b6dd8dd8ae7b189d07ad435d0e3c1ba1))


## v0.16.1 (2026-07-19)

### Bug Fixes

- Report verified source scorer versions
  ([#176](https://github.com/ditto-assistant/ditto-subnet/pull/176),
  [`6a2f5b6`](https://github.com/ditto-assistant/ditto-subnet/commit/6a2f5b6ac74cf1bf233c8a72e4705d4978ba061e))


## v0.16.0 (2026-07-19)

### Features

- Enforce benchmark v3 screened-image contract
  ([#175](https://github.com/ditto-assistant/ditto-subnet/pull/175),
  [`2da1e4b`](https://github.com/ditto-assistant/ditto-subnet/commit/2da1e4b5fcf63a11b60b276228a8004c9c11d40e))


## v0.15.0 (2026-07-19)

### Features

- **validator**: Signed per-component stack health (heartbeat protocol 9)
  ([#174](https://github.com/ditto-assistant/ditto-subnet/pull/174),
  [`cde5fff`](https://github.com/ditto-assistant/ditto-subnet/commit/cde5fff591b0b39811362bed0a9f4e6f164a6fb5))


## v0.14.4 (2026-07-19)

### Bug Fixes

- **validator**: Fail closed on an unexpected benchmark version
  ([#171](https://github.com/ditto-assistant/ditto-subnet/pull/171),
  [`57b9359`](https://github.com/ditto-assistant/ditto-subnet/commit/57b9359956bf5ce967963d48bf6ce39f8d5a9f34))

### Chores

- **docs**: Cut operator guides back to setup essentials
  ([#170](https://github.com/ditto-assistant/ditto-subnet/pull/170),
  [`aeb79c1`](https://github.com/ditto-assistant/ditto-subnet/commit/aeb79c1b55261e9bf3711d266d65ef587a2b18e7))

- **tests**: Pin legacy confirmation regression
  ([#169](https://github.com/ditto-assistant/ditto-subnet/pull/169),
  [`8a7aee0`](https://github.com/ditto-assistant/ditto-subnet/commit/8a7aee052f6f9695d1f919509a3d9256c7137578))


## v0.14.3 (2026-07-19)

### Bug Fixes

- Keep validator rescoring lease-bound
  ([#168](https://github.com/ditto-assistant/ditto-subnet/pull/168),
  [`108e304`](https://github.com/ditto-assistant/ditto-subnet/commit/108e30423dd68775511e116cb4e883407ecbe47f))


## v0.14.2 (2026-07-19)

### Bug Fixes

- Pass protocol to native arm smoke
  ([#167](https://github.com/ditto-assistant/ditto-subnet/pull/167),
  [`08581ad`](https://github.com/ditto-assistant/ditto-subnet/commit/08581ad8b914876ba5cad747528504e233f20302))

### Chores

- Document safe stack updater migration
  ([#163](https://github.com/ditto-assistant/ditto-subnet/pull/163),
  [`0a902d9`](https://github.com/ditto-assistant/ditto-subnet/commit/0a902d9a84ddf431bb3f3b3b88a3f116c92a838c))


## v0.14.1 (2026-07-19)

### Bug Fixes

- Classify sandbox resource exhaustion as infrastructure
  ([#156](https://github.com/ditto-assistant/ditto-subnet/pull/156),
  [`e796c7c`](https://github.com/ditto-assistant/ditto-subnet/commit/e796c7c19e315a236f1eef616350513e360f1328))


## v0.14.0 (2026-07-19)

### Features

- Mirror v3 audit fields + fetch, sign, and publish run transcripts
  ([#155](https://github.com/ditto-assistant/ditto-subnet/pull/155),
  [`4c3c79e`](https://github.com/ditto-assistant/ditto-subnet/commit/4c3c79e68e7fdab85b1b7abb405ac1bf7ba4da67))


## v0.13.0 (2026-07-19)

### Features

- Negotiate DittoBench v3 scorer capability
  ([#160](https://github.com/ditto-assistant/ditto-subnet/pull/160),
  [`236ebab`](https://github.com/ditto-assistant/ditto-subnet/commit/236ebab889da0d6443dd1af2282e0e176d03bb1f))


## v0.12.0 (2026-07-19)

### Features

- Bench screened Docker images on validators
  ([#154](https://github.com/ditto-assistant/ditto-subnet/pull/154),
  [`6559112`](https://github.com/ditto-assistant/ditto-subnet/commit/655911283d97fb9e161215bbbf6a2c7f4058a5f3))


## v0.11.0 (2026-07-19)

### Features

- Bind managed scorer benchmark capabilities
  ([#159](https://github.com/ditto-assistant/ditto-subnet/pull/159),
  [`cd670af`](https://github.com/ditto-assistant/ditto-subnet/commit/cd670afef0044a8cec88a4bc24ca7de2287ff216))


## v0.10.3 (2026-07-19)

### Bug Fixes

- Harden updater and native release smoke
  ([#166](https://github.com/ditto-assistant/ditto-subnet/pull/166),
  [`9ea0090`](https://github.com/ditto-assistant/ditto-subnet/commit/9ea0090abe7a376f0ebe0180dbcd8acd0f5d1db0))


## v0.10.2 (2026-07-19)

### Bug Fixes

- Allow updater signature cache writes
  ([#164](https://github.com/ditto-assistant/ditto-subnet/pull/164),
  [`f2e7ef3`](https://github.com/ditto-assistant/ditto-subnet/commit/f2e7ef3546b2b1f12b4db1896c22a99c92e279a1))

### Chores

- **.github/workflows**: Migrate workflows to Blacksmith runners
  ([#165](https://github.com/ditto-assistant/ditto-subnet/pull/165),
  [`97bc902`](https://github.com/ditto-assistant/ditto-subnet/commit/97bc9029c214b62c1712dff60a028d0036a5254b))


## v0.10.1 (2026-07-19)

### Bug Fixes

- Validate stack descriptors from canonical staging
  ([#162](https://github.com/ditto-assistant/ditto-subnet/pull/162),
  [`83c635e`](https://github.com/ditto-assistant/ditto-subnet/commit/83c635e169ce769d6f65597c4e06304669f33860))


## v0.10.0 (2026-07-19)

### Features

- Update the complete validator stack from GHCR
  ([#158](https://github.com/ditto-assistant/ditto-subnet/pull/158),
  [`f9254d9`](https://github.com/ditto-assistant/ditto-subnet/commit/f9254d9dc24d0021f86c35123d99175e3ebbf66d))


## v0.9.6 (2026-07-16)

### Bug Fixes

- Restore updater digest aliases ([#153](https://github.com/ditto-assistant/ditto-subnet/pull/153),
  [`6c19207`](https://github.com/ditto-assistant/ditto-subnet/commit/6c19207c40927fa7d0648eea6445bd28d208245f))


## v0.9.5 (2026-07-16)

### Bug Fixes

- Harden validator updater host context
  ([#151](https://github.com/ditto-assistant/ditto-subnet/pull/151),
  [`e4df9b6`](https://github.com/ditto-assistant/ditto-subnet/commit/e4df9b6206d04236839f2b82f55a456a3ed2f5bb))


## v0.9.4 (2026-07-16)

### Bug Fixes

- Sign validator ledger requests ([#149](https://github.com/ditto-assistant/ditto-subnet/pull/149),
  [`97719eb`](https://github.com/ditto-assistant/ditto-subnet/commit/97719ebf4476e3a1abf1d9d9b52b0593b8c13fb0))


## v0.9.3 (2026-07-16)

### Bug Fixes

- Allow heartbeat protocol upgrades
  ([#150](https://github.com/ditto-assistant/ditto-subnet/pull/150),
  [`22af669`](https://github.com/ditto-assistant/ditto-subnet/commit/22af669c35ba04689648c97056bba3a0e1b6f0b6))

- Exclude deregistered miners from weight fold
  ([#146](https://github.com/ditto-assistant/ditto-subnet/pull/146),
  [`1bc4ce9`](https://github.com/ditto-assistant/ditto-subnet/commit/1bc4ce9466625badbd98387eaf053f9d88cf9e33))


## v0.9.2 (2026-07-16)

### Bug Fixes

- Sign validator artifact requests
  ([#148](https://github.com/ditto-assistant/ditto-subnet/pull/148),
  [`5550547`](https://github.com/ditto-assistant/ditto-subnet/commit/5550547f5c8e8073b765683d8aaa44bc107a23e6))

### Chores

- **docs**: Condense validator getting-started guide
  ([#141](https://github.com/ditto-assistant/ditto-subnet/pull/141),
  [`961fc4d`](https://github.com/ditto-assistant/ditto-subnet/commit/961fc4d3207ab74f42d87b2c98d1d9eeb2a40988))


## v0.9.1 (2026-07-15)

### Bug Fixes

- Align weight updates with chain cadence
  ([#145](https://github.com/ditto-assistant/ditto-subnet/pull/145),
  [`a3f71c6`](https://github.com/ditto-assistant/ditto-subnet/commit/a3f71c613a6d01d5eadba1fcc0293d12721a6996))

- Smoke-test each validator image architecture
  ([#143](https://github.com/ditto-assistant/ditto-subnet/pull/143),
  [`37424cc`](https://github.com/ditto-assistant/ditto-subnet/commit/37424ccd9687712b18f259fb428c8cdab5408209))


## v0.9.0 (2026-07-15)

### Features

- Reduce KOTH margin to 2% and make the dethrone band noise-aware
  ([#142](https://github.com/ditto-assistant/ditto-subnet/pull/142),
  [`f4be7ac`](https://github.com/ditto-assistant/ditto-subnet/commit/f4be7acecb213c08c3d1561d2b631d2aad835900))


## v0.8.0 (2026-07-15)

### Features

- Add safe validator auto-updates ([#130](https://github.com/ditto-assistant/ditto-subnet/pull/130),
  [`13235ff`](https://github.com/ditto-assistant/ditto-subnet/commit/13235fff55e813242d92e16360cfeea05dc5e0c7))

- Expose agent submission versions
  ([#140](https://github.com/ditto-assistant/ditto-subnet/pull/140),
  [`215a451`](https://github.com/ditto-assistant/ditto-subnet/commit/215a4514bb205109e2fdf04d40b5f8ee95eb0f53))


## v0.7.5 (2026-07-15)

### Bug Fixes

- Restore Pylon hotkey access and production burn targeting
  ([#139](https://github.com/ditto-assistant/ditto-subnet/pull/139),
  [`c43c0cb`](https://github.com/ditto-assistant/ditto-subnet/commit/c43c0cbc8d7c352feb023abbb93b5e0460ae0526))


## v0.7.4 (2026-07-15)

### Bug Fixes

- Prune sandbox Docker data ([#138](https://github.com/ditto-assistant/ditto-subnet/pull/138),
  [`b34c8f0`](https://github.com/ditto-assistant/ditto-subnet/commit/b34c8f03e97056163e703488c210647d1625e8af))


## v0.7.3 (2026-07-15)

### Bug Fixes

- Decouple weight updates from scoring sweeps
  ([#137](https://github.com/ditto-assistant/ditto-subnet/pull/137),
  [`30fd379`](https://github.com/ditto-assistant/ditto-subnet/commit/30fd379e4fe29d6842efe5e58201a97194578d55))

### Chores

- Remove private screener dependency authentication
  ([#136](https://github.com/ditto-assistant/ditto-subnet/pull/136),
  [`8a821f7`](https://github.com/ditto-assistant/ditto-subnet/commit/8a821f72d3d280d35022383996d43a9c1577ecaa))


## v0.7.2 (2026-07-15)

### Bug Fixes

- Use curl for sandbox embedding healthcheck
  ([#133](https://github.com/ditto-assistant/ditto-subnet/pull/133),
  [`eab7ca8`](https://github.com/ditto-assistant/ditto-subnet/commit/eab7ca85238cccfb1bd40e6594a4d8726b6b0661))

- **release**: Authenticate private dependency build
  ([#135](https://github.com/ditto-assistant/ditto-subnet/pull/135),
  [`c937ae4`](https://github.com/ditto-assistant/ditto-subnet/commit/c937ae43e95df6b8a2d1c029b543611a88797770))

### Chores

- **ci**: Authenticate private dependency install
  ([#134](https://github.com/ditto-assistant/ditto-subnet/pull/134),
  [`dd00bf8`](https://github.com/ditto-assistant/ditto-subnet/commit/dd00bf89254790860fc0ee3d168fe684d1b625a9))


## v0.7.1 (2026-07-15)

### Bug Fixes

- Preflight validator embedding route before leasing
  ([#132](https://github.com/ditto-assistant/ditto-subnet/pull/132),
  [`aae4bae`](https://github.com/ditto-assistant/ditto-subnet/commit/aae4baee5c432de1c27b48ce283b144097272d8c))


## v0.7.0 (2026-07-14)

### Features

- Show screening reasons in miner status
  ([#131](https://github.com/ditto-assistant/ditto-subnet/pull/131),
  [`0b59d46`](https://github.com/ditto-assistant/ditto-subnet/commit/0b59d46d7ca2ffdb2c84edf5336fd79b17042361))


## v0.6.6 (2026-07-14)

### Bug Fixes

- Report durable weight telemetry status
  ([#129](https://github.com/ditto-assistant/ditto-subnet/pull/129),
  [`dc54030`](https://github.com/ditto-assistant/ditto-subnet/commit/dc540308613ed038f03862829a23e01829e2026d))


## v0.6.5 (2026-07-14)

### Bug Fixes

- Keep weights running on job poll failure
  ([#128](https://github.com/ditto-assistant/ditto-subnet/pull/128),
  [`a218f80`](https://github.com/ditto-assistant/ditto-subnet/commit/a218f80c9fc7bd8938d294690b2e466556b018d0))

### Chores

- Define cheating for miners ([#127](https://github.com/ditto-assistant/ditto-subnet/pull/127),
  [`43c7dbd`](https://github.com/ditto-assistant/ditto-subnet/commit/43c7dbd58c5b06c7737fed2154b2137178cdaced))


## v0.6.4 (2026-07-14)

### Bug Fixes

- Support external validator Compose builds
  ([#126](https://github.com/ditto-assistant/ditto-subnet/pull/126),
  [`2c83860`](https://github.com/ditto-assistant/ditto-subnet/commit/2c8386013d257ebb52e4897c42b713f7c9129885))


## v0.6.3 (2026-07-14)

### Bug Fixes

- Report isolated validator container health
  ([#124](https://github.com/ditto-assistant/ditto-subnet/pull/124),
  [`6597f56`](https://github.com/ditto-assistant/ditto-subnet/commit/6597f56170fbbf9db3d7249b3209dcc1a81556bf))


## v0.6.2 (2026-07-14)

### Bug Fixes

- Extend validator benchmark timeout to 75 minutes
  ([#125](https://github.com/ditto-assistant/ditto-subnet/pull/125),
  [`22121f7`](https://github.com/ditto-assistant/ditto-subnet/commit/22121f776741b6aaab4d9f189d9daef9520c5f93))


## v0.6.1 (2026-07-14)

### Bug Fixes

- Point miner CLI at production API
  ([#123](https://github.com/ditto-assistant/ditto-subnet/pull/123),
  [`402545b`](https://github.com/ditto-assistant/ditto-subnet/commit/402545ba583d0057a733271f5641c9adb9be622a))


## v0.6.0 (2026-07-14)

### Chores

- Remove extracted screener runtime
  ([#120](https://github.com/ditto-assistant/ditto-subnet/pull/120),
  [`ab66cc2`](https://github.com/ditto-assistant/ditto-subnet/commit/ab66cc22d7383eb2d1379bc39ed5e9bc396c1557))

### Features

- Report privacy-safe benchmark progress
  ([#121](https://github.com/ditto-assistant/ditto-subnet/pull/121),
  [`d783d80`](https://github.com/ditto-assistant/ditto-subnet/commit/d783d800e1a35f69684a0685cf963461cc104e0a))


## v0.5.0 (2026-07-14)

### Features

- Report privacy-safe fleet system health
  ([#119](https://github.com/ditto-assistant/ditto-subnet/pull/119),
  [`ec80571`](https://github.com/ditto-assistant/ditto-subnet/commit/ec8057163c5b1f2ad87717234c124f2383ec1e52))


## v0.4.3 (2026-07-14)

### Bug Fixes

- Cancel timed-out validator benchmarks
  ([#122](https://github.com/ditto-assistant/ditto-subnet/pull/122),
  [`b837419`](https://github.com/ditto-assistant/ditto-subnet/commit/b8374198006a36ec19d171cab0ff4a649d458ee5))


## v0.4.2 (2026-07-14)

### Bug Fixes

- Require screening policy handshake
  ([#116](https://github.com/ditto-assistant/ditto-subnet/pull/116),
  [`2ee244b`](https://github.com/ditto-assistant/ditto-subnet/commit/2ee244ba6edce0880907ab27983cdf75a4c8970b))


## v0.4.1 (2026-07-14)

### Bug Fixes

- Temporarily disable model canary
  ([#115](https://github.com/ditto-assistant/ditto-subnet/pull/115),
  [`83acde6`](https://github.com/ditto-assistant/ditto-subnet/commit/83acde695cf13475aeb235a0d84086216cd71568))


## v0.4.0 (2026-07-14)

### Features

- Report active screening and scoring work
  ([#114](https://github.com/ditto-assistant/ditto-subnet/pull/114),
  [`c22bc80`](https://github.com/ditto-assistant/ditto-subnet/commit/c22bc8024c3fb2d3589a19828e4ce36b1b849990))


## v0.3.0 (2026-07-14)

### Features

- Define leased screening attempts
  ([#113](https://github.com/ditto-assistant/ditto-subnet/pull/113),
  [`92045cb`](https://github.com/ditto-assistant/ditto-subnet/commit/92045cbc7afb88d3ce29a6f6c1ef09196094c780))


## v0.2.2 (2026-07-14)

### Bug Fixes

- Probe screener harness inside isolated network
  ([#112](https://github.com/ditto-assistant/ditto-subnet/pull/112),
  [`054063b`](https://github.com/ditto-assistant/ditto-subnet/commit/054063b35f4eb618a30d7ff14a15036693013831))


## v0.2.1 (2026-07-14)

### Bug Fixes

- Repair canary networking and bump screening policy
  ([#111](https://github.com/ditto-assistant/ditto-subnet/pull/111),
  [`48c54de`](https://github.com/ditto-assistant/ditto-subnet/commit/48c54de7934c5b52396ec9cff14b2b520d0bdf1a))


## v0.2.0 (2026-07-14)

### Bug Fixes

- Configure release git identity ([#108](https://github.com/ditto-assistant/ditto-subnet/pull/108),
  [`4db33f0`](https://github.com/ditto-assistant/ditto-subnet/commit/4db33f0f81b3aec044de058201f3072ed374f814))

- Fetch release history before bootstrapping
  ([#106](https://github.com/ditto-assistant/ditto-subnet/pull/106),
  [`3890f08`](https://github.com/ditto-assistant/ditto-subnet/commit/3890f084f306cea198d0b61b36d87310094d428d))

### Features

- Automate semantic releases ([#105](https://github.com/ditto-assistant/ditto-subnet/pull/105),
  [`8fb0424`](https://github.com/ditto-assistant/ditto-subnet/commit/8fb042466d6bfc98af8d0d210fc9faa0d1f51df9))


## v0.1.0 (2026-07-14)

- Initial Release

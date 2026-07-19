# CHANGELOG

<!-- version list -->

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

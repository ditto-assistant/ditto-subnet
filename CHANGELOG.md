# CHANGELOG

<!-- version list -->

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

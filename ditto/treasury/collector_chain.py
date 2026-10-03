"""Pinned public Substrate adapter and confined delegate signer.

Supports only register_limit and same-SN118 transfer_stake. It never receives
the primary coldkey, signs nested utility calls, swaps alpha, or buys credits.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import fields
from importlib.metadata import version
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ditto.treasury.collector import (
    CollectorPolicy,
    FinalizedEarnings,
    Observation,
    Settlement,
    SignedOperation,
)
from ditto.treasury.service_allocation import ServiceDestination
from ditto_screening_protocol.collector_receipts import (
    AUDITED_COLLECTOR_CODE_HASH,
    FINNEY_GENESIS,
    collector_gross_incentive,
    collector_transfer_effect,
    liquid_collector_credit,
)

# Exact live v472 bytes bound to reconstructed source, with only the documented
# compile-time hash-seed constants differing in the independent srtool rebuild.
# See docs/service-collector-automation.md for artifact/source fingerprints.
AUDITED_CODE_HASH = AUDITED_COLLECTOR_CODE_HASH


class NoCredentialRedirect(HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        raise RuntimeError("credential endpoint redirect refused")


def credential_request(request, *, timeout):
    # Environment proxies must not redirect IMDS tokens or authorization.
    return build_opener(ProxyHandler({}), NoCredentialRedirect()).open(
        request, timeout=timeout
    )


def unwrap(value):
    return getattr(value, "value", value)


def uint(value):
    value = unwrap(value)
    if type(value) is not int or not 0 <= value < 2**64:
        raise ValueError("invalid chain integer")
    return value


def load_policy(path: Path, expected_digest: str) -> CollectorPolicy:
    """Offline primary signature plus deployment-pinned immutable digest.

    Backroom shadow settings are not spend authority. Unknown envelope fields
    are ignored, but only these known fields enter the canonical signature.
    """
    from bittensor_wallet import Keypair

    envelope = json.loads(path.read_text())
    raw = envelope["policy"]
    known = {f.name for f in fields(CollectorPolicy)}
    body = {k: v for k, v in raw.items() if k in known}
    body["destinations"] = tuple(
        ServiceDestination(
            **{k: d[k] for k in ("bucket_id", "allocation_bps", "holding_coldkey")}
        )
        for d in body["destinations"]
    )
    policy = CollectorPolicy(**body)
    if policy.digest != expected_digest:
        raise ValueError("policy differs from immutable deployment pin")
    for address in (
        policy.collector_coldkey,
        policy.collector_hotkey,
        policy.registration_delegate,
        policy.transfer_delegate,
        *(d.holding_coldkey for d in policy.destinations),
    ):
        Keypair(ss58_address=address)
    public = Keypair(ss58_address=policy.collector_coldkey)
    signature = bytes.fromhex(envelope["signature"].removeprefix("0x"))
    if not public.verify(
        f"ditto-collector-policy-v1:{policy.digest}".encode(), signature
    ):
        raise ValueError("policy lacks offline collector approval")
    return policy


class PublicCollectorChain:
    def __init__(self, substrate, *, role: str):
        if version("bittensor") != "10.5.0":
            raise ValueError("collector requires audited pinned SDK 10.5.0")
        if role not in {"registration", "transfer"}:
            raise ValueError("invalid delegate role")
        self.substrate = substrate
        self.role = role

    def query(self, module, name, params, block_hash):
        return unwrap(
            self.substrate.query(module, name, params=params, block_hash=block_hash)
        )

    def guard_runtime(self, policy, block_hash):
        s = self.substrate
        if (policy.genesis_hash, policy.runtime_code_hash) != (
            FINNEY_GENESIS,
            AUDITED_CODE_HASH,
        ):
            raise ValueError("runtime policy lacks independent byte/source audit")
        if s.get_block_hash(0) != policy.genesis_hash:
            raise ValueError("wrong chain genesis")
        code_hash = s.rpc_request(
            "state_getStorageHash", ["0x3a636f6465", block_hash]
        ).get("result")
        if code_hash != policy.runtime_code_hash:
            raise ValueError("runtime changed; stop for independent contract audit")
        # SDK 10.5.0's singular helper does not match the audited plural API.
        filters = unwrap(
            s.runtime_call(
                "ProxyFilterRuntimeApi",
                "get_proxy_filters",
                [None],
                block_hash=block_hash,
            )
        )
        expected = {
            "Registration": {
                ("SubtensorModule", "register"),
                ("SubtensorModule", "register_limit"),
                ("SubtensorModule", "burned_register"),
            },
            "Transfer": {
                ("Balances", "transfer_keep_alive"),
                ("Balances", "transfer_allow_death"),
                ("Balances", "transfer_all"),
                ("SubtensorModule", "transfer_stake"),
                ("SubtensorModule", "transfer_stake_and_hotkey"),
            },
        }
        for name, allowed in expected.items():
            entries = [e for e in filters if e.get("name") == name]
            if len(entries) != 1 or entries[0].get("deprecated") is not False:
                raise ValueError("unsupported proxy filter")
            mode = entries[0].get("filter_mode")
            calls = mode.get("Allow") if isinstance(mode, dict) else None
            if not isinstance(calls, list) or any(
                c.get("constraint") is not None for c in calls
            ):
                raise ValueError("unsupported proxy constraints")
            if {
                (c.get("pallet_name"), c.get("call_name")) for c in calls
            } != allowed or len(calls) != len(allowed):
                raise ValueError("proxy scope differs from audited allowlist")

    def identity(self, policy, block_hash, *, allow_unowned=False):
        owner = self.query(
            "SubtensorModule", "Owner", [policy.collector_hotkey], block_hash
        )
        subnet_owner = self.query("SubtensorModule", "SubnetOwner", [118], block_hash)
        if subnet_owner == policy.collector_coldkey:
            raise ValueError(
                "collector ownership changed or is subnet-owner associated"
            )
        uid = self.query(
            "SubtensorModule", "Uids", [118, policy.collector_hotkey], block_hash
        )
        if owner != policy.collector_coldkey:
            # The first register_limit creates Owner. Its ValueQuery default is
            # an account address, not None; only raw storage absence proves this
            # hotkey is new. This exception is registration-only and cannot
            # authorize earnings or a transfer, or accept an existing owner/UID.
            if not allow_unowned or self.role != "registration" or uid is not None:
                raise ValueError("collector ownership changed")
            key = self.substrate.create_storage_key(
                "SubtensorModule",
                "Owner",
                [policy.collector_hotkey],
                block_hash=block_hash,
            )
            result = self.substrate.rpc_request(
                "state_getStorageAt", [key.to_hex(), block_hash]
            )
            if (
                result.get("error")
                or "result" not in result
                or result["result"] is not None
            ):
                raise ValueError("collector ownership present or unavailable")
            return None
        if uid is not None:
            uid = uint(uid)
            if (
                self.query("SubtensorModule", "Keys", [118, uid], block_hash)
                != policy.collector_hotkey
            ):
                raise ValueError("reused or inconsistent UID binding")
        return uid

    def alpha(self, policy, coldkey, block_hash):
        value = unwrap(
            self.substrate.runtime_call(
                "StakeInfoRuntimeApi",
                "get_stake_info_for_hotkey_coldkey_netuid",
                [policy.collector_hotkey, coldkey, 118],
                block_hash=block_hash,
            )
        )
        if value is None:
            return 0
        if (value.get("hotkey"), value.get("coldkey"), value.get("netuid")) != (
            policy.collector_hotkey,
            coldkey,
            118,
        ):
            raise ValueError("stake API identity mismatch")
        stake = uint(value["stake"])
        collateral = self.query(
            "SubtensorModule",
            "MinerCollateral",
            [118, policy.collector_hotkey, coldkey],
            block_hash,
        )
        locked = uint(collateral["locked"]) if collateral is not None else 0
        if locked > stake:
            raise ValueError("invalid collateral position")
        # The audited runtime omits zero-stake/zero-lock entries from aggregate
        # availability. A brand-new collector has no position to spend; do not
        # require a nonexistent map entry before its first registration.
        if not stake:
            return 0
        availability = unwrap(
            self.substrate.runtime_call(
                "StakeInfoRuntimeApi",
                "get_stake_availability_for_coldkeys",
                [[coldkey], [118]],
                block_hash=block_hash,
            )
        )
        bounds = availability[coldkey][118]
        total, account_locked, available = (
            uint(bounds[k]) for k in ("total", "locked", "available")
        )
        if account_locked > total or available > total - account_locked:
            raise ValueError("invalid aggregate lock accounting")
        return min(stake - locked, available)

    def observe(self, policy, role):
        if role != self.role:
            raise ValueError("wrong signer role")
        s = self.substrate
        block_hash = s.get_chain_finalised_head()
        block = s.get_block_number(block_hash)
        self.guard_runtime(policy, block_hash)
        uid = self.identity(policy, block_hash, allow_unowned=role == "registration")
        delegate = (
            policy.registration_delegate
            if role == "registration"
            else policy.transfer_delegate
        )
        proxies = self.query("Proxy", "Proxies", [policy.collector_coldkey], block_hash)
        expected_type = "Registration" if role == "registration" else "Transfer"
        if not isinstance(proxies, (tuple, list)) or len(proxies) != 2:
            raise ValueError("unavailable proxy registration")
        grants = [p for p in proxies[0] if p.get("delegate") == delegate]
        all_grants = {
            (p.get("delegate"), p.get("proxy_type"), p.get("delay")) for p in proxies[0]
        }
        if len(proxies[0]) != 2 or all_grants != {
            (policy.registration_delegate, "Registration", 0),
            (policy.transfer_delegate, "Transfer", 0),
        }:
            raise ValueError("collector has unreviewed or broad proxy grants")
        if (
            len(grants) != 1
            or grants[0].get("proxy_type") != expected_type
            or grants[0].get("delay") != 0
        ):
            raise ValueError(
                "delegate must have exactly one narrow immediate proxy grant"
            )
        self.assert_no_sponsor(policy, delegate, block_hash)

        def free(address):
            account = self.query("System", "Account", [address], block_hash)
            return uint(account["data"]["free"])

        return Observation(
            block,
            block_hash,
            uid,
            uint(self.query("SubtensorModule", "Burn", [118], block_hash)),
            free(policy.collector_coldkey),
            free(delegate),
            self.alpha(policy, policy.collector_coldkey, block_hash),
        )

    def earnings(self, policy, block):
        s = self.substrate
        if block > s.get_block_number(s.get_chain_finalised_head()):
            raise ValueError("emission block is not finalized")
        block_hash = s.get_block_hash(block)
        self.guard_runtime(policy, block_hash)
        uid = self.identity(policy, block_hash)
        parent_uid = self.identity(policy, s.get_block_hash(block - 1))
        if uid is None or uid != parent_uid:
            # Registration/rebind within payout block makes attribution ambiguous.
            if any(
                e.get("event_id") == "IncentiveAlphaEmittedToMiners"
                for e in s.get_events(block_hash)
            ):
                raise ValueError("emission intersects collector identity transition")
            return None
        gross = collector_gross_incentive(s.get_events(block_hash), uid)
        if gross is None:
            return None
        # Gross SERVER_EMISSION precedes collateral capture and routing. Only
        # this exact liquid initialization credit authorizes distribution.
        for at in (block_hash, s.get_block_hash(block - 1)):
            if (
                self.query(
                    "SubtensorModule",
                    "AutoStakeDestination",
                    [policy.collector_coldkey, 118],
                    at,
                )
                != policy.collector_hotkey
            ):
                raise ValueError("liquid emission route is not pinned to collector")
        credit = liquid_collector_credit(
            s.get_events(block_hash),
            collector_hotkey=policy.collector_hotkey,
            collector_coldkey=policy.collector_coldkey,
            gross_incentive_rao=gross,
        )
        return (
            FinalizedEarnings(credit.amount_rao, block_hash, credit.event_digest)
            if credit is not None
            else None
        )

    def assert_no_sponsor(self, policy, delegate, block_hash):
        # SCALE Option<()> can decode BOTH absence and presence as None. Only
        # raw storage absence proves the collector is not sponsoring fees.
        key = self.substrate.create_storage_key(
            "Proxy",
            "RealPaysFeeConsentV1",
            [policy.collector_coldkey, delegate],
            block_hash=block_hash,
        )
        result = self.substrate.rpc_request(
            "state_getStorageAt", [key.to_hex(), block_hash]
        )
        if (
            result.get("error")
            or "result" not in result
            or result["result"] is not None
        ):
            raise ValueError("fee sponsorship present or unavailable")

    def key(self, policy):
        from bittensor_wallet import Keypair

        host = f"sn118-collector-{self.role}-signer"
        request = Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/name",
            headers={"Metadata-Flavor": "Google"},
        )
        with credential_request(request, timeout=3) as response:
            if response.read(128).decode() != host:
                raise ValueError("delegate confined to dedicated signer host")
        principal = getattr(policy, f"{self.role}_service_account")
        secret_version = getattr(policy, f"{self.role}_secret_version")
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", policy.gcp_project):
            raise ValueError("invalid GCP project")

        def metadata(path):
            request = Request(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/"
                + path,
                headers={"Metadata-Flavor": "Google"},
            )
            with credential_request(request, timeout=3) as response:
                return response.read(4096)

        if metadata("email").decode().strip() != principal:
            raise ValueError("unexpected signer service identity")
        try:
            token = json.loads(metadata("token"))["access_token"]
            request = Request(
                f"https://secretmanager.googleapis.com/v1/projects/{policy.gcp_project}/secrets/sn118-collector-{self.role}-delegate/versions/{secret_version}:access",
                headers={"Authorization": f"Bearer {token}"},
            )
            with credential_request(request, timeout=15) as response:
                payload = json.loads(response.read(8192))["payload"]
            mnemonic = base64.b64decode(payload["data"], validate=True).decode().strip()
        except Exception:
            # Never include response bodies, tokens, mnemonic or exception output.
            raise RuntimeError("delegate secret retrieval failed") from None
        if len(mnemonic.split()) != 24:
            raise ValueError("delegate requires 24-word mnemonic")
        try:
            key = Keypair.create_from_mnemonic(mnemonic)
        except Exception:
            raise RuntimeError("delegate key payload is invalid") from None
        expected = (
            policy.registration_delegate
            if self.role == "registration"
            else policy.transfer_delegate
        )
        if key.ss58_address != expected:
            raise ValueError("wrong delegate key; primary coldkey never accepted")
        return key

    def prepare(self, policy, role, call, observation):
        if role != self.role:
            raise ValueError("role mismatch")
        expected_function = (
            "register_limit" if role == "registration" else "transfer_stake"
        )
        if call["module"] != "SubtensorModule" or call["function"] != expected_function:
            raise ValueError("arbitrary/nested calls forbidden")
        params = call["params"]
        if role == "registration":
            if params != {
                "netuid": 118,
                "hotkey": policy.collector_hotkey,
                "limit_price": policy.max_registration_burn_rao,
            }:
                raise ValueError("registration intent mismatch")
        elif (
            set(params)
            != {
                "destination_coldkey",
                "hotkey",
                "origin_netuid",
                "destination_netuid",
                "alpha_amount",
            }
            or params["destination_coldkey"]
            not in {d.holding_coldkey for d in policy.destinations}
            or params["hotkey"] != policy.collector_hotkey
            or params["origin_netuid"] != 118
            or params["destination_netuid"] != 118
            or type(params["alpha_amount"]) is not int
            or not 0 < params["alpha_amount"] <= policy.max_distribution_rao
        ):
            raise ValueError("distribution intent mismatch")
        s = self.substrate
        current = self.observe(policy, role)
        if (
            current.block != observation.block
            or current.block_hash != observation.block_hash
        ):
            raise ValueError("finalized observation changed before signing")
        key = self.key(policy)
        self.guard_runtime(policy, s.get_chain_head())
        inner = s.compose_call(
            call["module"], call["function"], params, block_hash=current.block_hash
        )
        proxy = s.compose_call(
            "Proxy",
            "proxy",
            {
                "real": policy.collector_coldkey,
                "force_proxy_type": "Registration"
                if role == "registration"
                else "Transfer",
                "call": inner,
            },
            block_hash=current.block_hash,
        )
        era = {"period": 64, "current": current.block}
        nonce = s.get_account_nonce(key.ss58_address)
        fee = uint(s.get_payment_info(proxy, key, era=era, nonce=nonce)["partialFee"])
        if fee > policy.max_fee_rao:
            raise ValueError("estimated fee exceeds cap")
        extrinsic = s.create_signed_extrinsic(proxy, key, era=era, nonce=nonce, tip=0)
        self.guard_runtime(policy, s.get_chain_head())
        self.authorized_policy = policy
        encoded = extrinsic.data.to_hex()
        digest = (
            "0x"
            + hashlib.blake2b(bytes.fromhex(encoded[2:]), digest_size=32).hexdigest()
        )
        # Mortality birth may round backward, so this conservative latest expiry
        # never declares expiry early. Scan to current+64 inclusive.
        return SignedOperation(encoded, digest, current.block, current.block + 64, fee)

    def broadcast(self, encoded):
        policy = getattr(self, "authorized_policy", None)
        if policy is None:
            raise ValueError("broadcast requires this process's prepared policy")
        self.guard_runtime(policy, self.substrate.get_chain_head())
        result = self.substrate.rpc_request("author_submitExtrinsic", [encoded])
        if result.get("error"):
            raise RuntimeError(
                "submission uncertain; persisted hash requires reconciliation"
            )

    def reconcile(self, policy, operation, observation):
        signed = json.loads(operation["signed_json"])
        s = self.substrate
        start = max(signed["start_block"], operation["reconciled_through"])
        end = min(observation.block, signed["expires_block"], start + 32)
        for block in range(start + 1, end + 1):
            block_hash = s.get_block_hash(block)
            self.guard_runtime(policy, block_hash)
            raw = s.rpc_request("chain_getBlock", [block_hash])["result"]["block"][
                "extrinsics"
            ]
            matches = [
                i
                for i, encoded in enumerate(raw)
                if "0x"
                + hashlib.blake2b(
                    bytes.fromhex(encoded[2:]), digest_size=32
                ).hexdigest()
                == signed["extrinsic_hash"]
            ]
            if not matches:
                continue
            if len(matches) != 1:
                raise ValueError("duplicate extrinsic hash")
            events = [
                e
                for e in s.get_events(block_hash)
                if e.get("extrinsic_idx") == matches[0]
            ]
            if any(e.get("phase") != "ApplyExtrinsic" for e in events):
                raise ValueError("extrinsic event phase differs from dispatch")
            inner = [
                e
                for e in events
                if e.get("module_id") == "Proxy"
                and e.get("event_id") == "ProxyExecuted"
            ]
            outer = [
                e
                for e in events
                if e.get("module_id") == "System"
                and e.get("event_id") == "ExtrinsicSuccess"
            ]
            self.fee_evidence(policy, operation["role"], events)
            if not outer:
                failures = [
                    e
                    for e in events
                    if e.get("module_id") == "System"
                    and e.get("event_id") == "ExtrinsicFailed"
                ]
                if len(failures) != 1:
                    raise ValueError("unknown outer outcome; retain durable claim")
                return Settlement("failed", block, block_hash)
            if len(outer) != 1 or len(inner) != 1:
                raise ValueError("missing or ambiguous inner proxy result")
            attrs = inner[0].get("event", {}).get("attributes")
            if not isinstance(attrs, dict) or set(attrs) != {"result"}:
                raise ValueError("unsupported proxy result schema")
            if attrs["result"] not in ({"Ok": None}, {"Ok": []}):
                error = attrs["result"]
                if (
                    isinstance(error, dict)
                    and set(error) == {"Err"}
                    and isinstance(error["Err"], (dict, str))
                    and error["Err"]
                ):
                    return Settlement("failed", block, block_hash)
                raise ValueError("unsupported inner result; retain durable claim")
            uid = self.identity(policy, block_hash)
            if operation["role"] == "registration":
                if (
                    uid is None
                    or self.identity(
                        policy, s.get_block_hash(block - 1), allow_unowned=True
                    )
                    is not None
                ):
                    raise ValueError("registration effect/binding unproved")
                registrations = [
                    e
                    for e in events
                    if e.get("module_id") == "SubtensorModule"
                    and e.get("event_id") == "NeuronRegistered"
                ]
                if len(registrations) != 1 or registrations[0].get("event", {}).get(
                    "attributes"
                ) != [118, uid, policy.collector_hotkey]:
                    raise ValueError("missing exact registration effect event")
                return Settlement("finalized", block, block_hash, uid)
            destination = operation["destination"]
            # The same audited decoder is used by the independent ingestion
            # reader. This function still verifies runtime, fee, identity and
            # signed extrinsic binding before accepting its decoded effects.
            collector_transfer_effect(
                events,
                extrinsic_index=matches[0],
                collector_coldkey=policy.collector_coldkey,
                collector_hotkey=policy.collector_hotkey,
                recipient_coldkey=destination,
                amount_rao=operation["amount"],
            )
            if uid is None:
                raise ValueError("finalized UID binding unavailable")
            self.alpha(policy, destination, block_hash)
            return Settlement(
                "finalized",
                block,
                block_hash,
                uid,
                extrinsic_index=matches[0],
                extrinsic_hash=signed["extrinsic_hash"],
            )
        status = (
            "expired"
            if observation.block > signed["expires_block"]
            and end == signed["expires_block"]
            else "pending"
        )
        return Settlement(status, scanned_through=end)

    @staticmethod
    def fee_evidence(policy, role, events):
        fees = [
            e
            for e in events
            if e.get("module_id") == "TransactionPayment"
            and e.get("event_id") == "TransactionFeePaid"
        ]
        delegate = (
            policy.registration_delegate
            if role == "registration"
            else policy.transfer_delegate
        )
        if len(fees) != 1:
            raise ValueError("missing exact fee evidence")
        fee = fees[0].get("event", {}).get("attributes")
        if (
            not isinstance(fee, dict)
            or set(fee) != {"who", "actual_fee", "tip"}
            or fee["who"] != delegate
            or uint(fee["tip"]) != 0
            or uint(fee["actual_fee"]) > policy.max_fee_rao
        ):
            raise ValueError("actual fee/payer exceeds authorized accounting")

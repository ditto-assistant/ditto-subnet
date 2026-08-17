from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import patch

from screener_capacity.targon import TargonAPIError, TargonClient


class _Response:
    def __init__(self, value: Any = None, *, text: str | None = None) -> None:
        self.body = text.encode() if text is not None else json.dumps(value).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class TargonClientTests(unittest.TestCase):
    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_inventory_uses_public_v3_endpoint_without_authentication(
        self, urlopen: object
    ) -> None:
        urlopen.return_value = _Response(  # type: ignore[attr-defined]
            [{"name": "cpu-small", "available": 2}]
        )

        rows = TargonClient(api_key=None).inventory()

        self.assertEqual(rows, [{"name": "cpu-small", "available": 2}])
        request = urlopen.call_args.args[0]  # type: ignore[attr-defined]
        self.assertIsInstance(request, urllib.request.Request)
        self.assertEqual(
            request.full_url,
            "https://api.targon.com/tha/v3/inventory?type=rental&gpu=false",
        )
        self.assertIsNone(request.get_header("Authorization"))

    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_list_workloads_uses_org_scope_and_follows_cursors(
        self, urlopen: object
    ) -> None:
        urlopen.side_effect = [  # type: ignore[attr-defined]
            _Response(
                {
                    "items": [{"uid": "rental-1"}],
                    "next_cursor": "rental-1",
                }
            ),
            _Response({"items": [{"uid": "rental-2"}], "next_cursor": None}),
        ]
        client = TargonClient(api_key="x" * 40, org_slug="ditto")

        rows = client.list_workloads(name="ditto worker")

        self.assertEqual(rows, [{"uid": "rental-1"}, {"uid": "rental-2"}])
        requests = [
            call.args[0]
            for call in urlopen.call_args_list  # type: ignore[attr-defined]
        ]
        self.assertEqual(
            requests[0].full_url,
            "https://api.targon.com/tha/v3/orgs/ditto/workloads"
            "?limit=1000&name=ditto+worker",
        )
        self.assertEqual(
            requests[1].full_url,
            "https://api.targon.com/tha/v3/orgs/ditto/workloads"
            "?limit=1000&name=ditto+worker&cursor=rental-1",
        )
        self.assertTrue(
            all(
                request.get_header("Authorization") == "Bearer " + "x" * 40
                for request in requests
            )
        )

    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_rental_lifecycle_stays_inside_selected_organization(
        self, urlopen: object
    ) -> None:
        urlopen.side_effect = [  # type: ignore[attr-defined]
            _Response({"uid": "rental-1"}),
            _Response({"uid": "rental-1", "state": {"status": "provisioning"}}),
            _Response({"uid": "rental-1"}),
            _Response({"uid": "rental-1", "status": "running"}),
            _Response(text="command output"),
            _Response(text="log output"),
            _Response({"uid": "rental-1", "state": {"status": "suspended"}}),
            _Response(None),
        ]
        client = TargonClient(api_key="x" * 40, org_slug="ditto")

        client.create_rental(
            name="ditto-test",
            image="busybox:1.37",
            resource_name="cpu-small",
            experiments={"persistent-workload": False},
        )
        client.deploy("rental-1")
        client.update("rental-1", envs=[{"name": "PUBLIC", "value": "yes"}])
        client.state("rental-1")
        self.assertEqual(client.exec("rental-1", ["echo", "ok"]), "command output")
        self.assertEqual(client.logs("rental-1", tail=12), "log output")
        client.suspend("rental-1")
        client.delete("rental-1")

        requests = [
            call.args[0]
            for call in urlopen.call_args_list  # type: ignore[attr-defined]
        ]
        self.assertEqual(
            [request.get_method() for request in requests],
            ["POST", "POST", "PATCH", "GET", "POST", "GET", "POST", "DELETE"],
        )
        create_payload = json.loads(requests[0].data)
        self.assertEqual(create_payload["experiments"], {"persistent-workload": False})
        update_payload = json.loads(requests[2].data)
        self.assertEqual(update_payload, {"envs": [{"name": "PUBLIC", "value": "yes"}]})
        base = "https://api.targon.com/tha/v3/orgs/ditto/workloads"
        self.assertEqual(
            [request.full_url for request in requests],
            [
                base,
                base + "/rental-1/deploy",
                base + "/rental-1",
                base + "/rental-1/state",
                base + "/rental-1/exec?command=echo&command=ok",
                base + "/rental-1/logs?tail=12",
                base + "/rental-1/suspend",
                base + "/rental-1",
            ],
        )

    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_update_can_replace_image_and_command(self, urlopen: object) -> None:
        urlopen.return_value = _Response({"uid": "rental-1"})  # type: ignore[attr-defined]
        client = TargonClient(api_key="x" * 40, org_slug="ditto")

        client.update(
            "rental-1",
            image="busybox:1.37.0",
            commands=["sleep", "3600"],
            args=[],
            envs=[],
        )

        request = urlopen.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(
            request.full_url,
            "https://api.targon.com/tha/v3/orgs/ditto/workloads/rental-1",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "image": "busybox:1.37.0",
                "commands": ["sleep", "3600"],
                "args": [],
                "envs": [],
            },
        )

    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_vm_and_ephemeral_ssh_key_stay_inside_selected_organization(
        self, urlopen: object
    ) -> None:
        urlopen.side_effect = [  # type: ignore[attr-defined]
            _Response({"uid": "key-1"}),
            _Response({"uid": "vm-1"}),
            _Response(None),
        ]
        client = TargonClient(api_key="x" * 40, org_slug="ditto")

        client.create_ssh_key(name="probe-key", public_key="ssh-ed25519 public")
        client.create_vm(
            name="probe-vm",
            image="ubuntu-24-04-lts-595",
            resource_name="rtx6000b-small",
            ssh_key_uids=["key-1"],
            password="one-time-password",
        )
        client.delete_ssh_key("key-1")

        requests = [
            call.args[0]
            for call in urlopen.call_args_list  # type: ignore[attr-defined]
        ]
        self.assertEqual(
            [request.full_url for request in requests],
            [
                "https://api.targon.com/tha/v3/orgs/ditto/ssh-keys",
                "https://api.targon.com/tha/v3/orgs/ditto/workloads",
                "https://api.targon.com/tha/v3/orgs/ditto/ssh-keys/key-1",
            ],
        )
        vm_payload = json.loads(requests[1].data)
        self.assertEqual(
            vm_payload,
            {
                "type": "VM",
                "name": "probe-vm",
                "image": "ubuntu-24-04-lts-595",
                "resource_name": "rtx6000b-small",
                "ssh_keys": ["key-1"],
                "vm_config": {"password": "one-time-password"},
            },
        )

    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_authenticated_workload_call_requires_org_slug(
        self, urlopen: object
    ) -> None:
        client = TargonClient(api_key="x" * 40)

        with self.assertRaisesRegex(TargonAPIError, "organization slug unavailable"):
            client.list_workloads()

        urlopen.assert_not_called()  # type: ignore[attr-defined]

    def test_invalid_org_slug_is_rejected_before_requests(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid Targon organization slug"):
            TargonClient(api_key="x" * 40, org_slug="Not / Safe")

    @patch("screener_capacity.targon.time.sleep", return_value=None)
    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_transient_error_retries_without_echoing_body(
        self, urlopen: object, _sleep: object
    ) -> None:
        error = urllib.error.HTTPError(
            "https://api.targon.com/tha/v3/orgs/ditto/workloads",
            429,
            "rate limit",
            {},
            io.BytesIO(b'{"reason":"leaked-secret-value"}'),
        )
        urlopen.side_effect = [error, error, error]  # type: ignore[attr-defined]
        client = TargonClient(api_key="x" * 40, org_slug="ditto")

        with self.assertRaises(TargonAPIError) as raised:
            client.list_workloads()

        self.assertEqual(urlopen.call_count, 3)  # type: ignore[attr-defined]
        self.assertNotIn("leaked-secret-value", str(raised.exception))
        self.assertIn("rate limited", str(raised.exception))

    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_delete_uses_long_timeout_and_does_not_retry_teardown(
        self, urlopen: object
    ) -> None:
        urlopen.return_value = _Response(None)  # type: ignore[attr-defined]
        client = TargonClient(api_key="x" * 40, org_slug="ditto")

        client.delete("rental-1")

        self.assertEqual(urlopen.call_count, 1)  # type: ignore[attr-defined]
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 180.0)  # type: ignore[attr-defined]

    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_delete_reconciles_timeout_when_state_is_already_deleted(
        self, urlopen: object
    ) -> None:
        urlopen.side_effect = [  # type: ignore[attr-defined]
            TimeoutError(),
            _Response({"status": "deleted"}),
        ]
        client = TargonClient(api_key="x" * 40, org_slug="ditto")

        client.delete("rental-1")

        self.assertEqual(urlopen.call_count, 2)  # type: ignore[attr-defined]

    @patch("screener_capacity.targon.time.sleep", return_value=None)
    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_deploy_retries_transient_provider_failure(
        self, urlopen: object, _sleep: object
    ) -> None:
        error = urllib.error.HTTPError(
            "https://api.targon.com/tha/v3/orgs/ditto/workloads/rental-1/deploy",
            503,
            "unavailable",
            {},
            io.BytesIO(b"provider body is never copied"),
        )
        urlopen.side_effect = [error, _Response({"uid": "rental-1"})]  # type: ignore[attr-defined]
        client = TargonClient(api_key="x" * 40, org_slug="ditto")

        client.deploy("rental-1")

        self.assertEqual(urlopen.call_count, 2)  # type: ignore[attr-defined]

    @patch("screener_capacity.targon.time.sleep", return_value=None)
    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_deploy_reconciles_lost_response_from_provider(
        self, urlopen: object, _sleep: object
    ) -> None:
        error = urllib.error.HTTPError(
            "https://api.targon.com/tha/v3/orgs/ditto/workloads/rental-1/deploy",
            503,
            "unavailable",
            {},
            io.BytesIO(b"provider body is never copied"),
        )
        urlopen.side_effect = [  # type: ignore[attr-defined]
            error,
            error,
            error,
            _Response({"status": "provisioning"}),
        ]
        client = TargonClient(api_key="x" * 40, org_slug="ditto")

        state = client.deploy("rental-1")

        self.assertEqual(state, {"status": "provisioning"})
        self.assertEqual(urlopen.call_count, 4)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()

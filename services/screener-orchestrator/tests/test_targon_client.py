from __future__ import annotations

import io
import unittest
import urllib.error
from unittest.mock import patch

from screener_capacity.targon import TargonAPIError, TargonClient


class TargonClientTests(unittest.TestCase):
    @patch("screener_capacity.targon.time.sleep", return_value=None)
    @patch("screener_capacity.targon.urllib.request.urlopen")
    def test_transient_error_retries_without_echoing_body(
        self, urlopen: object, _sleep: object
    ) -> None:
        error = urllib.error.HTTPError(
            "https://api.targon.com/tha/v2/workloads",
            429,
            "rate limit",
            {},
            io.BytesIO(b'{"reason":"leaked-secret-value"}'),
        )
        urlopen.side_effect = [error, error, error]  # type: ignore[attr-defined]
        client = TargonClient(api_key="x" * 40)
        with self.assertRaises(TargonAPIError) as raised:
            client.list_workloads()
        self.assertEqual(urlopen.call_count, 3)  # type: ignore[attr-defined]
        self.assertNotIn("leaked-secret-value", str(raised.exception))
        self.assertIn("rate limited", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

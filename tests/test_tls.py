import io
from pathlib import Path
import ssl
import sys
import unittest
from unittest import mock
from urllib.error import URLError

SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "paper-search"
    / "skills"
    / "paper-search"
)
sys.path.insert(0, str(SKILL_DIR))

import paper_search


ARXIV_RESPONSE = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1503.06862v2</id>
    <title>Multi-terminal Josephson junctions as topological materials</title>
  </entry>
</feed>
"""


class TlsFallbackTests(unittest.TestCase):
    def test_arxiv_verification_retries_with_next_ca_context(self):
        contexts = [object(), object()]
        certificate_error = URLError(
            ssl.SSLCertVerificationError("unable to get local issuer certificate")
        )

        with mock.patch.object(paper_search, "_https_contexts", return_value=contexts):
            with mock.patch.object(
                paper_search.urllib.request,
                "urlopen",
                side_effect=[certificate_error, io.BytesIO(ARXIV_RESPONSE)],
            ) as urlopen:
                titles = paper_search._fetch_arxiv_titles(["1503.06862"])

        self.assertEqual(
            titles["1503.06862"],
            "Multi-terminal Josephson junctions as topological materials",
        )
        self.assertEqual(urlopen.call_count, 2)
        self.assertIs(urlopen.call_args_list[0].kwargs["context"], contexts[0])
        self.assertIs(urlopen.call_args_list[1].kwargs["context"], contexts[1])

    def test_tls_fallback_raises_last_failure(self):
        contexts = [object(), object()]
        failures = [URLError("first"), URLError("second")]

        with mock.patch.object(paper_search, "_https_contexts", return_value=contexts):
            with mock.patch.object(
                paper_search.urllib.request,
                "urlopen",
                side_effect=failures,
            ):
                with self.assertRaisesRegex(URLError, "second"):
                    paper_search._urlopen_with_tls_fallback("https://example.com", 1)


if __name__ == "__main__":
    unittest.main()

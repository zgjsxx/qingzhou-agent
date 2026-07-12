import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import registry
from tools.web import (
    _jina_reader_url,
    web_extract_impl,
    web_search_impl,
)


class WebToolsTest(unittest.TestCase):
    def test_web_search_formats_ddgs_results(self):
        hits = [
            {
                "title": "Qingzhou Agent",
                "url": "https://example.com/qingzhou",
                "snippet": "A personal agent.",
                "position": 1,
            }
        ]
        with patch("tools.web._search_ddgs", return_value=(hits, None)):
            result = web_search_impl("qingzhou agent", limit=5)

        self.assertIn("Web search results for: qingzhou agent", result)
        self.assertIn("1. Qingzhou Agent", result)
        self.assertIn("URL: https://example.com/qingzhou", result)
        self.assertIn("Snippet: A personal agent.", result)

    def test_web_search_rejects_unsupported_backend(self):
        result = web_search_impl("qingzhou agent", backend="tavily")

        self.assertIn("unsupported web_search backend", result)

    def test_web_extract_uses_jina_first_in_auto_mode(self):
        with patch("tools.web._request_text", return_value=("Title: Example\nContent", None)) as request:
            result = web_extract_impl("https://example.com/page", backend="auto")

        self.assertIn("## https://example.com/page", result)
        self.assertIn("Title: Example", result)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], _jina_reader_url("https://example.com/page"))

    def test_web_extract_falls_back_to_raw_http(self):
        def fake_request(url, _timeout):
            if url.startswith("https://r.jina.ai/"):
                return None, "jina failed"
            return "<html><body><h1>Hello</h1><script>bad()</script><p>World</p></body></html>", None

        with patch("tools.web._request_text", side_effect=fake_request):
            result = web_extract_impl("https://example.com/page", backend="auto")

        self.assertIn("Hello", result)
        self.assertIn("World", result)
        self.assertNotIn("bad()", result)

    def test_web_extract_blocks_local_urls(self):
        result = web_extract_impl("http://127.0.0.1:2024", backend="raw")

        self.assertIn("private, local, and link-local hosts are not allowed", result)

    def test_web_tools_are_registered(self):
        tool_names = {tool.name for tool in registry.ALL_TOOLS}

        self.assertIn("web_search", tool_names)
        self.assertIn("web_extract", tool_names)


if __name__ == "__main__":
    unittest.main()

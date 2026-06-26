import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agent_rag


class AgentRagTest(unittest.TestCase):
    def test_empty_query_returns_without_loading_index(self):
        with patch("agent_rag._load_index") as load_index:
            result = agent_rag.rag_search("   ")

        self.assertEqual(result, "RAG query cannot be empty.")
        load_index.assert_not_called()

    def test_truncate_chunk_records_omitted_chars_and_digest(self):
        text = "a" * 40 + "b" * 40

        with patch.dict("os.environ", {"RAG_MAX_CHARS_PER_CHUNK": "20"}):
            result = agent_rag._truncate_chunk(text)

        self.assertIn("omitted 60 chars", result)
        self.assertIn("sha256=", result)
        self.assertTrue(result.startswith("aaaaaaaaaa"))
        self.assertTrue(result.endswith("bbbbbbbbbb"))


if __name__ == "__main__":
    unittest.main()

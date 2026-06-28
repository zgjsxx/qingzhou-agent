import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import agent_rag


class AgentRagTest(unittest.TestCase):
    def test_openai_embedding_accepts_auth_token(self):
        embeddings = Mock()
        modules = {"OpenAIEmbeddings": embeddings}

        with patch.dict(
            "os.environ",
            {
                "RAG_EMBEDDING_PROVIDER": "openai",
                "RAG_EMBEDDING_MODEL": "embedding-model",
                "RAG_EMBEDDING_AUTH_TOKEN": "auth-token",
                "RAG_EMBEDDING_API_KEY": "api-key",
                "RAG_EMBEDDING_BASE_URL": "https://example.test/v1",
            },
            clear=True,
        ):
            agent_rag._create_embedding_model(modules)

        embeddings.assert_called_once_with(
            model="embedding-model",
            api_key="auth-token",
            base_url="https://example.test/v1",
        )

    def test_dashscope_embedding_accepts_auth_token(self):
        with patch.dict(
            "os.environ",
            {
                "RAG_EMBEDDING_PROVIDER": "dashscope",
                "RAG_EMBEDDING_MODEL": "text-embedding-v4",
                "RAG_EMBEDDING_AUTH_TOKEN": "auth-token",
                "RAG_EMBEDDING_API_KEY": "api-key",
            },
            clear=True,
        ):
            embedding = agent_rag._create_embedding_model({})

        self.assertIsInstance(embedding, agent_rag.DashScopeEmbeddingWrapper)
        self.assertEqual(embedding.model, "text-embedding-v4")
        self.assertEqual(embedding._api_key, "auth-token")

    def test_dashscope_does_not_reuse_anthropic_llm_auth_token(self):
        with patch.dict(
            "os.environ",
            {
                "RAG_EMBEDDING_PROVIDER": "dashscope",
                "RAG_EMBEDDING_API_KEY": "dashscope-api-key",
                "LLM_AUTH_TOKEN": "anthropic-gateway-token",
            },
            clear=True,
        ):
            embedding = agent_rag._create_embedding_model({})

        self.assertEqual(embedding._api_key, "dashscope-api-key")

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

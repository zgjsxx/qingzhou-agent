"""Local RAG support built on LlamaIndex.

The tool layer deliberately returns retrieved evidence instead of generating a
second model answer inside the tool. The main agent can then answer with normal
conversation context and citations from the returned chunks.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Any

from agent_tasks import BACKEND_DIR

DEFAULT_DOCS_DIR = BACKEND_DIR / "data" / "rag_docs"
DEFAULT_STORAGE_DIR = BACKEND_DIR / "data" / "rag_storage"
DEFAULT_TOP_K = 5
MAX_TOP_K = 20
MAX_CHUNK_CHARS = 1800
RAG_LOCK = threading.Lock()


class DashScopeEmbeddingWrapper:
    """Small LangChain Embeddings-compatible wrapper for DashScope embeddings."""

    def __init__(self, model: str, api_key: str):
        self.model = model
        self._api_key = api_key

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            import dashscope
            from dashscope import TextEmbedding
        except ImportError as exc:
            raise RuntimeError("DashScope embeddings require: pip install dashscope") from exc

        dashscope.api_key = self._api_key
        result = TextEmbedding.call(model=self.model, input=texts)
        if result.status_code != 200:
            raise RuntimeError(
                "DashScope embedding failed: "
                f"code={getattr(result, 'code', 'N/A')}, "
                f"message={getattr(result, 'message', 'N/A')}"
            )
        return [item["embedding"] for item in result.output["embeddings"]]


def _normalize_text(value: str) -> str:
    return value.lower().strip()


def _tokenize_text(text: str) -> list[str]:
    import jieba

    normalized = _normalize_text(text)
    ascii_tokens = re.findall(r"[a-zA-Z0-9_]+", normalized)
    chinese_parts = re.findall(r"[\u4e00-\u9fff]+", normalized)

    tokens: list[str] = []
    tokens.extend(token for token in ascii_tokens if token)
    for part in chinese_parts:
        tokens.extend(token for token in jieba.cut_for_search(part) if token.strip())
    return tokens


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _resolve_path(value: str, default: Path) -> Path:
    raw = Path(value).expanduser() if value else default
    resolved = raw if raw.is_absolute() else BACKEND_DIR / raw
    return resolved.absolute()


def _resolve_docs_dir(data_dir: str = "") -> Path:
    return _resolve_path(data_dir or os.getenv("RAG_DOCS_DIR", ""), DEFAULT_DOCS_DIR)


def _resolve_storage_dir() -> Path:
    return _resolve_path(os.getenv("RAG_STORAGE_DIR", ""), DEFAULT_STORAGE_DIR)


def _import_llama_index() -> dict[str, Any]:
    try:
        from langchain_community.embeddings import MiniMaxEmbeddings
        from langchain_openai import OpenAIEmbeddings
        from llama_index.core import (
            Settings,
            SimpleDirectoryReader,
            StorageContext,
            VectorStoreIndex,
            load_index_from_storage,
        )
        from llama_index.core.node_parser import SentenceSplitter
        from llama_index.embeddings.langchain import LangchainEmbedding
    except ImportError as exc:
        raise RuntimeError(
            "RAG dependencies are missing. Install backend requirements, including "
            "llama-index, llama-index-embeddings-langchain, langchain-community, "
            "rank-bm25, jieba, and pypdf."
        ) from exc

    return {
        "LangchainEmbedding": LangchainEmbedding,
        "MiniMaxEmbeddings": MiniMaxEmbeddings,
        "OpenAIEmbeddings": OpenAIEmbeddings,
        "Settings": Settings,
        "SimpleDirectoryReader": SimpleDirectoryReader,
        "StorageContext": StorageContext,
        "VectorStoreIndex": VectorStoreIndex,
        "load_index_from_storage": load_index_from_storage,
        "SentenceSplitter": SentenceSplitter,
    }


def _create_embedding_model(modules: dict[str, Any]):
    provider = _env_first("RAG_EMBEDDING_PROVIDER", "EMBEDDING_PROVIDER", default="openai").lower()
    model = _env_first("RAG_EMBEDDING_MODEL", "EMBEDDING_MODEL")

    if provider == "openai":
        api_key = _env_first("RAG_EMBEDDING_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY")
        base_url = _env_first("RAG_EMBEDDING_BASE_URL", "OPENAI_BASE_URL", "LLM_BASE_URL")
        if not api_key:
            raise RuntimeError("RAG_EMBEDDING_API_KEY or OPENAI_API_KEY is required for RAG embeddings.")
        kwargs: dict[str, Any] = {
            "model": model or "text-embedding-3-small",
            "api_key": api_key,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return modules["OpenAIEmbeddings"](**kwargs)

    if provider == "dashscope":
        api_key = _env_first("RAG_EMBEDDING_API_KEY", "DASHSCOPE_API_KEY", "PROVIDER_DASHSCOPE_API_KEY")
        if not api_key:
            raise RuntimeError("RAG_EMBEDDING_API_KEY or DASHSCOPE_API_KEY is required for DashScope RAG embeddings.")
        return DashScopeEmbeddingWrapper(model=model or "text-embedding-v4", api_key=api_key)

    if provider == "minimax":
        api_key = _env_first("RAG_EMBEDDING_API_KEY", "MINIMAX_API_KEY", "PROVIDER_MINIMAX_API_KEY")
        group_id = _env_first("RAG_MINIMAX_GROUP_ID", "MINIMAX_GROUP_ID", "PROVIDER_MINIMAX_GROUP_ID")
        if not api_key or not group_id:
            raise RuntimeError("MINIMAX_API_KEY and MINIMAX_GROUP_ID are required for MiniMax RAG embeddings.")
        return modules["MiniMaxEmbeddings"](minimax_api_key=api_key, minimax_group_id=group_id)

    raise RuntimeError("Unsupported RAG_EMBEDDING_PROVIDER. Supported values: openai, dashscope, minimax.")


def _configure_settings(modules: dict[str, Any]) -> None:
    Settings = modules["Settings"]
    SentenceSplitter = modules["SentenceSplitter"]
    LangchainEmbedding = modules["LangchainEmbedding"]

    Settings.embed_model = LangchainEmbedding(_create_embedding_model(modules))
    Settings.llm = None
    Settings.node_parser = SentenceSplitter(
        chunk_size=int(os.getenv("RAG_CHUNK_SIZE", "512")),
        chunk_overlap=int(os.getenv("RAG_CHUNK_OVERLAP", "64")),
    )


def _build_index(docs_dir: Path, storage_dir: Path) -> tuple[Any, int]:
    modules = _import_llama_index()
    _configure_settings(modules)

    if not docs_dir.exists():
        raise RuntimeError(f"RAG docs directory does not exist: {docs_dir}")

    documents = modules["SimpleDirectoryReader"](
        input_dir=str(docs_dir),
        recursive=True,
        required_exts=[".md", ".txt", ".pdf", ".docx"],
    ).load_data()
    if not documents:
        raise RuntimeError(f"No supported documents found in: {docs_dir}")

    index = modules["VectorStoreIndex"].from_documents(documents)
    storage_dir.mkdir(parents=True, exist_ok=True)
    index.storage_context.persist(persist_dir=str(storage_dir))
    return index, len(documents)


def _load_index() -> Any:
    modules = _import_llama_index()
    _configure_settings(modules)

    storage_dir = _resolve_storage_dir()
    if not storage_dir.exists() or not any(storage_dir.iterdir()):
        raise RuntimeError(
            f"RAG index does not exist: {storage_dir}. "
            "Run rag_rebuild_index first after placing documents in the RAG docs directory."
        )

    storage_context = modules["StorageContext"].from_defaults(persist_dir=str(storage_dir))
    return modules["load_index_from_storage"](storage_context)


def _unwrap_node(node: Any) -> Any:
    return getattr(node, "node", node)


def _safe_node_id(node: Any, fallback_index: int) -> str:
    raw_node = _unwrap_node(node)
    return getattr(raw_node, "node_id", None) or f"node_{fallback_index}"


def _all_index_nodes(index: Any) -> list[Any]:
    docstore = getattr(index, "docstore", None)
    docs = getattr(docstore, "docs", {}) if docstore is not None else {}
    return [node for node in docs.values() if hasattr(node, "get_content")]


def _file_name_from_metadata(metadata: dict[str, Any]) -> str:
    file_path = metadata.get("file_path") or metadata.get("filename") or metadata.get("file_name") or ""
    return Path(str(file_path)).name if file_path else ""


def _source_from_metadata(metadata: dict[str, Any]) -> str:
    file_path = metadata.get("file_path") or metadata.get("filename") or metadata.get("file_name") or ""
    page = metadata.get("page_label") or metadata.get("page_number") or metadata.get("page")
    label = str(file_path or "unknown source")
    if page:
        label = f"{label}#page={page}"
    return label


def _extract_high_confidence_tokens(query: str) -> list[str]:
    normalized = _normalize_text(query)
    tokens = re.findall(r"[a-zA-Z0-9_]+", normalized)
    unique_tokens: list[str] = []
    for token in tokens:
        if len(token) >= 3 and ("_" in token or token.endswith("define") or token.endswith("info")):
            if token not in unique_tokens:
                unique_tokens.append(token)
    return unique_tokens


def _rule_retrieve(index: Any, query: str, top_k: int) -> list[Any]:
    tokens = _extract_high_confidence_tokens(query)
    if not tokens:
        return []

    matched_nodes: list[tuple[Any, int]] = []
    for node in _all_index_nodes(index):
        content = node.get_content()
        text = _normalize_text(content)
        metadata = getattr(node, "metadata", {}) or {}
        source_name = _normalize_text(_file_name_from_metadata(metadata))

        score = 0
        for token in tokens:
            if token in text:
                score += 10
            if token in source_name:
                score += 20
        if score > 0:
            if "## " in content or "# " in content:
                score += 3
            matched_nodes.append((node, score))

    matched_nodes.sort(key=lambda item: item[1], reverse=True)
    return [node for node, _ in matched_nodes[: max(1, top_k)]]


def _bm25_retrieve(index: Any, query: str, top_k: int) -> list[Any]:
    from rank_bm25 import BM25Okapi

    nodes = _all_index_nodes(index)
    if not nodes:
        return []

    query_tokens = _tokenize_text(query)
    if not query_tokens:
        return []

    corpus_tokens = [_tokenize_text(node.get_content()) for node in nodes]
    bm25 = BM25Okapi(corpus_tokens)
    scores = bm25.get_scores(query_tokens)
    ranked_pairs = sorted(zip(nodes, scores), key=lambda item: item[1], reverse=True)
    return [node for node, score in ranked_pairs if score > 0][: max(1, top_k)]


def _vector_retrieve(index: Any, query: str, top_k: int) -> list[Any]:
    retriever = index.as_retriever(similarity_top_k=max(1, top_k))
    return list(retriever.retrieve(query))


def _ranked_retrieval_results(index: Any, query: str, top_k: int) -> list[Any]:
    fusion_pool_size = max(5, top_k * 3)
    rule_nodes = _rule_retrieve(index, query, fusion_pool_size)
    vector_nodes = _vector_retrieve(index, query, fusion_pool_size)
    bm25_nodes = _bm25_retrieve(index, query, fusion_pool_size)

    fused_scores: dict[str, float] = {}
    fused_nodes: dict[str, Any] = {}

    for rank, node in enumerate(rule_nodes, start=1):
        node_id = _safe_node_id(node, rank)
        fused_nodes[node_id] = _unwrap_node(node)
        fused_scores[node_id] = fused_scores.get(node_id, 0.0) + (5.0 / rank)

    for source_nodes in (vector_nodes, bm25_nodes):
        for rank, node in enumerate(source_nodes, start=1):
            node_id = _safe_node_id(node, rank)
            fused_nodes[node_id] = _unwrap_node(node)
            fused_scores[node_id] = fused_scores.get(node_id, 0.0) + (1.0 / (60 + rank))

    ranked_ids = sorted(fused_scores, key=lambda node_id: fused_scores[node_id], reverse=True)
    return [fused_nodes[node_id] for node_id in ranked_ids[: max(1, top_k)]]


def _clamp_top_k(top_k: int) -> int:
    return max(1, min(int(top_k or DEFAULT_TOP_K), int(os.getenv("RAG_TOP_K_MAX", str(MAX_TOP_K)))))


def _truncate_chunk(text: str) -> str:
    limit = int(os.getenv("RAG_MAX_CHARS_PER_CHUNK", str(MAX_CHUNK_CHARS)))
    if len(text) <= limit:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[omitted {omitted} chars, sha256={digest}]...\n{text[-tail:]}"


def rag_rebuild_index(data_dir: str = "") -> str:
    """Rebuild the local RAG index from documents."""
    try:
        docs_dir = _resolve_docs_dir(data_dir)
        storage_dir = _resolve_storage_dir()
        with RAG_LOCK:
            _, doc_count = _build_index(docs_dir, storage_dir)
        return (
            "RAG index rebuilt successfully.\n"
            f"documents: {doc_count}\n"
            f"docs_dir: {docs_dir}\n"
            f"storage_dir: {storage_dir}"
        )
    except Exception as exc:
        return f"RAG index rebuild failed: {exc}"


def rag_search(query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """Search the local RAG index and return ranked evidence chunks."""
    if not query.strip():
        return "RAG query cannot be empty."

    try:
        with RAG_LOCK:
            index = _load_index()
            ranked_nodes = _ranked_retrieval_results(index, query, top_k=_clamp_top_k(top_k))
    except Exception as exc:
        return f"RAG search failed: {exc}"

    if not ranked_nodes:
        return f"No relevant RAG documents found for query: {query}"

    lines = [
        "RAG search results. Use only these retrieved passages when answering unless you explicitly say you are using general knowledge.",
        f"query: {query}",
        f"storage_dir: {_resolve_storage_dir()}",
        "",
    ]
    for index_num, node in enumerate(ranked_nodes, start=1):
        metadata = getattr(node, "metadata", {}) or {}
        source = _source_from_metadata(metadata)
        content = _truncate_chunk(node.get_content().strip())
        lines.append(f"[{index_num}] source: {source}")
        lines.append(content)
        lines.append("")

    return "\n".join(lines).strip()

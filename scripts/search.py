#!/usr/bin/env python3
"""
search.py – Query Azure AI Search + Claude API for Intelligent Answers

Combines Azure AI Search (retrieval) with Claude API (generation) in a
classic RAG pattern:

1. User asks a question
2. Azure AI Search finds Top-8 relevant documents
3. Claude analyzes the retrieved context and generates an answer
4. Sources and relevance scores are displayed

Usage:
    python3 scripts/search.py "your question here"
    python3 scripts/search.py "welche EU AI Act Artikel muss ich abdecken?"
    python3 scripts/search.py "what components does the architecture have?"

Token Cost: ~0.01-0.05$ per query (Claude API only, Azure Search is free tier)
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.core.exceptions import AzureError

load_dotenv()

# ── Config ────────────────────────────────────────────────
SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
SEARCH_KEY = os.getenv("AZURE_SEARCH_ADMIN_KEY") or os.getenv("AZURE_SEARCH_KEY")
INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX_NAME") or os.getenv("AZURE_SEARCH_INDEX", "ai-context-vault")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
DEFAULT_SOURCE_REPO = Path(__file__).resolve().parents[1].name
SOURCE_REPO_FILTER = os.getenv("SEARCH_SOURCE_REPO") or os.getenv("SUMMARY_SOURCE_REPO") or DEFAULT_SOURCE_REPO
TOP_K = 8
MAX_TOKENS = 2000
MODEL = "claude-sonnet-4-20250514"


def _first_non_empty(result, *fields: str, default: str = "") -> str:
    for field in fields:
        value = result.get(field)
        if value:
            return value
    return default


def _filter_expression() -> str | None:
    if not SOURCE_REPO_FILTER:
        return None
    escaped = SOURCE_REPO_FILTER.replace("'", "''")
    return f"source_repo eq '{escaped}'"


def search_azure(query: str) -> tuple[list, str]:
    """Search Azure AI Search for relevant documents."""
    client = SearchClient(
        SEARCH_ENDPOINT, INDEX_NAME, AzureKeyCredential(SEARCH_KEY)
    )
    filter_expr = _filter_expression()
    warning = ""

    try:
        results = client.search(
            search_text=query,
            top=TOP_K,
            include_total_count=True,
            filter=filter_expr,
        )
    except AzureError as exc:
        if not filter_expr:
            raise RuntimeError(f"Azure Search query failed: {exc}") from exc
        warning = f"Search filter disabled after Azure error: {exc}"
        results = client.search(
            search_text=query,
            top=TOP_K,
            include_total_count=True,
        )

    docs = []
    for r in results:
        source_repo = r.get("source_repo") or SOURCE_REPO_FILTER or ""
        docs.append(
            {
                "path": _first_non_empty(r, "path", "source_path", "blob_name", "title", default="unknown"),
                "title": _first_non_empty(r, "title", "path", "source_path", "blob_name", default="Untitled"),
                "doc_type": _first_non_empty(r, "doc_type", "topic", default="session_summary"),
                "content": r.get("content", "")[:3000],
                "score": r.get("@search.score", 0),
                "source_repo": source_repo,
            }
        )
    return docs, warning


def ask_claude(query: str, docs: list) -> str:
    """Send query + retrieved context to Claude for analysis."""
    if not ANTHROPIC_KEY:
        return "❌ ANTHROPIC_API_KEY not set in .env"
    if not docs:
        return "No matching documents found in Azure Search."
    try:
        import anthropic
    except Exception as exc:
        return f"❌ Anthropic client import failed: {exc}"

    context_parts = []
    for i, doc in enumerate(docs):
        repo_suffix = f", repo: {doc['source_repo']}" if doc.get("source_repo") else ""
        context_parts.append(
            f"[{i+1}] {doc['path']} (type: {doc['doc_type']}{repo_suffix}, score: {doc['score']:.2f})\n"
            f"{doc['content'][:2000]}"
        )
    context = "\n\n---\n\n".join(context_parts)

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Du bist ein Research-Assistent. Beantworte die folgende Frage "
                    f"NUR basierend auf dem bereitgestellten Kontext. "
                    f"Nutze Quellenverweise wie [1], [2] etc.\n\n"
                    f"Frage: {query}\n\n"
                    f"Kontext:\n{context}"
                ),
            }
        ],
    )
    return message.content[0].text


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/search.py \"your question\"")
        sys.exit(1)

    query = " ".join(sys.argv[1:])

    if not SEARCH_ENDPOINT or not SEARCH_KEY:
        print("❌ Azure Search credentials missing in .env")
        sys.exit(1)

    print(f"\n🔍 Suche: '{query}'")
    print("=" * 60)

    # Step 1: Retrieve from Azure
    try:
        docs, warning = search_azure(query)
    except Exception as exc:
        print(f"❌ Azure Search query failed: {exc}")
        sys.exit(2)

    if warning:
        print(f"⚠️  {warning}")
    print(f"📄 {len(docs)} relevante Dokumente gefunden:")
    for doc in docs:
        repo_suffix = f" | repo: {doc['source_repo']}" if doc.get("source_repo") else ""
        print(f"   • [{doc['doc_type']}] {doc['title']} (Score: {doc['score']:.2f}{repo_suffix})")

    # Step 2: Generate answer with Claude
    print("\n🤖 Claude analysiert...\n")
    answer = ask_claude(query, docs)
    print("=" * 60)
    print(answer)
    print("=" * 60)


if __name__ == "__main__":
    main()

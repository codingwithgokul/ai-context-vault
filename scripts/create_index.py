#!/usr/bin/env python3
"""
create_index.py – Create Azure AI Search Index Schema

Creates a search index with fields optimized for AI context retrieval:
- Full-text search on content, title, tags
- Filterable metadata (doc_type, chapter)
- EU AI Act reference tracking

Usage:
    python3 scripts/create_index.py

Run once to initialize the index. Safe to re-run (updates existing).
"""

import os
import sys
from dotenv import load_dotenv
from azure.core.credentials import AzureKeyCredential
from azure.search.documents.indexes import SearchIndexClient
from azure.core.exceptions import ResourceNotFoundError
from azure.search.documents.indexes.models import SearchIndex, SimpleField, SearchableField, SearchFieldDataType

load_dotenv()

ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
KEY = os.getenv("AZURE_SEARCH_ADMIN_KEY") or os.getenv("AZURE_SEARCH_KEY")
INDEX = os.getenv("AZURE_SEARCH_INDEX_NAME") or os.getenv("AZURE_SEARCH_INDEX", "ai-context-vault")


def create_index():
    """Create or update the Azure AI Search index."""
    if not ENDPOINT or not KEY:
        print("❌ AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_KEY required in .env")
        sys.exit(1)

    client = SearchIndexClient(ENDPOINT, AzureKeyCredential(KEY))

    desired_fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="blob_name", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="doc_type", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="chapter", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="status", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="topic", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="created_at", type=SearchFieldDataType.String, filterable=True, sortable=True),
        SimpleField(name="repo_scope", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="summary_type", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="source_repo", type=SearchFieldDataType.String, filterable=True, facetable=True),
        SimpleField(name="source_path", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="path", type=SearchFieldDataType.String),
        SearchableField(name="title", type=SearchFieldDataType.String),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchableField(name="tags", type=SearchFieldDataType.String),
        SearchableField(name="eu_ai_act_refs", type=SearchFieldDataType.String),
    ]

    try:
        existing = client.get_index(INDEX)
        existing_fields = list(existing.fields)
    except ResourceNotFoundError:
        existing = None
        existing_fields = []

    by_name = {field.name: field for field in existing_fields}
    for field in desired_fields:
        by_name.setdefault(field.name, field)

    index = SearchIndex(name=INDEX, fields=list(by_name.values()))
    if existing and getattr(existing, "semantic_search", None):
        index.semantic_search = existing.semantic_search
    if existing and getattr(existing, "scoring_profiles", None):
        index.scoring_profiles = existing.scoring_profiles
    if existing and getattr(existing, "cors_options", None):
        index.cors_options = existing.cors_options

    result = client.create_or_update_index(index)
    print(f"✅ Index '{result.name}' erstellt/aktualisiert!")
    print(f"   Felder: {len(result.fields)}")
    print(f"   URL: {ENDPOINT}/indexes/{INDEX}")


if __name__ == "__main__":
    create_index()

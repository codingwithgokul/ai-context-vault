#!/usr/bin/env python3
"""Local workflow smoke test: save -> reindex -> local retrieval."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from workflow_lib import (
    REPO_ROOT,
    build_index,
    build_resume_text,
    save_session_summary,
    write_index,
    write_resume_text,
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _refresh_local_context() -> None:
    index = build_index()
    write_index(index)
    write_resume_text(build_resume_text(index))


def main() -> int:
    marker = f"smoke-{uuid.uuid4().hex[:8]}"
    summary_path: Path | None = None
    return_code = 0

    try:
        text = (
            f"Decision: keep workflow stable for marker {marker}.\n"
            f"Next step: verify retrieval for marker {marker}.\n"
            f"Result: smoke test must find marker {marker} in indexed summary."
        )
        summary_path, payload = save_session_summary(
            text=text,
            topic="general",
            title=f"workflow-smoke-{marker}",
            source="ci-smoke",
            tags=["smoke", "ci"],
            use_llm=False,
        )

        _assert(summary_path.exists(), "summary file was not created")
        _refresh_local_context()

        index = build_index()
        rel_path = str(summary_path.relative_to(REPO_ROOT))
        matching = [s for s in index.get("session_summaries", []) if s.get("path") == rel_path]
        _assert(bool(matching), f"saved summary missing from index: {rel_path}")

        doc = matching[0]
        for field in ("id", "title", "summary_bullets", "repo_scope", "summary_type", "source_repo"):
            _assert(field in doc and doc[field], f"missing required summary field: {field}")

        doc_text = json.dumps(doc, ensure_ascii=True).lower()
        _assert(marker in doc_text, "marker not found in indexed summary")

        # Local retrieval check without cloud dependencies.
        local_hits = []
        for item in index.get("session_summaries", []):
            item_text = json.dumps(item, ensure_ascii=True).lower()
            if marker in item_text:
                local_hits.append(item)
        _assert(any(hit.get("path") == rel_path for hit in local_hits), "local retrieval did not find saved summary")

        _assert(payload.get("summary_engine") == "local_rules", "smoke summary must use local_rules engine")
        print(f"SMOKE OK: save/reindex/local-retrieval passed ({marker})")

    except Exception as exc:
        print(f"SMOKE FAIL: {exc}")
        return_code = 1

    finally:
        if summary_path and summary_path.exists():
            summary_path.unlink()
        try:
            _refresh_local_context()
        except Exception as exc:
            print(f"SMOKE CLEANUP FAIL: {exc}")
            return_code = 1

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())

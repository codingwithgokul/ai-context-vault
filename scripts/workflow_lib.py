#!/usr/bin/env python3
"""Shared helpers for AI Context Vault workflow scripts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib import request
from urllib.error import HTTPError, URLError

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = REPO_ROOT / ".memory"
INDEX_PATH = MEMORY_DIR / "index.json"
RESUME_PATH = MEMORY_DIR / "resume_context.txt"
BLOB_SYNC_STATE_PATH = MEMORY_DIR / "blob_sync_state.json"

TRACKED_EXTENSIONS = {".md", ".yaml", ".yml", ".csv", ".txt"}
EXCLUDE_DIRS = {".git", ".memory", "backups", "__pycache__", ".venv", "venv", "env"}
SUMMARY_DIRNAME = "session_summaries"

TOPIC_TO_DIR = {
    "architecture": "docs/session_summaries",
    "requirements": "examples/session_summaries",
    "evaluation": "docs/session_summaries",
    "methodology": "docs/session_summaries",
    "general": "examples/session_summaries",
}

TOPIC_HINTS = {
    "architecture": ["architecture", "architektur", "rq2", "gate", "quality gate"],
    "requirements": ["requirement", "anforderung", "rq1", "must", "should"],
    "evaluation": ["evaluation", "rq3", "interview", "coverage", "validierung"],
    "methodology": ["method", "methodik", "dsr", "design science", "research design"],
}

DEFAULT_REPO_SCOPE = "vault"
DEFAULT_SUMMARY_TYPE = "technisch"
DEFAULT_SOURCE_REPO = "ai-context-vault"


@dataclass
class FileEntry:
    path: str
    size_bytes: int
    modified_utc: str
    lines: int


def load_dotenv(path: Path | None = None) -> None:
    env_file = path or (REPO_ROOT / ".env")
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            # .env values always win over empty/missing env vars
            if key not in os.environ or not os.environ[key]:
                os.environ[key] = value


def _tls_context() -> ssl.SSLContext | None:
    """Build TLS context with proper CA bundle; fallback to system defaults.

    Uses secure defaults and only disables verification if explicitly requested.
    """
    insecure = os.getenv("AZURE_INSECURE_TLS", "").lower() in {"1", "true", "yes"}
    if insecure:
        return ssl._create_unverified_context()

    explicit_ca = os.getenv("SSL_CERT_FILE", "") or os.getenv("REQUESTS_CA_BUNDLE", "")
    if explicit_ca and Path(explicit_ca).exists():
        return ssl.create_default_context(cafile=explicit_ca)

    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _load_yaml(path: Path) -> dict:
    if yaml is None:
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            value = yaml.safe_load(f)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _dump_yaml(path: Path, payload: dict) -> None:
    if yaml is None:
        raise RuntimeError("PyYAML missing. Install with: pip install pyyaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def iter_source_files() -> Iterable[Path]:
    for p in REPO_ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in TRACKED_EXTENSIONS:
            continue
        yield p


def _slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "summary"


def detect_topic(text: str) -> str:
    lower = text.lower()
    best_topic = "general"
    best_score = 0
    for topic, hints in TOPIC_HINTS.items():
        score = sum(lower.count(h) for h in hints)
        if score > best_score:
            best_score = score
            best_topic = topic
    return best_topic


def summarize_text_to_bullets(text: str, max_bullets: int = 8) -> list[str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bullets = []

    for ln in lines:
        cleaned = re.sub(r"^[-*\d.)\s]+", "", ln).strip()
        if len(cleaned) < 18:
            continue
        if len(cleaned) > 220:
            cleaned = cleaned[:217].rstrip() + "..."
        if cleaned not in bullets:
            bullets.append(cleaned)
        if len(bullets) >= max_bullets:
            break

    if len(bullets) < max_bullets:
        joined = " ".join(lines)
        sentences = re.split(r"(?<=[.!?])\s+", joined)
        for sent in sentences:
            sent = sent.strip()
            if len(sent) < 24:
                continue
            if len(sent) > 220:
                sent = sent[:217].rstrip() + "..."
            if sent not in bullets:
                bullets.append(sent)
            if len(bullets) >= max_bullets:
                break

    return bullets[:max_bullets]


def extract_actions(text: str) -> tuple[list[str], list[str]]:
    decisions: list[str] = []
    next_steps: list[str] = []
    for raw in text.splitlines():
        line = raw.strip().lower()
        if not line:
            continue
        if any(k in line for k in ["decision", "entscheidung", "we will", "we choose"]):
            decisions.append(raw.strip())
        if any(k in line for k in ["next", "todo", "next step", "offen", "naechste", "nächste"]):
            next_steps.append(raw.strip())
    return decisions[:6], next_steps[:8]


def anthropic_configured() -> bool:
    load_dotenv()
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _anthropic_chat_complete(messages: list[dict], max_tokens: int = 300) -> str:
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    model = os.getenv("ANTHROPIC_SUMMARY_MODEL", "claude-haiku-4-5-20251001")
    temperature = float(os.getenv("ANTHROPIC_TEMPERATURE", "0.1"))

    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing.")

    system_msg = ""
    anthropic_messages = []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
        else:
            anthropic_messages.append({"role": m["role"], "content": m["content"]})

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": anthropic_messages,
    }
    if system_msg:
        payload["system"] = system_msg

    req = request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    context = _tls_context()
    with request.urlopen(req, timeout=45, context=context) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        result = json.loads(body)
        # Extract text from Anthropic response format
        content_blocks = result.get("content", [])
        return content_blocks[0].get("text", "") if content_blocks else ""


def summarize_with_claude(text: str, max_bullets: int = 8) -> tuple[list[str], list[str], list[str], str]:
    max_input_chars = int(os.getenv("ANTHROPIC_MAX_INPUT_CHARS", "6000"))
    safe_text = text[:max_input_chars]
    prompt = (
        "Summarize this work session in concise project notes. "
        "Return ONLY valid JSON with keys: title (string), summary_bullets (array), "
        "decisions (array), next_steps (array). "
        f"Limit summary_bullets to max {max_bullets}. "
        "Return raw JSON only, no markdown fences."
    )
    messages = [
        {"role": "system", "content": "You write compact and precise engineering notes. Always respond with raw JSON only."},
        {"role": "user", "content": f"{prompt}\n\nSESSION:\n{safe_text}"},
    ]
    content = _anthropic_chat_complete(messages, max_tokens=int(os.getenv("ANTHROPIC_MAX_OUTPUT_TOKENS", "2000")))
    # Strip markdown fences if present
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    # Repair truncated JSON: close open strings/arrays/objects
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        repaired = content.rstrip().rstrip(",")
        for ch in ['"', "]", "}"]:
            try:
                parsed = json.loads(repaired)
                break
            except json.JSONDecodeError:
                repaired += ch
        else:
            parsed = json.loads(repaired)

    bullets = parsed.get("summary_bullets", []) or []
    decisions = parsed.get("decisions", []) or []
    next_steps = parsed.get("next_steps", []) or []
    title = parsed.get("title", "") or ""

    def _clean(items: list, limit: int) -> list[str]:
        out: list[str] = []
        for i in items:
            s = str(i).strip()
            if not s:
                continue
            if len(s) > 220:
                s = s[:217].rstrip() + "..."
            if s not in out:
                out.append(s)
            if len(out) >= limit:
                break
        return out

    return _clean(bullets, max_bullets), _clean(decisions, 6), _clean(next_steps, 8), title


def azure_openai_configured() -> bool:
    load_dotenv()
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    key = os.getenv("AZURE_OPENAI_API_KEY", "") or os.getenv("AZURE_OPENAI_KEY", "")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "") or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "")
    return bool(endpoint and key and deployment)


def _azure_openai_chat_complete(messages: list[dict]) -> dict:
    load_dotenv()
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    key = os.getenv("AZURE_OPENAI_API_KEY", "") or os.getenv("AZURE_OPENAI_KEY", "")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "") or os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT", "")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    max_tokens = int(os.getenv("AZURE_OPENAI_MAX_OUTPUT_TOKENS", "300"))
    temperature = float(os.getenv("AZURE_OPENAI_TEMPERATURE", "0.1"))

    if not endpoint or not key or not deployment:
        raise RuntimeError("AZURE_OPENAI_* config missing.")

    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "api-key": key},
    )
    context = _tls_context()
    with request.urlopen(req, timeout=45, context=context) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)


def summarize_with_azure_openai(text: str, max_bullets: int = 8) -> tuple[list[str], list[str], list[str], str]:
    max_input_chars = int(os.getenv("AZURE_OPENAI_MAX_INPUT_CHARS", "6000"))
    safe_text = text[:max_input_chars]
    prompt = (
        "Summarize this work session in concise project notes. "
        "Return ONLY valid JSON with keys: title (string), summary_bullets (array), "
        "decisions (array), next_steps (array). "
        f"Limit summary_bullets to max {max_bullets}."
    )
    messages = [
        {"role": "system", "content": "You write compact and precise engineering notes."},
        {"role": "user", "content": f"{prompt}\n\nSESSION:\n{safe_text}"},
    ]
    result = _azure_openai_chat_complete(messages)
    content = result.get("choices", [{}])[0].get("message", {}).get("content", "{}")
    parsed = json.loads(content)

    bullets = parsed.get("summary_bullets", []) or []
    decisions = parsed.get("decisions", []) or []
    next_steps = parsed.get("next_steps", []) or []
    title = parsed.get("title", "") or ""

    def _clean(items: list, limit: int) -> list[str]:
        out: list[str] = []
        for i in items:
            s = str(i).strip()
            if not s:
                continue
            if len(s) > 220:
                s = s[:217].rstrip() + "..."
            if s not in out:
                out.append(s)
            if len(out) >= limit:
                break
        return out

    return _clean(bullets, max_bullets), _clean(decisions, 6), _clean(next_steps, 8), title


def summary_output_path(topic: str, title: str | None = None) -> Path:
    rel_dir = TOPIC_TO_DIR.get(topic, TOPIC_TO_DIR["general"])
    target_dir = REPO_ROOT / rel_dir
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = _slugify(title or topic)
    return target_dir / f"{ts}_{suffix}.yaml"


def _summary_metadata_defaults() -> tuple[str, str, str]:
    load_dotenv()
    repo_scope = os.getenv("SUMMARY_REPO_SCOPE", DEFAULT_REPO_SCOPE).strip() or DEFAULT_REPO_SCOPE
    summary_type = os.getenv("SUMMARY_TYPE", DEFAULT_SUMMARY_TYPE).strip() or DEFAULT_SUMMARY_TYPE
    source_repo = os.getenv("SUMMARY_SOURCE_REPO", DEFAULT_SOURCE_REPO).strip() or DEFAULT_SOURCE_REPO
    return repo_scope, summary_type, source_repo


def save_session_summary(
    text: str,
    topic: str = "auto",
    title: str | None = None,
    source: str = "manual",
    tags: list[str] | None = None,
    use_llm: bool = True,
) -> tuple[Path, dict]:
    resolved_topic = detect_topic(text) if topic == "auto" else topic.lower().strip()
    if resolved_topic not in TOPIC_TO_DIR:
        resolved_topic = "general"

    llm_used = False
    engine_name = "local_rules"
    llm_error = ""
    llm_title = ""
    bullets: list[str] = []
    decisions: list[str] = []
    next_steps: list[str] = []

    # 3-tier fallback: Claude (Anthropic) → Azure OpenAI → local rules
    if use_llm and anthropic_configured():
        try:
            bullets, decisions, next_steps, llm_title = summarize_with_claude(text)
            llm_used = True
            engine_name = "anthropic_claude"
        except Exception as e:
            llm_error = f"[anthropic] {e}"

    if use_llm and not llm_used and azure_openai_configured():
        try:
            bullets, decisions, next_steps, llm_title = summarize_with_azure_openai(text)
            llm_used = True
            engine_name = "azure_openai"
        except Exception as e:
            llm_error += f" [aoai] {e}" if llm_error else str(e)

    if not bullets:
        bullets = summarize_text_to_bullets(text)
    if not decisions or not next_steps:
        local_decisions, local_next = extract_actions(text)
        if not decisions:
            decisions = local_decisions
        if not next_steps:
            next_steps = local_next

    path = summary_output_path(resolved_topic, title)
    final_title = title or llm_title or "Session Summary"

    repo_scope, summary_type, source_repo = _summary_metadata_defaults()

    payload = {
        "id": f"SUM-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "topic": resolved_topic,
        "target_folder": str(path.parent.relative_to(REPO_ROOT)),
        "repo_scope": repo_scope,
        "summary_type": summary_type,
        "source_repo": source_repo,
        "title": final_title,
        "summary_bullets": bullets,
        "decisions": decisions,
        "next_steps": next_steps,
        "tags": tags or [],
        "source": source,
        "summary_engine": engine_name,
    }
    if llm_error:
        payload["summary_engine_error"] = llm_error[:300]

    _dump_yaml(path, payload)
    return path, payload


def load_session_summaries(limit: int | None = None) -> list[dict]:
    rows: list[dict] = []
    default_repo_scope, default_summary_type, default_source_repo = _summary_metadata_defaults()
    for p in sorted(REPO_ROOT.rglob(f"{SUMMARY_DIRNAME}/*.yaml"), reverse=True):
        doc = _load_yaml(p)
        if not doc:
            continue
        doc.setdefault("repo_scope", default_repo_scope)
        doc.setdefault("summary_type", default_summary_type)
        doc.setdefault("source_repo", default_source_repo)
        doc["path"] = str(p.relative_to(REPO_ROOT))
        rows.append(doc)
        if limit and len(rows) >= limit:
            break
    return rows


def build_index() -> dict:
    entries = []
    for path in sorted(iter_source_files()):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.count("\n") + (1 if text else 0)
            stat = path.stat()
            entries.append(
                FileEntry(
                    path=str(path.relative_to(REPO_ROOT)),
                    size_bytes=stat.st_size,
                    modified_utc=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    lines=lines,
                ).__dict__
            )
        except Exception:
            continue

    summaries = load_session_summaries(limit=None)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(REPO_ROOT),
        "files": entries,
        "session_summaries": summaries,
    }


def write_index(index: dict) -> Path:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return INDEX_PATH


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, minimum)


def _parse_created_at(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _select_resume_summaries(summaries: list[dict]) -> tuple[list[dict], int, int, int]:
    window_hours = _env_int("RESUME_WINDOW_HOURS", 24)
    min_items = _env_int("RESUME_MIN_ITEMS", 3)
    max_items = _env_int("RESUME_MAX_ITEMS", 10)
    if min_items > max_items:
        min_items = max_items

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=window_hours)

    ranked: list[tuple[dict, datetime, int]] = []
    for index, summary in enumerate(summaries):
        created = _parse_created_at(str(summary.get("created_at", "")))
        if created is None:
            created = datetime.fromtimestamp(0, tz=timezone.utc)
        ranked.append((summary, created, index))

    ranked.sort(key=lambda item: (item[1], -item[2]), reverse=True)

    selected: list[dict] = []
    selected_keys: set[str] = set()

    def key_for(summary: dict, index: int) -> str:
        return str(summary.get("id") or summary.get("path") or f"idx-{index}")

    for summary, created, index in ranked:
        if created >= cutoff:
            selected.append(summary)
            selected_keys.add(key_for(summary, index))

    if len(selected) < min_items:
        for summary, _, index in ranked:
            key = key_for(summary, index)
            if key in selected_keys:
                continue
            selected.append(summary)
            selected_keys.add(key)
            if len(selected) >= min_items:
                break

    return selected[:max_items], window_hours, min_items, max_items


def build_resume_text(index: dict) -> str:
    lines = []
    lines.append("Here is my current project status - use this as context:")
    lines.append("")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Repo: {index.get('repo_root','')}")
    lines.append(f"Indexed artifacts: {len(index.get('files', []))}")
    lines.append("")

    summaries = index.get("session_summaries", [])
    if not isinstance(summaries, list):
        summaries = []
    selected, window_hours, min_items, max_items = _select_resume_summaries(summaries)

    lines.append("Latest session summaries:")
    lines.append(f"(selection: last {window_hours}h, min {min_items}, max {max_items})")
    if not selected:
        lines.append("- No session summaries available yet.")
    else:
        for s in selected:
            topic = s.get("topic", "general")
            title = s.get("title", "Session Summary")
            bullets = s.get("summary_bullets", [])
            first = bullets[0] if bullets else "(no bullet)"
            lines.append(f"- [{topic}] {title}: {first}")

    return "\n".join(lines).strip() + "\n"


def write_resume_text(text: str) -> Path:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    RESUME_PATH.write_text(text, encoding="utf-8")
    return RESUME_PATH


def azure_configured() -> bool:
    load_dotenv()
    endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")
    key = os.getenv("AZURE_SEARCH_ADMIN_KEY") or os.getenv("AZURE_SEARCH_KEY")
    index_name = os.getenv("AZURE_SEARCH_INDEX_NAME") or os.getenv("AZURE_SEARCH_INDEX")
    return bool(endpoint and key and index_name)


def blob_configured() -> bool:
    load_dotenv()
    account = os.getenv("AZURE_STORAGE_ACCOUNT")
    key = os.getenv("AZURE_STORAGE_KEY")
    return bool(account and key)


def _fetch_azure_index_schema(endpoint: str, key: str, index_name: str, api_version: str) -> tuple[dict, str]:
    url = f"{endpoint}/indexes/{index_name}?api-version={api_version}"
    req = request.Request(url, method="GET", headers={"api-key": key})
    context = _tls_context()
    with request.urlopen(req, timeout=30, context=context) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        schema = json.loads(body)
        return schema, body


def _summary_docs_for_azure(index: dict, schema: dict) -> tuple[list[dict], str]:
    fields = schema.get("fields", []) if isinstance(schema, dict) else []
    by_name = {f.get("name"): f for f in fields if isinstance(f, dict) and f.get("name")}
    string_fields = [f.get("name") for f in fields if f.get("type") == "Edm.String" and f.get("name")]
    key_candidates = [f.get("name") for f in fields if f.get("key") is True and f.get("name")]

    configured_key = os.getenv("AZURE_SEARCH_KEY_FIELD", "")
    if configured_key and configured_key in by_name:
        key_field = configured_key
    elif key_candidates:
        key_field = key_candidates[0]
    elif "id" in by_name:
        key_field = "id"
    else:
        return [], "No key field found in search index schema."

    configured_content = os.getenv("AZURE_SEARCH_CONTENT_FIELD", "")
    content_priorities = [configured_content, "content", "text", "summary", "body", "chunk", "message"]
    content_field = ""
    for c in content_priorities:
        if c and c in by_name:
            content_field = c
            break
    if not content_field:
        for sf in string_fields:
            if sf != key_field:
                content_field = sf
                break
    if not content_field:
        return [], "No suitable content field (Edm.String) found in search index schema."

    title_field = "title" if "title" in by_name else ("name" if "name" in by_name else "")
    topic_field = "topic" if "topic" in by_name else ("category" if "category" in by_name else "")
    path_field = "path" if "path" in by_name else ""
    source_field = "source_path" if "source_path" in by_name else ("source" if "source" in by_name else "")
    created_field = "created_at" if "created_at" in by_name else ("timestamp" if "timestamp" in by_name else "")
    doc_type_field = "doc_type" if "doc_type" in by_name else ""
    chapter_field = "chapter" if "chapter" in by_name else ""
    blob_name_field = "blob_name" if "blob_name" in by_name else ""
    tags_field = "tags" if "tags" in by_name else ""
    eu_ai_act_refs_field = "eu_ai_act_refs" if "eu_ai_act_refs" in by_name else ""
    repo_scope_field = "repo_scope" if "repo_scope" in by_name else ""
    summary_type_field = "summary_type" if "summary_type" in by_name else ""
    source_repo_field = "source_repo" if "source_repo" in by_name else ""

    docs = []
    for s in index.get("session_summaries", []):
        sid = s.get("id") or _slugify(s.get("path", "summary"))
        bullets = s.get("summary_bullets", [])
        decisions = s.get("decisions", []) or []
        next_steps = s.get("next_steps", []) or []
        path_value = s.get("path", "")
        topic_value = s.get("topic", "general")
        tags_value = s.get("tags", []) or []
        chapter_value = (path_value.split("/", 1)[0] if path_value else "") or s.get("target_folder", "")
        eu_ai_act_refs = s.get("eu_ai_act_refs", []) or []

        content_parts = []
        if s.get("title"):
            content_parts.append(str(s["title"]))
        if bullets:
            content_parts.extend(f"- {b}" for b in bullets)
        if decisions:
            content_parts.append("Decisions:")
            content_parts.extend(f"- {d}" for d in decisions)
        if next_steps:
            content_parts.append("Next steps:")
            content_parts.extend(f"- {n}" for n in next_steps)
        content = "\n".join(content_parts).strip()

        doc = {"@search.action": "mergeOrUpload", key_field: sid, content_field: content}
        if title_field:
            doc[title_field] = s.get("title", "Session Summary")
        if topic_field:
            doc[topic_field] = topic_value
        if path_field:
            doc[path_field] = path_value
        if source_field:
            doc[source_field] = path_value
        if created_field:
            doc[created_field] = s.get("created_at", "")
        if doc_type_field:
            doc[doc_type_field] = "session_summary"
        if chapter_field:
            doc[chapter_field] = chapter_value
        if blob_name_field:
            doc[blob_name_field] = path_value
        if tags_field:
            doc[tags_field] = ", ".join(str(tag) for tag in tags_value if str(tag).strip())
        if eu_ai_act_refs_field:
            doc[eu_ai_act_refs_field] = ", ".join(str(ref) for ref in eu_ai_act_refs if str(ref).strip())
        if repo_scope_field:
            doc[repo_scope_field] = s.get("repo_scope", "")
        if summary_type_field:
            doc[summary_type_field] = s.get("summary_type", "")
        if source_repo_field:
            doc[source_repo_field] = s.get("source_repo", "")
        docs.append(doc)

    return docs, f"Schema detected: key={key_field}, content={content_field}"


def push_index_to_azure(index: dict) -> tuple[bool, str]:
    load_dotenv()
    endpoint = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
    key = os.getenv("AZURE_SEARCH_ADMIN_KEY", "") or os.getenv("AZURE_SEARCH_KEY", "")
    index_name = os.getenv("AZURE_SEARCH_INDEX_NAME", "") or os.getenv("AZURE_SEARCH_INDEX", "")
    api_version = os.getenv("AZURE_SEARCH_API_VERSION", "2023-11-01")

    if not endpoint or not key or not index_name:
        return False, "Azure config missing (AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_SEARCH_INDEX)."

    try:
        schema, _ = _fetch_azure_index_schema(endpoint, key, index_name, api_version)
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return False, f"Azure HTTP error during schema read {e.code}: {body[:400]}"
    except URLError as e:
        return False, f"Azure network error during schema read: {e}"

    docs, schema_msg = _summary_docs_for_azure(index, schema)
    if not docs:
        return True, f"{schema_msg} Nothing to upload."

    url = f"{endpoint}/indexes/{index_name}/docs/index?api-version={api_version}"
    payload = json.dumps({"value": docs}).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "api-key": key},
    )

    try:
        context = _tls_context()
        with request.urlopen(req, timeout=30, context=context) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, f"{schema_msg}. Search index updated ({len(docs)} docs). Response: {body[:200]}"
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return False, f"Azure HTTP error {e.code}: {body[:400]}"
    except URLError as e:
        return False, f"Azure network error: {e}"


def _blob_container_name() -> str:
    return os.getenv("AZURE_BLOB_CONTAINER", "session-summaries")


def _list_summary_files() -> list[Path]:
    return sorted(REPO_ROOT.rglob(f"{SUMMARY_DIRNAME}/*.yaml"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_blob_sync_state() -> dict:
    if not BLOB_SYNC_STATE_PATH.exists():
        return {}
    try:
        return json.loads(BLOB_SYNC_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_blob_sync_state(state: dict) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    BLOB_SYNC_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def push_summaries_to_blob() -> tuple[bool, str]:
    load_dotenv()
    account = os.getenv("AZURE_STORAGE_ACCOUNT", "")
    key = os.getenv("AZURE_STORAGE_KEY", "")
    container = _blob_container_name()

    if not account or not key:
        return False, "Blob config missing (AZURE_STORAGE_ACCOUNT, AZURE_STORAGE_KEY)."

    files = _list_summary_files()
    if not files:
        return True, "No session summaries found, nothing to upload."

    state = _load_blob_sync_state()
    previous_container = state.get("container") if isinstance(state, dict) else None
    synced = state.get("synced_hashes", {}) if isinstance(state, dict) else {}
    if not isinstance(synced, dict):
        synced = {}

    # If the target container changed, force a one-time full upload to avoid cross-project cache skips.
    if previous_container and previous_container != container:
        synced = {}

    try:
        create_cmd = [
            "az",
            "storage",
            "container",
            "create",
            "--name",
            container,
            "--account-name",
            account,
            "--account-key",
            key,
            "--auth-mode",
            "key",
            "--output",
            "none",
        ]
        subprocess.run(create_cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        return False, "Azure CLI (az) not found."
    except subprocess.CalledProcessError as e:
        return False, f"Container creation failed: {(e.stderr or e.stdout).strip()[:300]}"

    uploaded = 0
    skipped = 0
    new_synced: dict[str, str] = {}

    for file_path in files:
        rel = file_path.relative_to(REPO_ROOT).as_posix()
        file_hash = _sha256_file(file_path)
        new_synced[rel] = file_hash

        if synced.get(rel) == file_hash:
            skipped += 1
            continue

        cmd = [
            "az",
            "storage",
            "blob",
            "upload",
            "--container-name",
            container,
            "--account-name",
            account,
            "--account-key",
            key,
            "--auth-mode",
            "key",
            "--file",
            str(file_path),
            "--name",
            rel,
            "--overwrite",
            "true",
            "--output",
            "none",
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            uploaded += 1
        except subprocess.CalledProcessError as e:
            return False, f"Blob upload failed ({rel}): {(e.stderr or e.stdout).strip()[:300]}"

    _write_blob_sync_state(
        {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "container": container,
            "synced_hashes": new_synced,
        }
    )

    return True, f"Blob sync OK: {uploaded} uploaded, {skipped} unchanged (container '{container}')."

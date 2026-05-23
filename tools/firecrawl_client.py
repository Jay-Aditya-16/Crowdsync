"""Firecrawl client — pulls live web data into CrowdSync.

Used for:
- Live cricket scoreboard refresh (replaces mocked match_state.json)
- Threat intel sweep (news + weather warnings around the venue)

We expose two thin sync helpers: scrape_markdown(url) and search(query).
Caches results on disk for the duration of a demo to stay under rate limits."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "firecrawl_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 180  # 3 min — fresh enough for live cricket


def _api_key() -> Optional[str]:
    return os.getenv("FIRECRAWL_API_KEY")


def _cache_path(key: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.json"


def _cache_get(key: str) -> Optional[dict]:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if time.time() - data.get("_cached_at", 0) > CACHE_TTL_SECONDS:
        return None
    return data


def _cache_put(key: str, value: dict) -> None:
    value = {**value, "_cached_at": time.time()}
    _cache_path(key).write_text(json.dumps(value, indent=2, default=str))


def _client():
    """Lazy import — firecrawl-py 2.x uses FirecrawlApp."""
    from firecrawl import FirecrawlApp  # type: ignore
    key = _api_key()
    if not key:
        raise RuntimeError("FIRECRAWL_API_KEY not set")
    return FirecrawlApp(api_key=key)


def _normalize_scrape_result(result) -> tuple[str, dict]:
    """Extract markdown + metadata regardless of whether the SDK returned dict or object."""
    if isinstance(result, dict):
        data = result.get("data", result)
        return data.get("markdown", "") or "", data.get("metadata", {}) or {}
    md = getattr(result, "markdown", None)
    if md is None:
        data = getattr(result, "data", None) or {}
        if isinstance(data, dict):
            md = data.get("markdown", "")
        else:
            md = getattr(data, "markdown", "") or ""
    metadata = getattr(result, "metadata", None) or {}
    if hasattr(metadata, "__dict__"):
        metadata = vars(metadata)
    return md or "", metadata


def scrape_markdown(url: str, use_cache: bool = True) -> dict:
    """Scrape a URL to markdown. Returns {url, markdown, metadata, source}."""
    cache_key = f"scrape::{url}"
    if use_cache:
        cached = _cache_get(cache_key)
        if cached:
            return {**cached, "source": "cache"}

    try:
        result = _client().scrape_url(url, formats=["markdown"])
        markdown, metadata = _normalize_scrape_result(result)
        out = {
            "url": url,
            "markdown": markdown,
            "metadata": metadata,
            "source": "live",
        }
        _cache_put(cache_key, out)
        return out
    except Exception as e:
        return {
            "url": url,
            "markdown": "",
            "metadata": {},
            "source": "error",
            "error": str(e),
        }


def scrape_many(urls: list[str]) -> list[dict]:
    return [scrape_markdown(u) for u in urls]

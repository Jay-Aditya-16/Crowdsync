"""Browser-Use Cloud API wrapper.

Lets agents drive a real cloud browser to take real-world actions —
scrape live traffic, post advisories, look up routes. The `live_url` from
session creation lets us embed a real-time stream in the dashboard so
operators (and judges) can watch the agent click around.

Sync polling (Streamlit doesn't love asyncio). Caches sessions by task
string for the duration of the demo to avoid burning quota."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE = "https://api.browser-use.com/api/v3"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "browseruse_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL = 600  # 10 min — browser tasks are expensive

_DEFAULT_POLL_INTERVAL = 3.0
_DEFAULT_MAX_WAIT = 90.0  # seconds


def _api_key() -> str:
    key = os.getenv("BROWSER_USE_API_KEY")
    if not key:
        raise RuntimeError("BROWSER_USE_API_KEY not set")
    return key


def _headers() -> dict:
    return {
        "X-Browser-Use-API-Key": _api_key(),
        "Content-Type": "application/json",
    }


@dataclass
class BrowserSession:
    session_id: str
    status: str
    live_url: Optional[str] = None
    output: Optional[str] = None
    error: Optional[str] = None
    elapsed: float = 0.0


def start_session(task: str, model: Optional[str] = None) -> BrowserSession:
    """Start a browser-use session. Returns immediately with live_url."""
    body: dict = {"task": task}
    if model:
        body["model"] = model
    r = requests.post(f"{API_BASE}/sessions", headers=_headers(), json=body, timeout=20)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"browser-use {r.status_code}: {r.text[:300]}")
    d = r.json()
    return BrowserSession(
        session_id=d.get("id") or d.get("session_id") or "",
        status=d.get("status", "unknown"),
        live_url=d.get("live_url") or d.get("liveUrl"),
    )


def get_session(session_id: str) -> BrowserSession:
    r = requests.get(f"{API_BASE}/sessions/{session_id}", headers=_headers(), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"browser-use get {r.status_code}: {r.text[:300]}")
    d = r.json()
    return BrowserSession(
        session_id=session_id,
        status=d.get("status", "unknown"),
        live_url=d.get("live_url") or d.get("liveUrl"),
        output=d.get("output"),
        error=d.get("error"),
    )


def _cache_key(task: str) -> str:
    import hashlib
    return hashlib.sha1(task.encode()).hexdigest()[:16]


def _cache_get(task: str) -> Optional[dict]:
    path = CACHE_DIR / f"{_cache_key(task)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if time.time() - data.get("_cached_at", 0) > CACHE_TTL:
        return None
    return data


def _cache_put(task: str, value: dict) -> None:
    payload = {**value, "_cached_at": time.time()}
    (CACHE_DIR / f"{_cache_key(task)}.json").write_text(json.dumps(payload, indent=2, default=str))


def run_task(
    task: str,
    max_wait: float = _DEFAULT_MAX_WAIT,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    use_cache: bool = True,
    model: Optional[str] = None,
) -> dict:
    """Start a session, poll until completion or timeout. Returns dict with status, output, live_url.

    Cached so the same task doesn't spawn a new session within CACHE_TTL.
    """
    if use_cache:
        cached = _cache_get(task)
        if cached:
            return {**cached, "source": "cache"}

    try:
        session = start_session(task, model=model)
    except Exception as e:
        return {"status": "error", "error": str(e), "live_url": None, "output": None, "source": "start_error"}

    start_time = time.time()
    terminal = {"idle", "stopped", "error", "timed_out"}
    final = session
    while time.time() - start_time < max_wait:
        time.sleep(poll_interval)
        try:
            final = get_session(session.session_id)
        except Exception as e:
            return {"status": "error", "error": str(e), "live_url": session.live_url, "output": None, "session_id": session.session_id, "source": "poll_error"}
        if final.status in terminal:
            break

    result = {
        "session_id": final.session_id,
        "status": final.status,
        "live_url": final.live_url or session.live_url,
        "output": final.output,
        "error": final.error,
        "elapsed": round(time.time() - start_time, 1),
        "source": "live",
    }
    if final.status in terminal and final.output:
        _cache_put(task, result)
    return result


def kickoff_session(task: str) -> BrowserSession:
    """Fire-and-forget: start a session and return immediately with the live_url
    so the UI can stream it. Caller polls separately via session_id."""
    return start_session(task)

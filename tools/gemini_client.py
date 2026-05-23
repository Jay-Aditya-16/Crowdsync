"""LLM client (OpenRouter-backed despite the legacy filename).

Originally wrapped Google Gemini; now routed through OpenRouter to pick up
free-tier access to DeepSeek V3.1 (text + JSON) and Llama 3.2 Vision
(multimodal). The agent layer keeps using `chat`, `chat_json`, and
`analyze_video`, so the swap is invisible to callers.

For mp4 inputs we extract 3 evenly-spaced frames with OpenCV and send them
as base64-encoded images via the OpenAI multi-image content format that
OpenRouter accepts.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Local rate limiter — OpenRouter free tier caps at ~20 req/min across all
# free models. We keep our own bucket to avoid surprise 429s mid-render.
# ---------------------------------------------------------------------------

class _TokenBucket:
    def __init__(self, max_per_minute: int):
        self.max = max_per_minute
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def try_acquire(self, block_max: float = 0.0) -> bool:
        """Return True if we can fire now; optionally block up to block_max seconds."""
        deadline = time.time() + block_max
        while True:
            with self._lock:
                now = time.time()
                while self._events and now - self._events[0] > 60.0:
                    self._events.popleft()
                if len(self._events) < self.max:
                    self._events.append(now)
                    return True
                wait_for = 60.0 - (now - self._events[0]) + 0.1
            if block_max == 0 or time.time() + wait_for > deadline:
                return False
            time.sleep(min(wait_for, 1.0))


_LIMITER = _TokenBucket(max_per_minute=int(os.getenv("OPENROUTER_RPM_BUDGET", "16")))

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_TEXT_MODEL = os.getenv("OPENROUTER_TEXT_MODEL", "liquid/lfm-2.5-1.2b-instruct:free")
_TEXT_FALLBACK_MODEL = os.getenv("OPENROUTER_TEXT_FALLBACK", "baidu/cobuddy:free")
_DEFAULT_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", "nvidia/nemotron-nano-12b-v2-vl:free")
_REQUEST_TIMEOUT = 20  # short — free models that hang must fail fast

# Gemini direct-REST fallback: only used when OpenRouter chain is exhausted
# (rate-limit or daily cap). Separate quota from OpenRouter so we stay alive.
GEMINI_REST_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")
LAST_PROVIDER = {"value": None}  # debug breadcrumb


def _api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    return key


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        # Optional referer / app title — OpenRouter shows these in your dashboard.
        "HTTP-Referer": "https://crowdsync.local",
        "X-Title": "CrowdSync",
    }


def _post(payload: dict, _retried: bool = False) -> str:
    # Local budget guard. If we're over, raise a clean rate-limit error so
    # the caller can fall back / show a friendly message.
    if not _LIMITER.try_acquire(block_max=0):
        raise RuntimeError("rate_limited_local: local budget exhausted; try again in a few seconds")

    r = requests.post(OPENROUTER_URL, headers=_headers(), json=payload, timeout=_REQUEST_TIMEOUT)

    if r.status_code == 429 and not _retried:
        # Honor the server-side reset hint, then retry once.
        retry_after = 1.0
        try:
            reset_ms = int(r.headers.get("X-RateLimit-Reset", "0"))
            if reset_ms > 0:
                retry_after = max(1.0, min(8.0, (reset_ms / 1000) - time.time()))
        except Exception:
            pass
        time.sleep(retry_after)
        return _post(payload, _retried=True)

    if r.status_code != 200:
        raise RuntimeError(f"openrouter {r.status_code}: {r.text[:400]}")

    data = r.json()
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"unexpected openrouter response: {data}") from e


# ---------------------------------------------------------------------------
# Text + JSON
# ---------------------------------------------------------------------------

def _gemini_chat(prompt: str, system: Optional[str], model: str) -> str:
    """Direct REST call to Google's Gemini API. Used as last-resort fallback
    when OpenRouter free tier is exhausted."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("no GEMINI_API_KEY for fallback")

    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    url = GEMINI_REST_URL.format(model=model) + f"?key={key}"
    r = requests.post(url, json=body, timeout=_REQUEST_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"gemini-fallback {r.status_code}: {r.text[:400]}")
    data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"unexpected gemini response: {data}") from e


def chat(prompt: str, system: Optional[str] = None, model: Optional[str] = None) -> str:
    """Single-turn chat. Tries OpenRouter primary → OpenRouter fallback → Gemini
    (separate-quota safety net). LAST_PROVIDER records which one served."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    primary = model or _DEFAULT_TEXT_MODEL
    chain = [(primary, "openrouter")]
    if _TEXT_FALLBACK_MODEL and _TEXT_FALLBACK_MODEL != primary:
        chain.append((_TEXT_FALLBACK_MODEL, "openrouter"))

    last_err: Exception | None = None
    for m, provider in chain:
        try:
            result = _post({"model": m, "messages": messages, "temperature": 0.4})
            LAST_PROVIDER["value"] = f"{provider}:{m}"
            return result
        except Exception as e:
            last_err = e
            continue

    # OpenRouter chain exhausted. Try Gemini.
    if os.getenv("GEMINI_API_KEY"):
        try:
            result = _gemini_chat(prompt, system, _GEMINI_FALLBACK_MODEL)
            LAST_PROVIDER["value"] = f"gemini:{_GEMINI_FALLBACK_MODEL}"
            return result
        except Exception as e:
            last_err = e

    raise last_err if last_err else RuntimeError("all providers exhausted")


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


def chat_json(prompt: str, system: Optional[str] = None, model: Optional[str] = None) -> dict:
    """Single-turn chat constrained to JSON. Tolerates ```json fences and pre/post text."""
    full_prompt = prompt + "\n\nRespond with ONLY valid JSON. No markdown, no commentary, no preamble."
    raw = chat(full_prompt, system=system, model=model)
    raw = _strip_fences(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
        raise


# ---------------------------------------------------------------------------
# Vision — mp4 → sample frames → multi-image chat
# ---------------------------------------------------------------------------

def _sample_frames(video_path: Path, n: int = 3) -> list[bytes]:
    """Return n JPEG-encoded frames evenly spaced through the clip."""
    import cv2  # heavy import — only when needed
    cap = cv2.VideoCapture(str(video_path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if total <= 0:
        cap.release()
        return []
    indices = [int(total * (i + 0.5) / n) for i in range(n)]
    frames: list[bytes] = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        # Downscale for token economy
        h, w = frame.shape[:2]
        max_dim = 720
        if max(h, w) > max_dim:
            scale = max_dim / max(h, w)
            frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            frames.append(buf.tobytes())
    cap.release()
    return frames


def analyze_video(video_path: str | Path, prompt: str, model: Optional[str] = None) -> dict:
    """Extract frames from an mp4, send to OpenRouter vision model, return parsed JSON."""
    path = Path(video_path)
    if not path.exists():
        raise FileNotFoundError(str(path))

    frames = _sample_frames(path, n=3)
    if not frames:
        raise RuntimeError(f"could not extract frames from {path}")

    schema_hint = (
        "Return ONLY a JSON object with keys: density_pct (int 0-100), trend "
        "('stable'|'rising'|'rising_fast'|'falling'), anomalies (list of strings), "
        "confidence (float 0-1), summary (one sentence). No commentary."
    )

    content: list[dict] = [{"type": "text", "text": prompt + "\n\n" + schema_hint}]
    for fb in frames:
        b64 = base64.b64encode(fb).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    payload = {
        "model": model or _DEFAULT_VISION_MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.3,
    }
    raw = _post(payload)
    raw = _strip_fences(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
        raise

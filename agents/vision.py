"""Vision Agent.

Analyzes short stadium video clips for crowd density + anomalies via Gemini
multimodal. Falls back to cached responses if the clip is missing or the
API call fails (so demos never break)."""
from __future__ import annotations

import json
from pathlib import Path

from tools.gemini_client import analyze_video
from tools import supabase_client

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CLIPS_DIR = DATA_DIR / "clips"
CACHE_PATH = DATA_DIR / "cached_vision.json"

VISION_PROMPT = """You are the Vision Agent for a stadium safety system.
Analyze this clip of a stadium concourse / stand.

Report:
- density_pct: estimated crowd density as % of zone capacity (0-100).
- trend: 'stable' | 'rising' | 'rising_fast' | 'falling' based on movement.
- anomalies: list of detected issues from this set: ['bottleneck_near_exit',
  'rapid_movement', 'directional_chaos', 'possible_fall', 'fight_indicator',
  'smoke_or_fire', 'unattended_bag', 'medical_emergency']. Empty list if none.
- confidence: 0.0-1.0 estimate of your certainty.
- summary: one short sentence (<20 words) for the operator dashboard."""


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def analyze_clip(clip_name: str) -> dict:
    """Run the Vision Agent on a clip in data/clips/. Falls back to cache."""
    cache = _load_cache()
    clip_path = CLIPS_DIR / clip_name

    if not clip_path.exists():
        cached = cache.get(clip_name)
        if cached:
            return {**cached, "source": "cache_miss_file_missing", "clip": clip_name}
        return {
            "density_pct": 0,
            "trend": "stable",
            "anomalies": [],
            "confidence": 0.0,
            "summary": f"Clip {clip_name} not found and not in cache.",
            "source": "missing",
            "clip": clip_name,
        }

    try:
        result = analyze_video(clip_path, VISION_PROMPT)
        result["source"] = "gemini_live"
        result["clip"] = clip_name
        if supabase_client.is_enabled():
            supabase_client.log_agent_decision(
                agent_name="vision",
                action=f"analyze_clip:{clip_name}",
                reasoning=result.get("summary", ""),
                confidence=float(result.get("confidence") or 0.0),
                payload={"density_pct": result.get("density_pct"), "anomalies": result.get("anomalies", [])},
            )
        return result
    except Exception as e:
        cached = cache.get(clip_name)
        if cached:
            return {**cached, "source": f"cache_fallback ({e})", "clip": clip_name}
        return {
            "density_pct": 0,
            "trend": "stable",
            "anomalies": [],
            "confidence": 0.0,
            "summary": f"Vision Agent error: {e}",
            "source": "error",
            "clip": clip_name,
        }


def list_available_clips() -> list[str]:
    if not CLIPS_DIR.exists():
        return []
    return sorted(p.name for p in CLIPS_DIR.glob("*.mp4"))

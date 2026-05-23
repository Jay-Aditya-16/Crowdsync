"""Match Context Agent.

Cricket-aware crowd-surge predictor. Reasons over match state, weather,
and recent events to forecast where crowds will move in the next N minutes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from tools.firecrawl_client import scrape_markdown
from tools.gemini_client import chat_json

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

SYSTEM = """You are the Match Context Agent inside CrowdSync — a stadium crowd
safety platform. You forecast where crowds will surge based on cricket match state,
weather, and stadium zone information.

Cricket-specific rules of thumb:
- A wicket near end of innings triggers light restroom surge in 2-4 min.
- An innings break triggers heavy concession + restroom surge (3-7 min).
- Last 3 overs: fans start preparing to exit; concourse density rises.
- Match end: heavy exit surge for ~20 min, biggest bottleneck at primary gates.
- Boundary (4 or 6): brief spike then settles, low impact.
- Rain (>60% probability in next 15 min): surge to covered areas.

Always return STRICT JSON. Use confidence 0.0-1.0. Severity is one of:
low, medium, high, critical."""


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text())


def predict_surge(zone_id: Optional[str] = None, minutes_ahead: int = 10) -> dict:
    """Predict crowd surge in a specific zone (or all zones) within N minutes."""
    match_state = _load("match_state.json")
    zones = _load("stadium_zones.json")

    if zone_id:
        target_zones = [z for z in zones["zones"] if z["id"] == zone_id]
    else:
        target_zones = zones["zones"]

    prompt = f"""MATCH STATE:
{json.dumps(match_state, indent=2)}

ZONES TO ANALYZE (predict surge for each):
{json.dumps(target_zones, indent=2)}

TIME WINDOW: next {minutes_ahead} minutes

For each zone, predict:
- expected_density_pct (0-100, where 100 = at capacity)
- trend ('stable' | 'rising' | 'rising_fast' | 'falling')
- driver (one short phrase: what cricket/weather event causes this)
- minutes_until_peak (int)
- severity ('low' | 'medium' | 'high' | 'critical')
- confidence (0.0-1.0)

Return JSON: {{"predictions": [{{"zone_id": ..., "expected_density_pct": ..., "trend": ..., "driver": ..., "minutes_until_peak": ..., "severity": ..., "confidence": ...}}], "overall_summary": "one sentence"}}
"""
    try:
        result = chat_json(prompt, system=SYSTEM)
        result["llm_source"] = "live"
        return result
    except Exception as e:
        # Heuristic fallback so the demo still works when all LLM providers
        # are rate-limited. Uses cricket rules-of-thumb encoded directly:
        # last 3 overs → exit prep, wicket → light restroom surge, rain →
        # covered-zone surge, etc.
        return _heuristic_predict(match_state, target_zones, str(e))


def _heuristic_predict(match_state: dict, target_zones: list[dict], err: str) -> dict:
    """Pure-Python fallback predictor for when LLM is unavailable.
    Returns the same shape as the live LLM call so callers don't care which path served."""
    score = match_state.get("score", {})
    runs = score.get("runs", 0); wkts = score.get("wickets", 0); target = score.get("target") or 0
    current_over = match_state.get("current_over", 0)
    balls_remaining = match_state.get("balls_remaining", 36)
    rain_pct = match_state.get("weather", {}).get("rain_probability_pct", 0)

    near_end = balls_remaining <= 18
    last_wicket = (match_state.get("recent_events") or [{}])[0]
    wicket_just_fell = last_wicket.get("event") == "wicket"
    chase_close = target and (target - runs) <= 30 and wkts >= 6

    predictions: list[dict] = []
    for z in target_zones:
        zid = z["id"]
        density = 60
        trend = "stable"
        driver = "baseline distribution"
        severity = "low"
        peak = 8

        if zid == "PITCH":
            continue
        if z["type"] == "stand":
            density += 15
        if near_end:
            density += 12
            trend = "rising"
            driver = "fans preparing to exit in final overs"
            severity = "medium"
            peak = 4
        if wicket_just_fell and zid in ("N_STAND", "A_STAND", "C_LOWER", "P2_STAND"):
            density += 8
            driver = "post-wicket restroom + concession surge"
        if chase_close and zid in ("A_STAND", "C_UPPER", "G_UPPER"):
            density += 6
            driver = "tense chase — fans staying engaged but moving"
        if rain_pct >= 50 and zid in ("CLUB_HOUSE", "P_CORPORATE", "PAVILION_TERRACE"):
            density += 18
            trend = "rising_fast"
            driver = f"rain forecast {rain_pct}% — migration to covered area"
            severity = "high"
            peak = 3

        density = max(20, min(98, density))
        if density >= 85: severity = "high"
        elif density >= 70 and severity == "low": severity = "medium"

        predictions.append({
            "zone_id": zid,
            "expected_density_pct": density,
            "trend": trend,
            "driver": driver,
            "minutes_until_peak": peak,
            "severity": severity,
            "confidence": 0.55,
        })

    return {
        "predictions": predictions,
        "overall_summary": (
            f"Heuristic predictor active (LLM unavailable: {err[:80]}). "
            f"Current driver: {'final-overs exit prep' if near_end else 'mid-match steady state'}."
        ),
        "llm_source": "heuristic_fallback",
        "error": err,
    }


def get_match_state() -> dict:
    """Read-only view of current match state. Useful for other agents."""
    return _load("match_state.json")


def refresh_from_live_scoreboard(url: Optional[str] = None) -> dict:
    """Pull a live cricket scoreboard via Firecrawl + Gemini, overwrite match_state.

    Returns the new state, plus a `refresh_meta` block describing the source.
    Falls back to existing state on error."""
    import os
    scoreboard_url = url or os.getenv("LIVE_SCOREBOARD_URL") or "https://www.cricbuzz.com/cricket-match/live-scores"

    scraped = scrape_markdown(scoreboard_url)
    if not scraped.get("markdown"):
        existing = _load("match_state.json")
        existing["refresh_meta"] = {
            "status": "no_content",
            "url": scoreboard_url,
            "error": scraped.get("error"),
        }
        return existing

    extraction_prompt = f"""You are reading a scraped cricket scoreboard page.
Extract the FIRST in-progress or most prominent match into this JSON structure:

{{
  "teams": {{"home": "...", "away": "..."}},
  "format": "T20|ODI|Test",
  "current_innings": 1|2,
  "current_over": <int>,
  "current_ball": <int 0-5>,
  "score": {{"runs": <int>, "wickets": <int>, "target": <int or null>}},
  "balls_remaining": <int>,
  "wickets_in_hand": <int>,
  "required_run_rate": <float or null>
}}

Use 0 / null for fields you cannot confidently extract. Do not invent data.

SCRAPED CONTENT (truncated):
{(scraped.get('markdown') or '')[:6000]}
"""
    try:
        extracted = chat_json(extraction_prompt)
    except Exception as e:
        existing = _load("match_state.json")
        existing["refresh_meta"] = {"status": "extract_failed", "url": scoreboard_url, "error": str(e)}
        return existing

    state = _load("match_state.json")
    for k in ["teams", "format", "current_innings", "current_over", "current_ball",
              "score", "balls_remaining", "wickets_in_hand", "required_run_rate"]:
        if k in extracted and extracted[k] is not None:
            state[k] = extracted[k]
    state["match_id"] = f"LIVE_{state['teams'].get('home','?')}_vs_{state['teams'].get('away','?')}"
    state["refresh_meta"] = {
        "status": "ok",
        "url": scoreboard_url,
        "source": scraped.get("source"),
    }
    (DATA_DIR / "match_state.json").write_text(json.dumps(state, indent=2))
    return state


def advance_over() -> dict:
    """Demo helper: advance the match by one over and add a wicket event."""
    path = DATA_DIR / "match_state.json"
    state = json.loads(path.read_text())
    state["current_over"] = state.get("current_over", 0) + 1
    state["balls_remaining"] = max(0, state.get("balls_remaining", 0) - 6)
    state["score"]["wickets"] = min(10, state["score"]["wickets"] + 1)
    state["recent_events"].insert(0, {
        "over": state["current_over"],
        "ball": 6,
        "event": "wicket",
        "description": "Wicket falls (demo advance)",
    })
    state["recent_events"] = state["recent_events"][:5]
    state["next_events"][1]["minutes_from_now"] = max(
        0, state["next_events"][1]["minutes_from_now"] - 4
    )
    path.write_text(json.dumps(state, indent=2))
    return state

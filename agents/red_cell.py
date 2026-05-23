"""Red Cell Agent — adversarial scenario hunter.

Most predictive systems forecast the most-likely future. The Red Cell
does the opposite: it actively SEARCHES the perturbation space for the
worst-case scenarios our stadium plan can survive. Outputs the top
vulnerabilities so operators can shore them up *before* the threat
materializes.

Pure compute (reuses the existing Monte Carlo engine) — no LLM cost,
runs in well under a second even with the full perturbation matrix.
"""
from __future__ import annotations

import json
from pathlib import Path

from agents.whatif_simulator import compare, current_baseline_state
from tools import supabase_client

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _candidate_perturbations() -> list[dict]:
    """Enumerate adversarial perturbation candidates worth testing."""
    zones = json.loads((DATA_DIR / "stadium_zones.json").read_text())
    main_gates = [g["id"] for g in zones["gates"] if g.get("kind") == "main"]

    candidates: list[dict] = []
    # Single-gate closures (main gates only — biggest impact)
    for gid in main_gates:
        candidates.append({"type": "close_gate", "gate_id": gid, "label": f"Close main gate {gid}"})
    # Weather shocks
    candidates.append({"type": "weather_rain", "label": "Sudden heavy rain"})
    # Wicket-at-innings-break surge
    candidates.append({"type": "wicket_end_innings", "label": "Wicket at innings break"})
    # Match end exit surge
    candidates.append({"type": "match_end", "label": "Match ends now"})
    # Incident in a high-density zone
    for zid in ["A_STAND", "C_UPPER", "G_UPPER"]:
        candidates.append({"type": "incident_zone", "zone_id": zid, "label": f"Security incident in {zid}"})
    return candidates


def _score(scenario_result: dict, baseline_result: dict) -> float:
    """Weighted risk score: heavier weight on crush than evac time."""
    delta_crush = scenario_result["p_crush"] - baseline_result["p_crush"]
    delta_evac = (scenario_result["evac_minutes"]["p50"] - baseline_result["evac_minutes"]["p50"]) / 30.0
    delta_slow = scenario_result["p_slow_evac"] - baseline_result["p_slow_evac"]
    # Crush dominates; slow evac is secondary; evac time tertiary
    return 0.6 * delta_crush + 0.3 * delta_slow + 0.1 * delta_evac


def hunt(risk_level: str = "medium", trials_per_scenario: int = 80, top_k: int = 3) -> dict:
    """Run the Red Cell sweep. Returns top-K vulnerabilities ranked by composite risk delta."""
    candidates = _candidate_perturbations()
    results: list[dict] = []

    for cand in candidates:
        try:
            cmp = compare({k: v for k, v in cand.items() if k != "label"},
                          risk_level=risk_level, trials=trials_per_scenario)
        except Exception as e:
            results.append({**cand, "error": str(e), "score": -1})
            continue

        scen = cmp["scenario"]
        base = cmp["baseline"]
        score = _score(scen, base)

        results.append({
            "label": cand.get("label", str(cand)),
            "perturbation": {k: v for k, v in cand.items() if k != "label"},
            "score": round(score, 4),
            "p_crush": scen["p_crush"],
            "delta_p_crush": round(scen["p_crush"] - base["p_crush"], 3),
            "evac_p50": scen["evac_minutes"]["p50"],
            "delta_evac_p50": round(scen["evac_minutes"]["p50"] - base["evac_minutes"]["p50"], 1),
            "p_slow_evac": scen["p_slow_evac"],
            "top_crush_zones": scen["top_crush_zones"][:2],
        })

    results.sort(key=lambda r: -r.get("score", 0))
    top = results[:top_k]

    # Pick a worst zone across top vulnerabilities for the headline
    worst_zone = None
    if top and top[0].get("top_crush_zones"):
        worst_zone = top[0]["top_crush_zones"][0].get("zone_id")

    out = {
        "risk_level": risk_level,
        "trials_per_scenario": trials_per_scenario,
        "candidates_evaluated": len(candidates),
        "top_vulnerabilities": top,
        "worst_zone_overall": worst_zone,
        "headline": _headline(top),
    }
    # Only log when we found something noteworthy — avoids spamming audit
    # trail with "no vulnerabilities" every minute.
    if supabase_client.is_enabled() and top and top[0].get("delta_p_crush", 0) >= 0.05:
        supabase_client.log_agent_decision(
            agent_name="red_cell",
            action="hunt_top_vulnerability",
            reasoning=_headline(top),
            payload={"top": top[0], "candidates_evaluated": len(candidates), "risk_level": risk_level},
        )
    return out


def _headline(top: list[dict]) -> str:
    if not top:
        return "No critical vulnerabilities detected."
    t = top[0]
    return (
        f"CRITICAL: '{t['label']}' shifts P(crush) by {t['delta_p_crush']:+.1%} "
        f"and stretches evacuation by {t['delta_evac_p50']:+.1f} min. "
        f"Pre-position resources for {t.get('top_crush_zones', [{}])[0].get('zone_id', 'affected zone')}."
    )

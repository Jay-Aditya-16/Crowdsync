"""What-If Simulator + Monte Carlo Threat Predictor.

Two responsibilities:

1. **What-If**: take a perturbation to the stadium topology (close gate G3,
   open gate G14, rain starts, etc.) and project crowd state 2-5 minutes
   into the future. Returns predicted zone densities, evacuation timings,
   and identified crush points.

2. **Monte Carlo**: rather than a single deterministic forecast, sample
   uncertainty over hundreds of trials. Outputs probability distributions:
   P(crush in 2 min), P(evac > 10 min), 5th/50th/95th percentile zone
   densities. Width of the distribution widens when Threat Intel surfaces
   active risks (weather, transit disruption, protests near venue).

In production the loop would re-fire every 5s, feeding back into the
Commander Agent. For the demo it runs on click — same engine, different
trigger.
"""
from __future__ import annotations

import json
import math
import random
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tools.gemini_client import chat_json

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text())


# ---------------------------------------------------------------------------
# Topology + baseline state
# ---------------------------------------------------------------------------

# Per-zone-type fill bias and explicit per-zone tweaks for realism.
# General stands are near-full; premium is ~70%; A_STAND (cheap general) packed.
_FILL_BIAS = {"stand": 0.95, "premium": 0.65}
_ZONE_OVERRIDE = {
    "A_STAND": 1.02,
    "B_UPPER": 0.92,
    "B_LOWER": 0.88,
    "C_UPPER": 0.95,
    "C_LOWER": 0.92,
    "N_STAND": 0.93,
    "P_CORPORATE": 0.55,
    "CLUB_HOUSE": 0.70,
}


def _baseline_state() -> dict:
    """Combine current match state + zones + gates into a snapshot the
    simulator can perturb. Density is seeded by attendance and a
    type-aware distribution (general stands fill before premium)."""
    zones = _load("stadium_zones.json")
    match = _load("match_state.json")

    attendance = match.get("attendance", 35000)
    seatable_capacity = sum(z["capacity"] for z in zones["zones"] if z["type"] != "field")
    base_fill = min(1.0, attendance / max(1, seatable_capacity))

    state = {
        "attendance": attendance,
        "fill_ratio": base_fill,
        "zones": {},
        "gates": {g["id"]: dict(g) for g in zones["gates"]},
        "match": match,
        "geometry": zones.get("geometry", {}),
    }
    for z in zones["zones"]:
        if z["type"] == "field":
            continue
        cap = z["capacity"]
        bias = _FILL_BIAS.get(z["type"], 1.0)
        bias *= _ZONE_OVERRIDE.get(z["id"], 1.0)
        fill = min(0.99, base_fill * bias)
        occ = int(cap * fill)
        state["zones"][z["id"]] = {
            **z,
            "occupants": occ,
            "density_pct": int(round(100 * occ / max(1, cap))),
        }
    return state


# ---------------------------------------------------------------------------
# Perturbation model
# ---------------------------------------------------------------------------

PERTURBATIONS = {
    "close_gate": "Close a specific gate — fans rerouted to adjacent gates.",
    "open_gate": "Open an additional gate (e.g., auxiliary) — relieves pressure.",
    "weather_rain": "Heavy rain starts now — fans in open stands surge to covered areas.",
    "match_end": "Match concludes — full-stadium exit begins.",
    "wicket_end_innings": "Final wicket at innings break — heavy concession + restroom surge.",
    "incident_zone": "Security incident reported in a zone — local panic + exit demand.",
}


def apply_perturbation(state: dict, perturbation: dict) -> dict:
    """Mutate (a copy of) state according to the perturbation dict."""
    new_state = json.loads(json.dumps(state))
    ptype = perturbation.get("type")

    if ptype == "close_gate":
        gate_id = perturbation.get("gate_id")
        if gate_id and gate_id in new_state["gates"]:
            gate = new_state["gates"][gate_id]
            gate["is_open"] = False
            # Reroute that gate's throughput to neighboring gates on the same side.
            side = gate.get("side", "")
            siblings = [g for g in new_state["gates"].values() if g.get("side") == side and g["id"] != gate_id and g.get("is_open")]
            if siblings:
                share = gate.get("throughput_per_min", 0) / len(siblings)
                for s in siblings:
                    s["throughput_per_min"] = s.get("throughput_per_min", 0) + share * 0.7  # 70% recovered, 30% lost to congestion
            new_state.setdefault("notes", []).append(f"Gate {gate_id} closed; throughput rerouted to {[s['id'] for s in siblings]}")

    elif ptype == "open_gate":
        gate_id = perturbation.get("gate_id")
        if gate_id and gate_id in new_state["gates"]:
            new_state["gates"][gate_id]["is_open"] = True

    elif ptype == "weather_rain":
        # Fans in stands without 'P_CORPORATE' / 'PAVILION' / 'CLUB' migrate.
        covered = {"P_CORPORATE", "PAVILION_TERRACE", "CLUB_HOUSE"}
        migrating = 0
        for zid, z in new_state["zones"].items():
            if zid not in covered:
                drop = int(z["occupants"] * 0.25)
                z["occupants"] -= drop
                z["density_pct"] = int(round(100 * z["occupants"] / max(1, z["capacity"])))
                migrating += drop
        # Distribute migration into covered zones
        targets = [z for zid, z in new_state["zones"].items() if zid in covered]
        if targets:
            share = migrating // len(targets)
            for t in targets:
                t["occupants"] += share
                t["density_pct"] = int(round(100 * t["occupants"] / max(1, t["capacity"])))
        new_state.setdefault("notes", []).append(f"Rain perturbation: {migrating} fans migrated to covered areas")

    elif ptype == "match_end":
        # 100% of fans now headed for exits — exit demand spikes for ~15 min
        new_state["exit_demand_multiplier"] = 1.8
        new_state.setdefault("notes", []).append("Match end: full-stadium exit demand activated")

    elif ptype == "wicket_end_innings":
        # Concourse + amenity zones see 30% surge
        for zid in ["N_STAND", "A_STAND", "C_LOWER", "P2_STAND", "P3_STAND"]:
            if zid in new_state["zones"]:
                z = new_state["zones"][zid]
                z["occupants"] = int(z["occupants"] * 1.3)
                z["density_pct"] = int(round(100 * z["occupants"] / max(1, z["capacity"])))
        new_state.setdefault("notes", []).append("Final wicket: amenity surge in 5 main stands")

    elif ptype == "incident_zone":
        zid = perturbation.get("zone_id")
        if zid and zid in new_state["zones"]:
            z = new_state["zones"][zid]
            z["panic_factor"] = 1.0
            z["density_pct"] = min(100, int(z["density_pct"] * 1.15))
            new_state.setdefault("notes", []).append(f"Incident at {zid}: panic_factor=1.0")

    return new_state


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------

@dataclass
class TrialResult:
    peak_density: dict[str, int] = field(default_factory=dict)
    evac_minutes: float = 0.0
    crush_event: bool = False
    crush_zones: list[str] = field(default_factory=list)


def _evac_time_minutes(state: dict, exit_multiplier: float = 1.0) -> float:
    """Crude evacuation time estimate: total occupants / total open-gate throughput."""
    occupants = sum(z["occupants"] for z in state["zones"].values())
    throughput = sum(g.get("throughput_per_min", 0) for g in state["gates"].values() if g.get("is_open"))
    if throughput <= 0:
        return 999.0
    return (occupants * exit_multiplier) / throughput


def _crush_check(state: dict, threshold_pct: int = 95) -> list[str]:
    """A 'crush' is a zone above 95% density — the threshold where actual
    crush incidents are documented in stadium safety literature."""
    return [zid for zid, z in state["zones"].items() if z["density_pct"] >= threshold_pct]


def monte_carlo(
    state: dict,
    perturbation: Optional[dict] = None,
    trials: int = 200,
    risk_level: str = "low",
    seed: Optional[int] = None,
) -> dict:
    """Run N stochastic trials and aggregate to probability distributions.

    Sampling model:
    - Each zone's true density ~ Normal(reported_density, sigma_density)
    - Each open gate's effective throughput ~ Normal(declared, sigma_throughput)
    - sigma is amplified by risk_level (low/medium/high) from Threat Intel.

    Output keys:
    - p_crush: probability of any zone hitting >=90% density
    - p_slow_evac: probability evacuation > 10 minutes
    - density_percentiles: per-zone {p5, p50, p95}
    - evac_minutes: {p5, p50, p95}
    - top_crush_zones: zones with highest crush probability
    - inputs: snapshot of perturbation + risk_level + trials
    """
    if seed is not None:
        random.seed(seed)

    sigma_density = {"low": 4, "medium": 8, "high": 14}.get(risk_level, 6)
    sigma_throughput_pct = {"low": 0.08, "medium": 0.18, "high": 0.32}.get(risk_level, 0.12)

    base = apply_perturbation(state, perturbation) if perturbation else json.loads(json.dumps(state))
    exit_mult = base.get("exit_demand_multiplier", 1.0)

    per_zone_densities: dict[str, list[int]] = {zid: [] for zid in base["zones"]}
    per_zone_crush_counts: dict[str, int] = {zid: 0 for zid in base["zones"]}
    evac_samples: list[float] = []
    crush_count = 0
    slow_evac_count = 0

    for _ in range(trials):
        trial = json.loads(json.dumps(base))
        for zid, z in trial["zones"].items():
            mean = z["density_pct"]
            sampled = random.gauss(mean, sigma_density)
            sampled = max(0, min(100, int(round(sampled))))
            z["density_pct"] = sampled
            z["occupants"] = int(z["capacity"] * sampled / 100)
            per_zone_densities[zid].append(sampled)
            if sampled >= 95:
                per_zone_crush_counts[zid] += 1

        for gid, g in trial["gates"].items():
            if not g.get("is_open"):
                continue
            decl = g.get("throughput_per_min", 0)
            jitter = random.gauss(1.0, sigma_throughput_pct)
            g["throughput_per_min"] = max(0, decl * jitter)

        crush_zones = _crush_check(trial)
        if crush_zones:
            crush_count += 1
        evac = _evac_time_minutes(trial, exit_multiplier=exit_mult)
        evac_samples.append(evac)
        if evac > 10:
            slow_evac_count += 1

    def _pcts(values: list[float]) -> dict:
        if not values:
            return {"p5": 0, "p50": 0, "p95": 0, "mean": 0}
        s = sorted(values)
        def q(p):
            idx = max(0, min(len(s) - 1, int(p * (len(s) - 1))))
            return s[idx]
        return {
            "p5": round(q(0.05), 2),
            "p50": round(q(0.50), 2),
            "p95": round(q(0.95), 2),
            "mean": round(statistics.fmean(values), 2),
        }

    density_percentiles = {zid: _pcts(vals) for zid, vals in per_zone_densities.items()}

    top_crush = sorted(per_zone_crush_counts.items(), key=lambda kv: -kv[1])[:5]

    return {
        "trials": trials,
        "risk_level": risk_level,
        "p_crush": round(crush_count / trials, 3),
        "p_slow_evac": round(slow_evac_count / trials, 3),
        "evac_minutes": _pcts(evac_samples),
        "density_percentiles": density_percentiles,
        "top_crush_zones": [
            {"zone_id": zid, "p_crush": round(c / trials, 3)} for zid, c in top_crush
        ],
        "notes": base.get("notes", []),
        "inputs": {
            "perturbation": perturbation,
            "risk_level": risk_level,
            "exit_multiplier": exit_mult,
        },
    }


# ---------------------------------------------------------------------------
# High-level entry points
# ---------------------------------------------------------------------------

def current_baseline_state() -> dict:
    return _baseline_state()


def simulate(
    perturbation: Optional[dict] = None,
    risk_level: str = "low",
    trials: int = 200,
) -> dict:
    """Public API: run Monte Carlo from current baseline."""
    state = _baseline_state()
    return monte_carlo(state, perturbation=perturbation, risk_level=risk_level, trials=trials)


def compare(perturbation: dict, risk_level: str = "low", trials: int = 200) -> dict:
    """Run two Monte Carlos (current vs perturbed) for side-by-side display."""
    state = _baseline_state()
    return {
        "baseline": monte_carlo(state, perturbation=None, risk_level=risk_level, trials=trials),
        "scenario": monte_carlo(state, perturbation=perturbation, risk_level=risk_level, trials=trials),
        "perturbation": perturbation,
    }


def narrate_scenario(comparison: dict) -> str:
    """Gemini narrates the difference between baseline and scenario in plain English."""
    prompt = f"""You are the What-If Simulator's narrator. Explain the difference
between baseline and the perturbed scenario in 3-4 short sentences for a stadium
control-room operator. Focus on: change in crush probability, change in
evacuation time, and which zones became more dangerous.

DATA:
{json.dumps({
    'baseline': {
        'p_crush': comparison['baseline']['p_crush'],
        'p_slow_evac': comparison['baseline']['p_slow_evac'],
        'evac_minutes': comparison['baseline']['evac_minutes'],
        'top_crush_zones': comparison['baseline']['top_crush_zones'][:3],
    },
    'scenario': {
        'p_crush': comparison['scenario']['p_crush'],
        'p_slow_evac': comparison['scenario']['p_slow_evac'],
        'evac_minutes': comparison['scenario']['evac_minutes'],
        'top_crush_zones': comparison['scenario']['top_crush_zones'][:3],
    },
    'perturbation': comparison['perturbation'],
}, indent=2)}

Return JSON: {{"summary": "<3-4 sentences>", "recommendation": "<one imperative sentence>"}}"""
    try:
        return chat_json(prompt)
    except Exception as e:
        scen = comparison['scenario']
        return {
            "summary": (
                f"Scenario {comparison['perturbation']}: P(crush) "
                f"{comparison['baseline']['p_crush']:.0%} → {scen['p_crush']:.0%}; "
                f"evac median {comparison['baseline']['evac_minutes']['p50']} → {scen['evac_minutes']['p50']} min."
            ),
            "recommendation": "Review scenario manually — narrator unavailable.",
            "error": str(e),
        }

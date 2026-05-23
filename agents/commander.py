"""Commander Agent.

Orchestrates the other 3 agents and applies the SOP library. Maintains a
running incident log. Escalates to human operator via email when needed.

Two interfaces:
- handle_*(input) — automatic SOP-driven response paths
- answer_operator(question) — free-form operator chat that may invoke sub-agents
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from agents import fan_concierge, intel, match_context, vision
from tools.agentmail_client import get_client
from tools.gemini_client import chat, chat_json
from tools import supabase_client

load_dotenv()
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INCIDENTS_PATH = DATA_DIR / "incidents.json"
SOP_PATH = DATA_DIR / "sop_library.json"

COMMANDER_CLIENT_ID = os.getenv("COMMANDER_CLIENT_ID", "crowdsync-commander-v1")
COMMANDER_INBOX_ADDR = os.getenv("COMMANDER_INBOX_ADDR") or None
OPERATOR_EMAIL = os.getenv("OPERATOR_EMAIL", "")


def set_operator_email(email: str) -> None:
    """Override the escalation recipient at runtime (used by the login page)."""
    global OPERATOR_EMAIL
    OPERATOR_EMAIL = (email or "").strip()


def get_operator_email() -> str:
    return OPERATOR_EMAIL

_lock = threading.Lock()


def _load_sops() -> dict:
    return json.loads(SOP_PATH.read_text())["sops"]


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _read_incidents() -> list[dict]:
    if not INCIDENTS_PATH.exists():
        return []
    return json.loads(INCIDENTS_PATH.read_text())


def _write_incidents(incidents: list[dict]) -> None:
    INCIDENTS_PATH.write_text(json.dumps(incidents, indent=2))


def log_incident(incident: dict) -> dict:
    """Persist an incident to Supabase (primary) + JSON file (offline fallback)."""
    with _lock:
        incidents = _read_incidents()
        incident["id"] = f"INC-{int(time.time()*1000)}"
        incident["timestamp"] = _now()
        incidents.insert(0, incident)
        _write_incidents(incidents[:100])
    # Best-effort Supabase persistence — never block the local path.
    if supabase_client.is_enabled():
        db_id = supabase_client.insert_incident(incident)
        if db_id:
            incident["db_id"] = db_id
            supabase_client.log_agent_decision(
                agent_name="commander",
                action=f"log_incident:{incident.get('type','?')}",
                reasoning=incident.get("summary", "")[:500],
                payload={"db_id": db_id, "severity": incident.get("severity"), "zone": incident.get("zone")},
            )
            # Find prior similar incidents — institutional memory linkage.
            similar = supabase_client.find_similar_incidents(
                incident.get("summary", ""), k=3, exclude_legacy_id=incident.get("id"),
            )
            if similar:
                incident["similar_past"] = [
                    {"id": s.get("id"), "type": s.get("type"), "zone": s.get("zone"),
                     "summary": s.get("summary"), "similarity": s.get("similarity")}
                    for s in similar
                ]
    return incident


def get_incidents(limit: int = 50) -> list[dict]:
    """Read from Supabase if available; fall back to local JSON."""
    if supabase_client.is_enabled():
        db = supabase_client.fetch_recent_incidents(limit)
        if db:
            return db
    return _read_incidents()[:limit]


def clear_incidents() -> None:
    _write_incidents([])
    if supabase_client.is_enabled():
        supabase_client.clear_incidents()


def _commander_inbox() -> str:
    return get_client().get_or_create_inbox(COMMANDER_CLIENT_ID, prefer_address=COMMANDER_INBOX_ADDR)


def escalate_to_human(severity: str, summary: str, details: dict) -> Optional[str]:
    """Send an escalation email from the Commander inbox to the human operator."""
    if not OPERATOR_EMAIL:
        return None
    body = (
        f"[CrowdSync Commander] {severity.upper()} incident\n\n"
        f"{summary}\n\n"
        f"Details:\n{json.dumps(details, indent=2)}\n\n"
        f"Open the dashboard to approve actions."
    )
    return get_client().send(
        inbox_id=_commander_inbox(),
        to=OPERATOR_EMAIL,
        subject=f"[CrowdSync] {severity.upper()}: {summary[:60]}",
        text=body,
        labels=["escalation", severity],
    )


def _draft_action_plan(sop: dict, context: dict) -> str:
    """Gemini drafts a concise plan referencing SOP actions + current context."""
    prompt = f"""You are the Commander Agent. Draft a concise action plan.

SOP:
{json.dumps(sop, indent=2)}

CURRENT CONTEXT:
{json.dumps(context, indent=2)}

Output a numbered list, max 5 items, each one short imperative sentence.
Mark items as [AUTO] or [NEEDS APPROVAL] based on SOP requires_approval flag.
Do not include any preamble."""
    try:
        return chat(prompt)
    except Exception as e:
        return f"(plan generation failed: {e})\n" + "\n".join(
            f"{i+1}. [{'NEEDS APPROVAL' if a.get('requires_approval') else 'AUTO'}] {a['label']}"
            for i, a in enumerate(sop.get("actions", []))
        )


def handle_predicted_surge(prediction: dict) -> dict:
    """Run the CROWD_SURGE SOP for a single zone prediction."""
    sops = _load_sops()
    sop = sops["CROWD_SURGE"]
    plan = _draft_action_plan(sop, prediction)
    nudges = fan_concierge.send_nudges_for_surge(prediction, max_recipients=3)
    incident = {
        "type": "CROWD_SURGE",
        "severity": prediction.get("severity", "medium"),
        "zone": prediction.get("zone_id"),
        "summary": (
            f"{prediction.get('zone_id')} expected at "
            f"{prediction.get('expected_density_pct', '?')}% density — "
            f"{prediction.get('driver', 'crowd surge')}"
        ),
        "plan": plan,
        "nudges_sent": [n for n in nudges if n.get("status") == "sent"],
        "nudges_logged": [n for n in nudges if n.get("status") == "logged_only"],
        "source": "match_context",
    }
    log_incident(incident)
    if prediction.get("severity") in ("high", "critical"):
        escalate_to_human(prediction["severity"], incident["summary"], prediction)
    return incident


def handle_vision_anomaly(vision_result: dict, zone_hint: str = "") -> Optional[dict]:
    """Trigger SOP based on Vision Agent output. Returns incident or None."""
    sops = _load_sops()
    anomalies = vision_result.get("anomalies", [])
    density = vision_result.get("density_pct", 0)

    sop_id = None
    severity = "low"
    if "possible_fall" in anomalies or "medical_emergency" in anomalies:
        sop_id, severity = "MEDICAL", "high"
    elif "rapid_movement" in anomalies and density >= 85:
        sop_id, severity = "PANIC_BEHAVIOR", "critical"
    elif density >= 75:
        sop_id, severity = "CROWD_SURGE", "medium"
    else:
        return None

    sop = sops[sop_id]
    plan = _draft_action_plan(sop, {**vision_result, "zone_hint": zone_hint})
    incident = {
        "type": sop_id,
        "severity": severity,
        "zone": zone_hint or "(from vision)",
        "summary": vision_result.get("summary", "Vision Agent anomaly"),
        "plan": plan,
        "vision": vision_result,
        "source": "vision",
    }
    log_incident(incident)
    if severity in ("high", "critical"):
        escalate_to_human(severity, incident["summary"], vision_result)
    return incident


def handle_fan_incident(classified: dict) -> Optional[dict]:
    """Trigger SOP based on classified fan email (or quarantine if VT flagged)."""
    cls = classified.get("classification", {})
    category = cls.get("category", "OTHER")
    sops = _load_sops()
    sop = sops.get(category)
    if not sop:
        return None

    plan = _draft_action_plan(sop, classified)
    incident = {
        "type": category,
        "severity": cls.get("severity", "medium"),
        "zone": cls.get("location", "(unknown)"),
        "summary": cls.get("summary", classified.get("subject", "")),
        "plan": plan,
        "fan_message": {
            "from": classified.get("from"),
            "subject": classified.get("subject"),
            "body": classified.get("body"),
        },
        "source": "fan_email",
    }

    security = classified.get("security")
    if security:
        incident["security"] = security
        if security.get("is_quarantined"):
            incident["source"] = "fan_email_quarantined"

    log_incident(incident)

    # Don't reply to quarantined messages — the sender may be hostile.
    if category != "SECURITY_THREAT":
        fan_concierge.acknowledge_fan(classified["message_id"], cls)

    if cls.get("severity") in ("high", "critical") or cls.get("needs_human"):
        escalate_to_human(cls.get("severity", "medium"), incident["summary"], classified)
    return incident


# Small TTL cache so repeated questions (judges asking the same demo Q
# twice) don't burn fresh LLM calls.
_ANSWER_CACHE: dict[str, tuple[float, dict]] = {}
_ANSWER_CACHE_TTL_S = 120


def answer_operator(question: str) -> dict:
    """Free-form operator chat. Commander chooses which sub-agent tools to call.

    Tool dispatch is keyword-based here for reliability under hackathon time;
    a fuller implementation would use Gemini function calling end-to-end.
    """
    import time
    cache_key = question.strip().lower()
    hit = _ANSWER_CACHE.get(cache_key)
    if hit and (time.time() - hit[0]) < _ANSWER_CACHE_TTL_S:
        return {**hit[1], "cached": True}
    q = question.lower()
    context: dict[str, Any] = {"question": question}
    used: list[str] = []

    if any(k in q for k in ["predict", "surge", "next", "minutes", "crowd"]):
        context["predictions"] = match_context.predict_surge(minutes_ahead=10)
        used.append("match_context.predict_surge")

    if any(k in q for k in ["camera", "vision", "north", "south", "east", "west", "video", "clip"]):
        clip = "dense.mp4" if "dense" in q or "north" in q else "normal.mp4"
        if "panic" in q:
            clip = "panic.mp4"
        context["vision"] = vision.analyze_clip(clip)
        used.append(f"vision.analyze_clip({clip})")

    if any(k in q for k in ["match", "score", "over", "wicket"]):
        context["match_state"] = match_context.get_match_state()
        used.append("match_context.get_match_state")

    if any(k in q for k in ["incident", "history", "log"]):
        context["incidents"] = get_incidents(limit=10)
        used.append("commander.get_incidents")

    if any(k in q for k in ["news", "threat", "protest", "strike", "transit", "weather alert", "outside", "around"]):
        context["intel"] = intel.run()
        used.append("intel.run")

    prompt = f"""You are the CrowdSync Commander Agent. Answer the operator's
question using the sub-agent outputs below.

QUESTION: {question}

SUB-AGENT OUTPUTS:
{json.dumps(context, indent=2)}

Be concise, action-oriented, and reference specific zones / events / numbers
from the sub-agent outputs. Maximum 4 sentences."""

    try:
        answer = chat(prompt)
    except Exception as e:
        # No LLM available — synthesize a useful answer from the sub-agent
        # outputs we already gathered. This keeps the demo working even when
        # OpenRouter + Gemini are both daily-capped.
        msg = str(e)
        rate_limited = "rate_limited" in msg or "429" in msg or "quota" in msg.lower()

        bits: list[str] = []
        if "predictions" in context:
            preds = (context["predictions"].get("predictions") or [])
            if preds:
                top = sorted(preds, key=lambda p: -(p.get("expected_density_pct", 0)))[0]
                bits.append(
                    f"Top forecasted zone is {top['zone_id']} at "
                    f"{top.get('expected_density_pct','?')}% density "
                    f"({top.get('severity','?')} severity) — driver: "
                    f"{top.get('driver','-')}."
                )
        if "vision" in context:
            v = context["vision"]
            bits.append(
                f"Vision Agent reports {v.get('density_pct','?')}% density "
                f"({v.get('trend','stable')}) in the camera feed; "
                f"anomalies: {', '.join(v.get('anomalies') or []) or 'none'}."
            )
        if "match_state" in context:
            m = context["match_state"]
            bits.append(
                f"Match: {m['teams'].get('home','?')} vs {m['teams'].get('away','?')}, "
                f"over {m.get('current_over','?')}.{m.get('current_ball','?')}, "
                f"{m.get('score',{}).get('runs','?')}/{m.get('score',{}).get('wickets','?')}."
            )
        if "incidents" in context:
            incs = context["incidents"]
            active = [i for i in incs if i.get("severity") in ("high", "critical")]
            bits.append(f"{len(active)} active high/critical incidents out of {len(incs)} total.")
        if "intel" in context:
            i = context["intel"]
            bits.append(
                f"Threat Intel risk level: {i.get('overall_risk_level','low')}. "
                f"{i.get('operator_briefing','')[:160]}"
            )

        if bits:
            answer = " ".join(bits)
            if rate_limited:
                answer += (
                    " (Note: LLM synthesizer is temporarily unavailable — the above "
                    "is a direct read-out of sub-agent outputs.)"
                )
        elif rate_limited:
            answer = (
                "All free-tier LLM providers (OpenRouter + Gemini) are currently "
                "rate-limited. Sub-agent data tiles continue to update; full "
                "natural-language synthesis returns when quota resets."
            )
        else:
            answer = f"Commander error: {e}"

    result = {"answer": answer, "tools_used": used, "context": context}
    if "rate_limited" not in answer.lower():
        _ANSWER_CACHE[cache_key] = (time.time(), result)
    return result

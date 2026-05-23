"""Fan Concierge Agent.

Two-way fan communication via AgentMail. Sends personalized routing nudges
based on predicted surges, reads fan replies, classifies them, and routes
incidents back to the Commander.

This is CrowdSync's demand-side load balancer: instead of only telling
operators about bottlenecks, it nudges affected fans BEFORE the bottleneck
forms."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from tools.agentmail_client import IncomingMessage, get_client
from tools.gemini_client import chat_json
from tools.virustotal_client import ScanVerdict, scan_message_text
from tools import supabase_client

load_dotenv()
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

FAN_CONCIERGE_CLIENT_ID = os.getenv("FAN_CONCIERGE_CLIENT_ID", "crowdsync-fan-concierge-v1")
FAN_CONCIERGE_INBOX_ADDR = os.getenv("FAN_CONCIERGE_INBOX_ADDR") or None


def _load_tickets() -> list[dict]:
    """Prefer Supabase tickets table; fall back to JSON when DB is unavailable."""
    if supabase_client.is_enabled():
        rows = supabase_client.list_tickets()
        if rows:
            return rows
    return json.loads((DATA_DIR / "tickets.json").read_text())["tickets"]


def _demo_recipients() -> list[str]:
    raw = os.getenv("DEMO_FAN_EMAILS", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


def _fans_in_zone(zone_id: str) -> list[dict]:
    """Return fans in zone, with demo (real-email) fans first so they get sent first."""
    in_zone = [t for t in _load_tickets() if t["zone"] == zone_id]
    if not in_zone:
        # Fall back to demo fans regardless of zone so the live email demo still fires
        in_zone = [t for t in _load_tickets() if t.get("is_demo")]
    in_zone.sort(key=lambda t: (0 if t.get("is_demo") else 1, t["ticket_id"]))
    return in_zone


def _draft_nudge(fan: dict, surge: dict) -> dict:
    """Use Gemini to write a personalized routing nudge for a single fan."""
    prompt = f"""Write a short routing nudge email for a cricket fan.

FAN:
- Name: {fan['name']}
- Seat: {fan['seat']}
- Zone: {fan['zone']}
- Assigned gate: {fan['gate_assigned']}

PREDICTED ISSUE:
- Zone {surge['zone_id']} expected at {surge.get('expected_density_pct', '?')}% density
- Driver: {surge.get('driver', 'crowd surge')}
- Peak in ~{surge.get('minutes_until_peak', '?')} min

GOAL: nudge the fan toward an alternate route/gate or off-peak timing.
Keep it under 60 words, friendly, action-oriented. Give ONE clear instruction
plus the estimated time saved. No emoji.

Return JSON: {{"subject": "...", "text": "..."}}"""
    try:
        return chat_json(prompt)
    except Exception as e:
        return {
            "subject": f"Quick tip for {fan['name'].split()[0]}",
            "text": (
                f"Hi {fan['name'].split()[0]}, heads-up: gate {fan['gate_assigned']} "
                f"is about to get busy. Consider exiting via the East gate to save "
                f"~10 minutes. (fallback message; agent: {e})"
            ),
        }


def send_nudges_for_surge(surge: dict, max_recipients: int = 3) -> list[dict]:
    """Send personalized routing nudges to fans affected by a predicted surge.

    For the demo, we only actually email the real AgentMail recipient inboxes;
    other fans get logged but not sent. Returns list of {fan, status, message_id}."""
    client = get_client()
    inbox_id = client.get_or_create_inbox(FAN_CONCIERGE_CLIENT_ID, prefer_address=FAN_CONCIERGE_INBOX_ADDR)

    zone = surge.get("zone_id")
    if not zone:
        return []

    fans = _fans_in_zone(zone)
    demo_emails = set(_demo_recipients())

    sent: list[dict] = []
    real_count = 0
    for fan in fans:
        is_real = fan["email"] in demo_emails
        if not is_real and real_count >= max_recipients:
            sent.append({"fan": fan, "status": "logged_only"})
            continue

        nudge = _draft_nudge(fan, surge)
        if is_real:
            msg_id = client.send(
                inbox_id=inbox_id,
                to=fan["email"],
                subject=nudge["subject"],
                text=nudge["text"],
                labels=["crowdsync", "routing_nudge", zone],
            )
            sent.append({
                "fan": fan,
                "status": "sent" if msg_id else "send_failed",
                "message_id": msg_id,
                "subject": nudge["subject"],
                "text": nudge["text"],
            })
            # Audit outbound
            if supabase_client.is_enabled():
                supabase_client.log_fan_message(
                    direction="outbound",
                    message_id=msg_id,
                    from_addr=os.getenv("FAN_CONCIERGE_INBOX_ADDR", ""),
                    to_addr=fan["email"],
                    subject=nudge["subject"],
                    body_preview=nudge["text"][:300],
                    category="ROUTING_NUDGE",
                )
                supabase_client.log_agent_decision(
                    agent_name="fan_concierge",
                    action="send_nudge",
                    reasoning=f"{fan['ticket_id']} ({fan['email']}) re: {zone} surge",
                    payload={"zone": zone, "ticket_id": fan["ticket_id"], "message_id": msg_id},
                )
            real_count += 1
        else:
            sent.append({
                "fan": fan,
                "status": "logged_only",
                "subject": nudge["subject"],
                "text": nudge["text"],
            })
    return sent


def classify_fan_reply(message: IncomingMessage) -> dict:
    """Classify an incoming fan email into an incident category."""
    prompt = f"""A fan at a cricket match sent this email to stadium support.
Classify the incident.

SUBJECT: {message.subject}
BODY: {message.body}

Categories: LOST_CHILD, MEDICAL, CROWD_SURGE, COMPLAINT, INFO_REQUEST, OTHER.
Also extract any location mentioned (gate, stand, seat) into 'location'.
Severity: low | medium | high | critical.

Return JSON: {{"category": "...", "severity": "...", "location": "...", "summary": "<one sentence>", "needs_human": true/false}}"""
    try:
        return chat_json(prompt)
    except Exception as e:
        return {
            "category": "OTHER",
            "severity": "low",
            "location": "",
            "summary": f"classification failed: {e}",
            "needs_human": True,
        }


def _security_scan(msg: IncomingMessage) -> dict:
    """Run VirusTotal on URLs in the message body + subject. Returns a summary."""
    text = f"{msg.subject}\n\n{msg.body}"
    verdicts: list[ScanVerdict] = scan_message_text(text)
    threats = [v for v in verdicts if v.is_threat]
    return {
        "scanned_urls": len(verdicts),
        "threats_found": len(threats),
        "is_quarantined": len(threats) > 0,
        "max_severity": max((v.severity for v in verdicts), default="low"),
        "verdicts": [
            {
                "target": v.target,
                "malicious": v.malicious,
                "suspicious": v.suspicious,
                "harmless": v.harmless,
                "categories": v.categories,
                "source": v.source,
                "severity": v.severity,
                "is_threat": v.is_threat,
            }
            for v in verdicts
        ],
    }


def poll_replies(inbox_id: Optional[str] = None) -> list[dict]:
    """Poll the Fan Concierge inbox for unread replies.

    For each message:
    1. Run VirusTotal scan on any URLs (security gate before Gemini sees content).
    2. If quarantined, return as SECURITY_THREAT — skip Gemini classification.
    3. Otherwise classify with Gemini.
    4. Mark message read.
    """
    client = get_client()
    if inbox_id is None:
        inbox_id = client.get_or_create_inbox(FAN_CONCIERGE_CLIENT_ID, prefer_address=FAN_CONCIERGE_INBOX_ADDR)

    incoming = client.list_unread(inbox_id, limit=10)
    processed: list[dict] = []
    for msg in incoming:
        security = _security_scan(msg)
        if security["is_quarantined"]:
            classification = {
                "category": "SECURITY_THREAT",
                "severity": security["max_severity"],
                "location": "",
                "summary": (
                    f"Quarantined: {security['threats_found']} malicious URL(s) detected by "
                    f"VirusTotal in fan email. Content NOT forwarded to LLM."
                ),
                "needs_human": True,
            }
        else:
            classification = classify_fan_reply(msg)

        processed.append({
            "message_id": msg.message_id,
            "from": msg.sender,
            "subject": msg.subject,
            "body": msg.body if not security["is_quarantined"] else "[REDACTED — quarantined by security scan]",
            "classification": classification,
            "security": security,
        })
        # Audit log (Supabase best-effort)
        if supabase_client.is_enabled():
            supabase_client.log_fan_message(
                direction="inbound",
                message_id=msg.message_id,
                from_addr=msg.sender,
                subject=msg.subject,
                body_preview=msg.body[:300],
                category=classification.get("category"),
                severity=classification.get("severity"),
                security_verdict=security,
            )
        client.mark_read(inbox_id, msg.message_id)
    return processed


def acknowledge_fan(message_id: str, classification: dict, inbox_id: Optional[str] = None) -> Optional[str]:
    """Send a calm acknowledgment reply to a fan whose email we just processed."""
    client = get_client()
    if inbox_id is None:
        inbox_id = client.get_or_create_inbox(FAN_CONCIERGE_CLIENT_ID, prefer_address=FAN_CONCIERGE_INBOX_ADDR)

    category = classification.get("category", "OTHER")
    if category == "LOST_CHILD":
        text = (
            "We have received your report and have alerted stadium security. "
            "Please stay where you are if safe; a steward will reach you shortly. "
            "The lost-child help desk is located at Gate 4 (main entrance)."
        )
    elif category == "MEDICAL":
        text = (
            "Medical team has been dispatched to your location. "
            "Please stay calm and keep the area around the person clear if possible."
        )
    elif category == "CROWD_SURGE":
        text = (
            "Thanks for the heads-up — we are routing additional resources. "
            "If you feel unsafe, the nearest emergency exit lights will guide you out."
        )
    else:
        text = "Thanks for reaching out — your message has been logged with stadium operations."

    return client.reply(inbox_id=inbox_id, message_id=message_id, text=text)


def get_inbox_id() -> str:
    return get_client().get_or_create_inbox(FAN_CONCIERGE_CLIENT_ID)

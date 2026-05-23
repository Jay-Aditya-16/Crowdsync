"""Supabase / Postgres client.

CrowdSync uses Supabase for persistent state — incidents, audit trail,
tickets registry, fan message log. We connect via direct Postgres
(psycopg) for both DDL (auto-migration on first start) and CRUD, which
sidesteps RLS complexity for the hackathon demo while still benefiting
from Supabase's managed Postgres, realtime, and dashboard.

On any error (network down, schema cache stale, table missing), CRUD
helpers degrade gracefully — callers see a None / empty result and the
JSON file fallbacks in agents/commander.py + agents/fan_concierge.py
take over so the demo never crashes.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

import psycopg
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = ROOT / "migrations" / "001_init.sql"

_lock = threading.Lock()
_conn: Optional[psycopg.Connection] = None
_migration_applied = False


def _db_url() -> str:
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        raise RuntimeError("SUPABASE_DB_URL not set")
    return url


def is_enabled() -> bool:
    return bool(os.getenv("SUPABASE_DB_URL"))


def _get_conn() -> psycopg.Connection:
    global _conn, _migration_applied
    with _lock:
        if _conn is None or _conn.closed:
            _conn = psycopg.connect(_db_url(), autocommit=True, connect_timeout=10)
        if not _migration_applied:
            try:
                with _conn.cursor() as cur:
                    cur.execute(MIGRATION_PATH.read_text())
                _migration_applied = True
            except Exception as e:
                print(f"[supabase] migration error (continuing): {e}")
        return _conn


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

def insert_incident(incident: dict) -> Optional[str]:
    """Persist an incident. Returns the new UUID or None on failure."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incidents (legacy_id, type, severity, zone, summary, plan, source, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id::text
                """,
                (
                    incident.get("id"),
                    incident.get("type", "UNKNOWN"),
                    incident.get("severity", "low"),
                    incident.get("zone"),
                    incident.get("summary"),
                    incident.get("plan"),
                    incident.get("source"),
                    json.dumps(incident, default=str),
                ),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"[supabase] insert_incident error: {e}")
        return None


def fetch_recent_incidents(limit: int = 50) -> list[dict]:
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text, legacy_id, type, severity, zone, summary, plan, source, payload, created_at "
                "FROM incidents ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]
        out: list[dict] = []
        for r in rows:
            d = dict(zip(cols, r))
            if d.get("payload"):
                full = d["payload"] if isinstance(d["payload"], dict) else json.loads(d["payload"])
                full.update({k: v for k, v in d.items() if v is not None and k != "payload"})
                d = full
            d["timestamp"] = d.get("created_at").isoformat() + "Z" if d.get("created_at") else ""
            out.append(d)
        return out
    except Exception as e:
        print(f"[supabase] fetch_recent_incidents error: {e}")
        return []


def clear_incidents() -> bool:
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM incidents")
        return True
    except Exception as e:
        print(f"[supabase] clear_incidents error: {e}")
        return False


# ---------------------------------------------------------------------------
# Agent decisions audit log
# ---------------------------------------------------------------------------

def log_agent_decision(agent_name: str, action: str, reasoning: str = "",
                       confidence: Optional[float] = None, payload: Optional[dict] = None) -> Optional[str]:
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_decisions (agent_name, action, reasoning, confidence, payload)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                RETURNING id::text
                """,
                (agent_name, action, reasoning, confidence, json.dumps(payload or {}, default=str)),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"[supabase] log_agent_decision error: {e}")
        return None


def fetch_recent_decisions(limit: int = 20) -> list[dict]:
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text, agent_name, action, reasoning, confidence, payload, created_at "
                "FROM agent_decisions ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        print(f"[supabase] fetch_recent_decisions error: {e}")
        return []


def fetch_active_operators(window_minutes: int = 60) -> list[dict]:
    """Operators who signed in within the last N minutes. Pulled from the audit trail."""
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (payload->>'email')
                  payload->>'name'  AS name,
                  payload->>'email' AS email,
                  payload->>'role'  AS role,
                  created_at
                FROM agent_decisions
                WHERE agent_name = 'auth'
                  AND action = 'session_start'
                  AND created_at > NOW() - (%s || ' minutes')::interval
                ORDER BY payload->>'email', created_at DESC
                """,
                (str(window_minutes),),
            )
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        print(f"[supabase] fetch_active_operators error: {e}")
        return []


# ---------------------------------------------------------------------------
# Incident similarity search.
# pgvector is overkill for short summaries during a hackathon; we use
# difflib's ratio() which is deterministic, dependency-free, and works
# well on short cricket-incident text. The migration leaves room for a
# `vector` column in production.
# ---------------------------------------------------------------------------

def find_similar_incidents(summary: str, k: int = 3, exclude_legacy_id: Optional[str] = None,
                            min_ratio: float = 0.30, scan: int = 100) -> list[dict]:
    """Return the K most similar prior incidents to `summary`."""
    if not summary:
        return []
    try:
        from difflib import SequenceMatcher
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text, legacy_id, type, severity, zone, summary, created_at "
                "FROM incidents ORDER BY created_at DESC LIMIT %s",
                (scan,),
            )
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        print(f"[supabase] find_similar_incidents error: {e}")
        return []

    needle = summary.lower()
    scored: list[dict] = []
    for r in rows:
        if exclude_legacy_id and r.get("legacy_id") == exclude_legacy_id:
            continue
        s = (r.get("summary") or "").lower()
        if not s:
            continue
        ratio = SequenceMatcher(None, needle, s).ratio()
        if ratio < min_ratio:
            continue
        scored.append({**r, "similarity": round(ratio, 3)})
    scored.sort(key=lambda x: -x["similarity"])
    return scored[:k]


# ---------------------------------------------------------------------------
# Demo seed — historical incidents so similarity search has something to find.
# Idempotent: only inserts if the table is empty.
# ---------------------------------------------------------------------------

DEMO_HISTORICAL = [
    {"type": "CROWD_SURGE", "severity": "high", "zone": "A_STAND",
     "summary": "Heavy congestion at exit Gate 7 after wicket fall in 18th over; restroom queue extended 12 min.",
     "plan": "Open auxiliary Gate G6; redirect via West concourse.", "source": "historical_demo"},
    {"type": "LOST_CHILD", "severity": "high", "zone": "C_LOWER",
     "summary": "Child reported missing near Gate G17 after innings break surge; reunited via help-desk.",
     "plan": "Page security lead; PA announce; lock soft exits.", "source": "historical_demo"},
    {"type": "WEATHER_RAIN", "severity": "medium", "zone": "N_STAND",
     "summary": "Sudden drizzle drove uncovered fans toward Pavilion; 18% surge in covered areas.",
     "plan": "Email nudges to uncovered-stand ticket holders; deploy umbrellas at Gate G14.",
     "source": "historical_demo"},
    {"type": "MEDICAL", "severity": "high", "zone": "G_UPPER",
     "summary": "Spectator collapsed in row 14 of G Upper Stand; medics dispatched via Gate G18.",
     "plan": "Clear nearest corridor; first-aid post notified.", "source": "historical_demo"},
    {"type": "CROWD_SURGE", "severity": "medium", "zone": "P2_STAND",
     "summary": "Match-end exit surge concentrated at Gate G5; evacuation reached 22 min P95.",
     "plan": "Pre-position staff at G6; stagger PA announcements 30s apart.",
     "source": "historical_demo"},
    {"type": "SECURITY_THREAT", "severity": "high", "zone": "G_LOWER",
     "summary": "Phishing email impersonating ticketing with malicious link; quarantined by VirusTotal scan.",
     "plan": "Block sender pattern; alert security ops.", "source": "historical_demo"},
]


def seed_historical_incidents_if_empty() -> int:
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM incidents WHERE source = 'historical_demo'")
            already = cur.fetchone()[0]
        if already > 0:
            return 0
        n = 0
        for inc in DEMO_HISTORICAL:
            if insert_incident(inc):
                n += 1
        return n
    except Exception as e:
        print(f"[supabase] seed_historical_incidents error: {e}")
        return 0


# ---------------------------------------------------------------------------
# Tickets registry
# ---------------------------------------------------------------------------

def upsert_ticket(ticket: dict) -> bool:
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tickets (ticket_id, name, email, zone, seat, gate_assigned, language, is_demo)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticket_id) DO UPDATE SET
                  name=EXCLUDED.name, email=EXCLUDED.email, zone=EXCLUDED.zone,
                  seat=EXCLUDED.seat, gate_assigned=EXCLUDED.gate_assigned,
                  language=EXCLUDED.language, is_demo=EXCLUDED.is_demo
                """,
                (
                    ticket["ticket_id"], ticket.get("name"), ticket.get("email"),
                    ticket.get("zone"), ticket.get("seat"), ticket.get("gate_assigned"),
                    ticket.get("language", "en"), bool(ticket.get("is_demo", False)),
                ),
            )
        return True
    except Exception as e:
        print(f"[supabase] upsert_ticket error: {e}")
        return False


def list_tickets(zone: Optional[str] = None) -> list[dict]:
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            if zone:
                cur.execute("SELECT * FROM tickets WHERE zone = %s", (zone,))
            else:
                cur.execute("SELECT * FROM tickets")
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        print(f"[supabase] list_tickets error: {e}")
        return []


# ---------------------------------------------------------------------------
# Fan message log
# ---------------------------------------------------------------------------

def log_fan_message(
    direction: str,
    message_id: Optional[str] = None,
    from_addr: str = "",
    to_addr: str = "",
    subject: str = "",
    body_preview: str = "",
    category: Optional[str] = None,
    severity: Optional[str] = None,
    security_verdict: Optional[dict] = None,
) -> Optional[str]:
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fan_messages_log
                  (message_id, direction, from_addr, to_addr, subject, body_preview, category, severity, security_verdict)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING id::text
                """,
                (message_id, direction, from_addr, to_addr, subject, body_preview[:500],
                 category, severity, json.dumps(security_verdict or {}, default=str)),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"[supabase] log_fan_message error: {e}")
        return None


def seed_tickets_from_json() -> int:
    """One-shot: copy tickets from tickets.json into the Supabase tickets table."""
    path = ROOT / "data" / "tickets.json"
    if not path.exists():
        return 0
    tickets = json.loads(path.read_text()).get("tickets", [])
    n = 0
    for t in tickets:
        if upsert_ticket(t):
            n += 1
    return n


def health() -> dict:
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT now()")
            now = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM incidents")
            inc_count = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM tickets")
            tk_count = cur.fetchone()[0]
        return {
            "ok": True,
            "server_time": now.isoformat() if hasattr(now, "isoformat") else str(now),
            "incidents": inc_count,
            "tickets": tk_count,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

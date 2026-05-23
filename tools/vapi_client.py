"""Vapi AI voice client.

Three integration modes:

1. **Web voice mode** — embeds the Vapi Web SDK in the dashboard so the
   operator can TALK to the Commander Agent through their browser
   microphone. Uses a pre-created assistant_id (server-side config).

2. **Live context injection** — periodically PATCHes the assistant's
   system prompt with current dashboard state (incidents, predictions,
   threat-intel risk). This lets the voice agent answer "what's the top
   risk?" with REAL current numbers without needing webhook tools.

3. **Outbound phone call** — uses the private key + REST API to place a
   real phone call when a critical incident fires. Needs a configured
   Vapi phone number (VAPI_PHONE_NUMBER_ID).

The private key NEVER reaches the browser — only used server-side. The
public key is the one embedded in the page.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

VAPI_BASE = "https://api.vapi.ai"
_REQUEST_TIMEOUT = 20
_ASSISTANT_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "vapi_assistant.json"


def public_key() -> Optional[str]:
    return os.getenv("VAPI_PUBLIC_KEY")


def _private_key() -> str:
    k = os.getenv("VAPI_PRIVATE_KEY")
    if not k:
        raise RuntimeError("VAPI_PRIVATE_KEY not set")
    return k


def _headers() -> dict:
    return {"Authorization": f"Bearer {_private_key()}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# System prompt — the Commander Agent persona for voice.
# Updated dynamically with live dashboard state via update_assistant_context.
# ---------------------------------------------------------------------------

BASE_SYSTEM_PROMPT = """You are the Commander Agent voice interface for CrowdSync,
a multi-agent stadium command platform deployed at M. Chinnaswamy Stadium in
Bengaluru during the RCB vs CSK match.

You have read-only awareness of:
- 19 stadium zones (A_STAND through P_CORPORATE)
- 18 gates around the perimeter (G1-G21, mapped to Cubbon/Queen's/Link/MG roads)
- 6 sub-agents: Match Context, Vision, Fan Concierge, Threat Intel, What-If
  Simulator, Red Cell

Voice answer rules:
- Keep responses under 25 seconds spoken (about 50 words).
- Always reference specific zones, gates, or numbers from the state below.
- Give ONE clear recommended action per answer.
- Never apologize for being an AI — you ARE the Commander Agent.
- If asked something you don't have data on, say so briefly and pivot to
  what you DO know.

Tone: calm, operational, gravelly. Sample: "Roger. North Stand at 82%
density, rising — wicket fall triggered restroom surge. Recommend opening
auxiliary Gate G14 now."
"""


def _build_system_prompt(live_state: Optional[dict] = None) -> str:
    parts = [BASE_SYSTEM_PROMPT]
    if live_state:
        parts.append("\n--- LIVE DASHBOARD STATE (snapshot, refresh in ~2 min) ---\n")
        parts.append(json.dumps(live_state, indent=2, default=str)[:3500])
    return "".join(parts)


def _assistant_body(live_state: Optional[dict] = None, name: str = "CrowdSync Commander") -> dict:
    return {
        "name": name,
        "firstMessage": "Commander online. Sit-rep ready when you are.",
        "model": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "messages": [{"role": "system", "content": _build_system_prompt(live_state)}],
            "temperature": 0.4,
        },
        "voice": {
            "provider": "11labs",
            "voiceId": "burt",
        },
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-2",
        },
        "endCallMessage": "Stay safe. Commander out.",
        "silenceTimeoutSeconds": 30,
    }


# ---------------------------------------------------------------------------
# Assistant lifecycle
# ---------------------------------------------------------------------------

def _read_cached_assistant() -> Optional[str]:
    if not _ASSISTANT_CACHE_PATH.exists():
        return None
    try:
        return json.loads(_ASSISTANT_CACHE_PATH.read_text()).get("id")
    except Exception:
        return None


def _write_cached_assistant(assistant_id: str) -> None:
    _ASSISTANT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ASSISTANT_CACHE_PATH.write_text(json.dumps({"id": assistant_id}, indent=2))


def _verify_assistant_exists(assistant_id: str) -> bool:
    try:
        r = requests.get(f"{VAPI_BASE}/assistant/{assistant_id}", headers=_headers(), timeout=_REQUEST_TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False


def get_or_create_assistant(live_state: Optional[dict] = None) -> Optional[str]:
    """Return a working assistant_id. Creates one if cached id is missing or invalid."""
    cached = _read_cached_assistant()
    if cached and _verify_assistant_exists(cached):
        return cached
    try:
        r = requests.post(
            f"{VAPI_BASE}/assistant",
            headers=_headers(),
            json=_assistant_body(live_state),
            timeout=_REQUEST_TIMEOUT,
        )
        if r.status_code not in (200, 201):
            print(f"[vapi] create assistant failed {r.status_code}: {r.text[:300]}")
            return None
        aid = r.json().get("id")
        if aid:
            _write_cached_assistant(aid)
        return aid
    except Exception as e:
        print(f"[vapi] create assistant exception: {e}")
        return None


def update_assistant_context(live_state: dict, assistant_id: Optional[str] = None) -> bool:
    """PATCH the assistant's system prompt so future calls reflect current dashboard state."""
    aid = assistant_id or get_or_create_assistant(live_state)
    if not aid:
        return False
    try:
        body = {
            "model": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "messages": [{"role": "system", "content": _build_system_prompt(live_state)}],
                "temperature": 0.4,
            }
        }
        r = requests.patch(f"{VAPI_BASE}/assistant/{aid}", headers=_headers(), json=body, timeout=_REQUEST_TIMEOUT)
        return r.status_code in (200, 201, 204)
    except Exception as e:
        print(f"[vapi] update_assistant_context error: {e}")
        return False


# ---------------------------------------------------------------------------
# Outbound phone calls
# ---------------------------------------------------------------------------

def place_outbound_alert(
    to_number: str,
    incident_summary: str,
    operator_name: str = "operator",
) -> dict:
    """Place an outbound phone call to alert someone about a critical incident."""
    phone_number_id = os.getenv("VAPI_PHONE_NUMBER_ID")
    if not phone_number_id:
        return {
            "ok": False,
            "error": "VAPI_PHONE_NUMBER_ID not set — purchase a Vapi number first.",
            "docs": "https://docs.vapi.ai/phone-numbers",
        }

    body = {
        "phoneNumberId": phone_number_id,
        "customer": {"number": to_number},
        "assistant": {
            **_assistant_body(),
            "firstMessage": (
                f"This is CrowdSync Commander with an urgent alert for "
                f"{operator_name}. {incident_summary} Please acknowledge by "
                f"saying 'received'."
            ),
        },
    }
    try:
        r = requests.post(f"{VAPI_BASE}/call/phone", headers=_headers(), json=body, timeout=_REQUEST_TIMEOUT)
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"vapi {r.status_code}: {r.text[:300]}"}
        return {"ok": True, "call_id": r.json().get("id"), "data": r.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Web widget — embedded in Streamlit via components.html
# ---------------------------------------------------------------------------

def web_widget_html(assistant_id: Optional[str] = None, operator_name: str = "Operator") -> str:
    """Self-contained HTML that mounts the Vapi web widget.

    Calls `vapi.start(assistant_id)` so the assistant config lives server-side
    (kept in sync with live dashboard state via update_assistant_context).
    """
    pk = public_key() or ""
    aid = assistant_id or _read_cached_assistant() or ""
    safe_name = (operator_name or "Operator").replace("'", "\\'")
    return f"""
    <style>
      .vapi-host {{
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        background: linear-gradient(135deg, #1B2838, #0F1B2A);
        border-radius: 8px; padding: 18px; color: white; text-align: center;
      }}
      .vapi-mic-btn {{
        background: linear-gradient(135deg, #5BD96B, #3DC7AE);
        color: white; border: none; border-radius: 50%;
        width: 88px; height: 88px; font-size: 36px; cursor: pointer;
        box-shadow: 0 4px 16px rgba(91, 217, 107, 0.4);
        transition: transform 0.15s;
      }}
      .vapi-mic-btn:hover {{ transform: scale(1.05); }}
      .vapi-mic-btn.live {{
        background: linear-gradient(135deg, #E94B4B, #9B1C1C);
        box-shadow: 0 4px 16px rgba(233, 75, 75, 0.5);
        animation: vapi-pulse 1.5s infinite;
      }}
      @keyframes vapi-pulse {{
        0%, 100% {{ box-shadow: 0 4px 16px rgba(233, 75, 75, 0.5); }}
        50% {{ box-shadow: 0 4px 32px rgba(233, 75, 75, 0.9); }}
      }}
      .vapi-status {{ margin-top: 12px; color: #9BB0C4; font-size: 13px; min-height: 18px; }}
      .vapi-transcript {{
        margin-top: 12px; padding: 10px; background: rgba(0,0,0,0.3);
        border-radius: 6px; font-size: 12px; text-align: left;
        max-height: 140px; overflow-y: auto; min-height: 60px;
      }}
      .vapi-transcript span.user {{ color: #9BB0C4; }}
      .vapi-transcript span.assistant {{ color: #5BD96B; }}
      .vapi-aid {{ color: #555; font-size: 10px; margin-top: 6px; font-family: monospace; }}
    </style>
    <div class="vapi-host">
      <div style="font-weight:600; margin-bottom:6px;">🎙️ Talk to Commander</div>
      <div style="font-size:12px; color:#9BB0C4; margin-bottom:14px;">
        Click to start a voice conversation. Mic permission required on first use.
      </div>
      <button id="vapi-mic-btn" class="vapi-mic-btn" onclick="vapiToggle()">🎤</button>
      <div class="vapi-status" id="vapi-status">Idle</div>
      <div class="vapi-transcript" id="vapi-transcript"><i style="color:#666;">Transcript appears here…</i></div>
      <div class="vapi-aid">assistant: { aid[:8] if aid else 'NOT CREATED'}…</div>
    </div>
    <script type="module">
      import Vapi from 'https://esm.sh/@vapi-ai/web@2.3.13';
      const PUBLIC_KEY = '{pk}';
      const ASSISTANT_ID = '{aid}';
      const OPERATOR_NAME = '{safe_name}';
      const vapi = new Vapi(PUBLIC_KEY);

      let isLive = false;
      const btn = document.getElementById('vapi-mic-btn');
      const status = document.getElementById('vapi-status');
      const transcript = document.getElementById('vapi-transcript');

      vapi.on('call-start', () => {{
        isLive = true;
        btn.classList.add('live'); btn.textContent = '⏹';
        status.textContent = 'Live call active. Speak naturally.';
        transcript.innerHTML = '';
      }});
      vapi.on('call-end', () => {{
        isLive = false;
        btn.classList.remove('live'); btn.textContent = '🎤';
        status.textContent = 'Call ended.';
      }});
      vapi.on('speech-start', () => {{ status.textContent = 'Commander speaking…'; }});
      vapi.on('speech-end', () => {{ status.textContent = 'Listening…'; }});
      vapi.on('message', (msg) => {{
        if (msg.type === 'transcript' && msg.transcript) {{
          const line = document.createElement('div');
          const role = msg.role === 'user' ? 'user' : 'assistant';
          const tag = msg.role === 'user' ? '👤' : '🎯';
          line.innerHTML = `<span class="${{role}}">${{tag}} ${{msg.transcript}}</span>`;
          transcript.appendChild(line);
          transcript.scrollTop = transcript.scrollHeight;
        }}
      }});
      vapi.on('error', (e) => {{
        status.textContent = 'Error: ' + ((e?.errorMsg || e?.message || JSON.stringify(e))+'').slice(0, 140);
      }});

      window.vapiToggle = async function() {{
        if (!ASSISTANT_ID) {{
          status.textContent = 'No assistant_id — server failed to create. Check VAPI_PRIVATE_KEY.';
          return;
        }}
        if (isLive) {{
          vapi.stop();
        }} else {{
          status.textContent = 'Connecting…';
          try {{
            await vapi.start(ASSISTANT_ID);
          }} catch(e) {{
            status.textContent = 'Failed to start: ' + ((e?.message || e)+'').slice(0, 140);
          }}
        }}
      }};
    </script>
    """


def build_live_context(commander_module, intel_module, match_context_module,
                        whatif_module, red_cell_module) -> dict:
    """Snapshot the most useful current dashboard state for the voice agent."""
    state: dict = {}
    try:
        state["match"] = match_context_module.get_match_state()
    except Exception as e:
        state["match_error"] = str(e)[:120]
    try:
        incidents = commander_module.get_incidents(limit=8)
        state["incidents"] = [
            {"type": i.get("type"), "severity": i.get("severity"),
             "zone": i.get("zone"), "summary": (i.get("summary") or "")[:160]}
            for i in incidents[:8]
        ]
    except Exception as e:
        state["incidents_error"] = str(e)[:120]
    return state

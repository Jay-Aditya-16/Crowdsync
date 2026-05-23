"""CrowdSync — live ops dashboard for stadium command.

Auto-refreshes every 5s. Each subsystem runs on its own TTL so we don't
torch the Gemini / Firecrawl free-tier quotas:

- 3D digital twin + Monte Carlo:  every 5s   (no API calls)
- Fan inbox poll + auto-classify: every 20s  (AgentMail + small Gemini)
- Predicted surges (Match Context): every 90s (Gemini)
- Vision Agent (camera rotator):   every 30s (cached or cheap)
- Threat Intel sweep:              every 5 min (Firecrawl + Gemini)
- Live scoreboard refresh:         every 2 min (Firecrawl + Gemini)

Operators get a continuously-updated view. Buttons remain for force-refresh
and demo controls.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from agents import (
    browser_agent,
    commander,
    fan_concierge,
    intel,
    match_context,
    red_cell,
    vision,
    whatif_simulator,
)
from tools import privacy, supabase_client, vapi_client
from agents import fan_reports
from ui.login import render_login, render_logout_button
from ui.stadium_3d import build_3d_figure, state_with_perturbation_applied
from ui.stadium_map import build_map_figure
import streamlit.components.v1 as components

DATA_DIR = ROOT / "data"
TICK_MS = 5000  # 5-second wall clock

# ---------- Auth gate ----------
# Must come BEFORE set_page_config call inside render_login, so we don't
# call set_page_config twice. render_login handles its own layout config.
if not render_login():
    st.stop()

st.set_page_config(page_title="Stadnium AI — Stadium Command", layout="wide", page_icon="🏟️")

from ui.theme import inject_css, render_header
inject_css()
render_header(
    operator_name=st.session_state.get("operator_name", "Operator"),
    operator_role=st.session_state.get("operator_role", "Command"),
)

# Ensure Commander knows the current operator email on every rerun (session_state
# survives, module globals may not in some hot-reload paths).
try:
    if st.session_state.get("operator_email"):
        commander.set_operator_email(st.session_state["operator_email"])
except Exception:
    pass

# Continuously re-run the script every TICK_MS. Each subsystem decides
# (via its own TTL) whether to recompute or reuse cached state.
tick = st_autorefresh(interval=TICK_MS, key="livetick")


# ---------------------------------------------------------------------------
# Auto-run helper with staggered scheduling
# ---------------------------------------------------------------------------
#
# The dashboard reruns every 5s. If we let every expensive op fire whenever it
# goes stale, multiple LLM/web calls can collide on a single tick and freeze
# the page render. So we cap "expensive ops per tick" to 1 — the most stale
# one wins, others wait one tick.

if "boot_at" not in st.session_state:
    st.session_state.boot_at = time.time()
    st.session_state.first_render_done = False

_BOOT_GRACE_SECONDS = 2  # don't fire expensive ops in this window — let UI paint first
_expensive_budget = {"count": 0, "max_per_tick": 1}


def maybe_run(key: str, ttl_seconds: float, fn, expensive: bool = True) -> dict | None:
    """Run fn() if last result is older than ttl_seconds.

    Cheap calls (`expensive=False`) always run. Expensive calls are capped at
    one per tick and skipped during the boot grace window. Errors are cached
    so a flaky model doesn't lock the dashboard."""
    last_at = st.session_state.get(f"{key}_at", 0)
    now = time.time()
    age = now - last_at
    if age < ttl_seconds:
        return st.session_state.get(key)

    # Skip during the boot grace window so the first paint is fast.
    if expensive and (now - st.session_state.boot_at) < _BOOT_GRACE_SECONDS:
        return st.session_state.get(key)

    # Budget: only one expensive op per tick.
    if expensive and _expensive_budget["count"] >= _expensive_budget["max_per_tick"]:
        return st.session_state.get(key)

    if expensive:
        _expensive_budget["count"] += 1

    try:
        st.session_state[key] = fn()
        st.session_state[f"{key}_at"] = now
        st.session_state[f"{key}_error"] = None
    except Exception as e:
        st.session_state[f"{key}_error"] = f"{type(e).__name__}: {e}"
        st.session_state[f"{key}_at"] = now  # back off so we don't hammer
    return st.session_state.get(key)


def age_str(key: str) -> str:
    age = int(time.time() - st.session_state.get(f"{key}_at", 0))
    if age < 60:
        return f"{age}s ago"
    return f"{age // 60}m {age % 60}s ago"


# ---------------------------------------------------------------------------
# Init session state
# ---------------------------------------------------------------------------

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "vision_clip_idx" not in st.session_state:
    st.session_state.vision_clip_idx = 0
if "auto_process_mail" not in st.session_state:
    st.session_state.auto_process_mail = True
if "whatif_perturbation" not in st.session_state:
    st.session_state.whatif_perturbation = {"type": "match_end"}
if "browser_session" not in st.session_state:
    st.session_state.browser_session = None
if "last_announced_incident_id" not in st.session_state:
    st.session_state.last_announced_incident_id = None
if "tts_enabled" not in st.session_state:
    st.session_state.tts_enabled = True
if "manual_mode" not in st.session_state:
    st.session_state.manual_mode = False


# ---------------------------------------------------------------------------
# Auto-running subsystems
# ---------------------------------------------------------------------------

# Cheap: Monte Carlo (pure compute, runs every tick — gives the live feel)
risk_from_intel = (st.session_state.get("intel") or {}).get("overall_risk_level", "low") if st.session_state.get("intel") else "low"
whatif_cmp = maybe_run(
    "whatif",
    ttl_seconds=5,
    fn=lambda: whatif_simulator.compare(
        st.session_state.whatif_perturbation,
        risk_level=risk_from_intel,
        trials=150,
    ),
    expensive=False,  # ~50ms pure-Python compute
)

# LLM narration of the current scenario — only refresh when the perturbation
# actually changes (or every 5 min as a hedge). Otherwise narrating every tick
# burns ~12 free-tier requests per minute and starves the Commander chat.
_narration_key = json.dumps(st.session_state.whatif_perturbation, sort_keys=True)
if st.session_state.get("whatif_narration_for") != _narration_key:
    st.session_state["whatif_narration_at"] = 0  # invalidate on perturbation change

def _narrate():
    if not whatif_cmp:
        return None
    n = whatif_simulator.narrate_scenario(whatif_cmp)
    st.session_state["whatif_narration_for"] = _narration_key
    return n

whatif_narration = maybe_run("whatif_narration", ttl_seconds=300, fn=_narrate)

# Cheap: fan inbox poll (AgentMail list is fast; Gemini classify only on new)
def _poll_and_auto_fire():
    replies = fan_concierge.poll_replies()
    if st.session_state.auto_process_mail:
        for r in replies:
            commander.handle_fan_incident(r)
    return replies
new_mail = maybe_run("fanmail", ttl_seconds=20, fn=_poll_and_auto_fire, expensive=False)

# Expensive (LLM calls). In MANUAL MODE we skip all of these to preserve the
# OpenRouter free-tier daily/per-minute budget for interactive chat. Cached
# values from previous runs continue to display.
if st.session_state.manual_mode:
    predictions = st.session_state.get("predictions")
    vision_result = st.session_state.get("vision")
    intel_result = st.session_state.get("intel")
    scoreboard = st.session_state.get("scoreboard")
else:
    # Predicted surges every 5 min (was 90s — too aggressive for free tier)
    predictions = maybe_run(
        "predictions",
        ttl_seconds=300,
        fn=lambda: match_context.predict_surge(minutes_ahead=10),
    )

    def _rotate_vision():
        clips = ["normal.mp4", "dense.mp4", "panic.mp4"]
        idx = st.session_state.vision_clip_idx
        clip = clips[idx % len(clips)]
        st.session_state.vision_clip_idx = idx + 1
        result = vision.analyze_clip(clip)
        commander.handle_vision_anomaly(result, zone_hint="N_STAND" if idx % 3 == 1 else "A_STAND")
        return result
    # Vision rotates every 3 min (was 30s)
    vision_result = maybe_run("vision", ttl_seconds=180, fn=_rotate_vision)

    # Threat Intel every 10 min
    intel_result = maybe_run("intel", ttl_seconds=600, fn=intel.run)

    # Live scoreboard every 5 min
    scoreboard = maybe_run("scoreboard", ttl_seconds=300, fn=match_context.refresh_from_live_scoreboard)

# Cheap: Red Cell adversarial sweep — pure compute, ~600ms for 14 scenarios.
red_cell_result = maybe_run(
    "redcell",
    ttl_seconds=60,
    fn=lambda: red_cell.hunt(risk_level=risk_from_intel, trials_per_scenario=80, top_k=3),
    expensive=False,
)


# ---------------------------------------------------------------------------
# Header — live status bar
# ---------------------------------------------------------------------------

state = match_context.get_match_state()
teams = state.get("teams", {})
incidents_recent = commander.get_incidents(limit=100)
active_incidents = [i for i in incidents_recent if i.get("severity") in ("high", "critical")]

risk_color = {"low": "#5BD96B", "medium": "#F5A623", "high": "#E94B4B", "critical": "#9B1C1C"}.get(
    (intel_result or {}).get("overall_risk_level", "low"), "#5BD96B"
)

st.markdown(
    f"""
    <div style="display:flex; align-items:center; justify-content:space-between; padding:8px 16px; background:linear-gradient(90deg,#0F1B2A,#1A2A40); border-radius:8px; margin-bottom:12px;">
      <div style="display:flex; align-items:center; gap:18px;">
        <div style="font-size:22px; font-weight:700; color:white;">🏟️ CrowdSync</div>
        <div style="color:#9BB0C4;">M. Chinnaswamy Stadium · Bengaluru</div>
        <div style="background:#1F8FA8; color:white; padding:4px 10px; border-radius:4px; font-size:13px;">
          {teams.get('home','-')} vs {teams.get('away','-')} · Over {state.get('current_over','?')}.{state.get('current_ball','?')} · {state.get('score',{}).get('runs','?')}/{state.get('score',{}).get('wickets','?')}
        </div>
      </div>
      <div style="display:flex; align-items:center; gap:12px;">
        <div style="background:{risk_color}; color:white; padding:4px 12px; border-radius:14px; font-weight:600; font-size:13px;">
          RISK: {(intel_result or {}).get('overall_risk_level','low').upper()}
        </div>
        <div style="color:{'#E94B4B' if active_incidents else '#9BB0C4'}; font-weight:600;">
          {len(active_incidents)} active · {len(incidents_recent)} total
        </div>
        <div style="color:#5BD96B; font-size:12px;">● LIVE · tick {tick}</div>
        <div style="color:#9BB0C4; font-size:12px;">👤 {st.session_state.get('operator_name','—')}</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Red Cell vulnerability banner (always visible — alarm color)
# ---------------------------------------------------------------------------

if red_cell_result and red_cell_result.get("top_vulnerabilities"):
    top = red_cell_result["top_vulnerabilities"][0]
    pulse_color = "#E94B4B" if top["delta_p_crush"] >= 0.15 else ("#F5A623" if top["delta_p_crush"] >= 0.05 else "#5BD96B")
    st.markdown(
        f"""
        <div style="background:linear-gradient(90deg,{pulse_color}22,{pulse_color}08); border-left:4px solid {pulse_color}; padding:10px 14px; border-radius:6px; margin-bottom:10px; display:flex; justify-content:space-between; align-items:center;">
          <div style="color:white;">
            <span style="font-weight:700; color:{pulse_color};">🚨 RED CELL · top vulnerability:</span>
            <span style="margin-left:6px;">{red_cell_result.get('headline', '')}</span>
          </div>
          <div style="color:#9BB0C4; font-size:12px;">
            scanned {red_cell_result.get('candidates_evaluated', '?')} scenarios · {age_str('redcell')}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# KPI row — always visible Monte Carlo + evac
# ---------------------------------------------------------------------------

kpi_cols = st.columns(5)
if whatif_cmp:
    scen = whatif_cmp["scenario"]
    base = whatif_cmp["baseline"]
    kpi_cols[0].metric("P(crush)", f"{scen['p_crush']:.0%}", f"{(scen['p_crush']-base['p_crush']):+.0%}", delta_color="inverse")
    kpi_cols[1].metric("Evac p50", f"{scen['evac_minutes']['p50']:.1f} min", f"{(scen['evac_minutes']['p50']-base['evac_minutes']['p50']):+.1f}", delta_color="inverse")
    kpi_cols[2].metric("P(evac > 10m)", f"{scen['p_slow_evac']:.0%}", f"{(scen['p_slow_evac']-base['p_slow_evac']):+.0%}", delta_color="inverse")
    kpi_cols[3].metric("Trials", f"{scen['trials']}", f"risk={scen['risk_level']}")
    kpi_cols[4].metric("Last MC", age_str("whatif"))
else:
    kpi_cols[0].metric("P(crush)", "—")
    kpi_cols[1].metric("Evac p50", "—")
    kpi_cols[2].metric("P(evac > 10m)", "—")
    kpi_cols[3].metric("Trials", "—")
    kpi_cols[4].metric("Last MC", "—")


# ---------------------------------------------------------------------------
# Voice + PA — pinned at the top so it's the first thing the operator sees
# ---------------------------------------------------------------------------

st.divider()
voice_col, broad_col = st.columns([3, 2])

with voice_col:
    st.subheader("🎙️ Voice Assistant")
    st.caption(
        "Talk to the system out loud. It hears you, knows what's happening in the stadium right now, "
        "and answers back in a calm voice. Updates with the latest incidents every few minutes."
    )
    if vapi_client.public_key():
        assistant_id = maybe_run(
            "vapi_assistant",
            ttl_seconds=3600,
            fn=lambda: vapi_client.get_or_create_assistant(
                vapi_client.build_live_context(commander, intel, match_context, whatif_simulator, red_cell)
            ),
            expensive=False,
        )
        def _refresh_vapi_context():
            live = vapi_client.build_live_context(commander, intel, match_context, whatif_simulator, red_cell)
            vapi_client.update_assistant_context(live, assistant_id=assistant_id)
            return {"ok": True, "at": time.time()}
        maybe_run("vapi_context", ttl_seconds=180, fn=_refresh_vapi_context, expensive=False)

        components.html(
            vapi_client.web_widget_html(
                assistant_id=assistant_id,
                operator_name=st.session_state.get("operator_name", "Operator"),
            ),
            height=380,
        )

        with st.expander("📞 Call someone with an urgent alert", expanded=False):
            import os as _os
            phone_id_set = bool(_os.getenv("VAPI_PHONE_NUMBER_ID"))
            if not phone_id_set:
                st.warning(
                    "Outbound calls aren't set up yet. Add a phone number ID to enable real calls."
                )

            critical_incidents = [
                i for i in incidents_recent
                if i.get("severity") in ("high", "critical")
            ][:5]
            default_summary = ""
            if critical_incidents:
                top = critical_incidents[0]
                default_summary = (
                    f"{(top.get('type') or 'incident').replace('_',' ').title()} "
                    f"in {top.get('zone','unknown zone')}. "
                    f"{(top.get('summary') or '')[:140]}"
                )

            to_number = st.text_input(
                "Phone number (with country code, e.g. +14155550123)",
                value=st.session_state.get("vapi_last_to_number", ""),
                key="vapi_outbound_to",
            )
            summary = st.text_area(
                "What the call should say",
                value=default_summary,
                height=80,
                key="vapi_outbound_summary",
            )
            op_name = st.session_state.get("operator_name", "operator")

            if st.button("📞 Call now", disabled=not (to_number and summary), use_container_width=True):
                with st.spinner("Dialing…"):
                    result = vapi_client.place_outbound_alert(
                        to_number=to_number.strip(),
                        incident_summary=summary.strip(),
                        operator_name=op_name,
                    )
                if result.get("ok"):
                    st.session_state.vapi_last_to_number = to_number.strip()
                    st.success(f"Call placed. Reference: {result.get('call_id','?')}")
                else:
                    st.error(f"Call failed: {result.get('error','unknown error')}")
    else:
        st.error("Voice assistant not set up.")

with broad_col:
    st.subheader("📢 Stadium Announcement")
    st.session_state.tts_enabled = st.toggle("Auto-announce urgent alerts", value=st.session_state.tts_enabled)
    incidents_to_announce = [i for i in incidents_recent if i.get("severity") in ("high", "critical")]
    if incidents_to_announce:
        latest = incidents_to_announce[0]
        text = f"Attention. {latest.get('type','alert').replace('_',' ')} in {latest.get('zone','the stadium')}. {(latest.get('summary','') or '')[:140]}"
        st.code(text[:200], language="text")
        col_a, col_b = st.columns(2)
        if col_a.button("📢 Broadcast now", use_container_width=True):
            safe_text = text.replace("`", "'").replace("\\", "")
            components.html(
                f"""
                <script>
                try {{
                  const u = new SpeechSynthesisUtterance({json.dumps(safe_text)});
                  u.rate = 0.95; u.pitch = 1.0; u.volume = 1.0;
                  window.speechSynthesis.cancel();
                  window.speechSynthesis.speak(u);
                }} catch(e) {{ console.error(e); }}
                </script>
                """,
                height=0,
            )
        if col_b.button("🔇 Stop", use_container_width=True):
            components.html(
                "<script>window.speechSynthesis.cancel();</script>",
                height=0,
            )
        if st.session_state.tts_enabled and latest.get("severity") == "critical" and latest.get("id") != st.session_state.last_announced_incident_id:
            st.session_state.last_announced_incident_id = latest.get("id")
            safe_text = text.replace("`", "'").replace("\\", "")
            components.html(
                f"""<script>
                const u = new SpeechSynthesisUtterance({json.dumps(safe_text)});
                u.rate = 0.95; u.pitch = 1.0; u.volume = 1.0;
                window.speechSynthesis.cancel();
                window.speechSynthesis.speak(u);
                </script>""",
                height=0,
            )
            st.success("📢 Auto-announcement played for new urgent alert")
    else:
        st.caption("Nothing urgent to announce right now.")

st.divider()


# ---------------------------------------------------------------------------
# Sidebar — demo controls (force-refresh + scenario picker)
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("Controls")
    st.caption(f"Auto-tick: every {TICK_MS//1000}s")

    # Operator card
    op_name = st.session_state.get("operator_name", "Unknown")
    op_email = st.session_state.get("operator_email", "—")
    op_role = st.session_state.get("operator_role", "—")
    st.markdown(
        f"""
        <div style='background:#FFFFFF; border-radius:18px; box-shadow:0 4px 18px rgba(20,20,20,0.06); padding:12px 14px; margin-bottom:10px;'>
          <div style='color:#0A0A0A; font-weight:700;'>{op_name}</div>
          <div style='color:#8E8C87; font-size:12px;'>{op_role}</div>
          <div style='color:#E85A3B; font-size:12px; margin-top:3px;'>{op_email}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    render_logout_button()
    st.divider()

    st.session_state.manual_mode = st.toggle(
        "🛑 Manual mode (pause LLM auto-refresh)",
        value=st.session_state.manual_mode,
        help=(
            "When ON: skips predictions/vision/intel/scoreboard auto-runs to "
            "preserve OpenRouter free-tier quota for chat. Use force-refresh "
            "buttons below to refresh manually. Monte Carlo + Red Cell still "
            "run every tick (free compute)."
        ),
    )

    st.subheader("Test a scenario")
    ptype = st.selectbox(
        "What if…",
        options=["match_end", "wicket_end_innings", "weather_rain", "close_gate", "open_gate", "incident_zone"],
        index=0,
    )
    perturbation = {"type": ptype}

    zones_json = json.loads((DATA_DIR / "stadium_zones.json").read_text())
    if ptype in ("close_gate", "open_gate"):
        gate_id = st.selectbox("Gate", options=[g["id"] for g in zones_json["gates"]])
        perturbation["gate_id"] = gate_id
    elif ptype == "incident_zone":
        zone_id = st.selectbox("Zone", options=[z["id"] for z in zones_json["zones"] if z["type"] != "field"])
        perturbation["zone_id"] = zone_id

    if perturbation != st.session_state.whatif_perturbation:
        st.session_state.whatif_perturbation = perturbation
        st.session_state["whatif_at"] = 0  # invalidate so next tick recomputes

    st.divider()
    st.subheader("👥 Active operators")
    if supabase_client.is_enabled():
        ops = maybe_run("active_ops", ttl_seconds=30, fn=lambda: supabase_client.fetch_active_operators(60), expensive=False) or []
        if not ops:
            st.caption("Only you in this session.")
        for op in ops[:6]:
            st.caption(f"👤 {op.get('name','?')} · {op.get('role','?')} ({op.get('email','?')})")

    st.divider()
    st.subheader("💾 Database")
    if supabase_client.is_enabled():
        sb = maybe_run("supabase_health", ttl_seconds=30, fn=supabase_client.health, expensive=False)
        if sb and sb.get("ok"):
            st.success(f"Connected · {sb['incidents']} incidents · {sb['tickets']} tickets")
            st.caption(f"server: {sb.get('server_time','')[:19]}")
        else:
            st.error(f"Database error: {(sb or {}).get('error','?')[:120]}")
    else:
        st.caption("Database not connected.")

    st.divider()
    st.subheader("Last update")
    st.caption(f"Crowd forecast: {age_str('whatif')}")
    st.caption(f"Crowding predictions: {age_str('predictions')}")
    st.caption(f"Fan messages: {age_str('fanmail')}")
    st.caption(f"Cameras: {age_str('vision')}")
    st.caption(f"Local alerts: {age_str('intel')}")
    st.caption(f"Match score: {age_str('scoreboard')}")

    st.divider()
    st.subheader("Run now")
    if st.button("⏭ Skip to next over", use_container_width=True):
        match_context.advance_over()
        st.session_state["predictions_at"] = 0
    if st.button("📰 Check local alerts now", use_container_width=True):
        st.session_state["intel_at"] = 0
    if st.button("🔮 Update crowding predictions", use_container_width=True):
        st.session_state["predictions_at"] = 0
    if st.button("🌐 Refresh match score", use_container_width=True):
        st.session_state["scoreboard_at"] = 0
    st.session_state.auto_process_mail = st.toggle(
        "Auto-handle new fan messages",
        value=st.session_state.auto_process_mail,
        help="When on, fan replies are classified by Gemini and routed to the Commander automatically.",
    )
    if st.button("🗑 Clear incidents", use_container_width=True):
        commander.clear_incidents()


# ---------------------------------------------------------------------------
# 3D digital twin (full width)
# ---------------------------------------------------------------------------

twin_cols = st.columns([3, 2])
with twin_cols[0]:
    st.subheader("Live Stadium View")
    if whatif_cmp:
        scen_overrides = {zid: pcts["p50"] for zid, pcts in whatif_cmp["scenario"]["density_percentiles"].items()}
        scen_state = state_with_perturbation_applied(st.session_state.whatif_perturbation)
        view_title = f"Scenario: {st.session_state.whatif_perturbation.get('type','-').replace('_',' ')}"
    else:
        scen_state = whatif_simulator.current_baseline_state()
        scen_overrides = None
        view_title = "Baseline"

    tab_map, tab_3d = st.tabs(["🗺 Map view", "📦 3D view"])
    with tab_map:
        st.caption(
            f"Real map of M. Chinnaswamy stadium. Brighter colours = more crowded. Last updated {age_str('whatif')}."
        )
        map_fig = build_map_figure(
            scen_state,
            title=view_title,
            density_overrides=scen_overrides,
            height=520,
        )
        st.plotly_chart(map_fig, use_container_width=True, key=f"map_{tick}")
    with tab_3d:
        st.caption(
            f"Each stadium section grows taller the more crowded it is. Last updated {age_str('whatif')} · drag to rotate."
        )
        fig = build_3d_figure(
            scen_state,
            title=view_title,
            density_overrides=scen_overrides,
            height=520,
        )
        st.plotly_chart(fig, use_container_width=True, key=f"twin_{tick}")

with twin_cols[1]:
    st.subheader("Highest-risk areas")
    if whatif_cmp:
        for z in whatif_cmp["scenario"]["top_crush_zones"][:5]:
            pct = z["p_crush"]
            color = "#E94B4B" if pct >= 0.5 else ("#F5A623" if pct >= 0.2 else "#7ED321")
            st.markdown(
                f"<div style='padding:10px 14px; margin:6px 0; background:#FFFFFF; border-radius:18px; box-shadow:0 4px 18px rgba(20,20,20,0.06); border-left:4px solid {color}; color:#0A0A0A;'>"
                f"<b>{z['zone_id']}</b> · Crush risk: <span style='color:{color}; font-weight:700;'>{pct:.1%}</span></div>",
                unsafe_allow_html=True,
            )
    else:
        st.caption("Calculating…")

    st.subheader("What's happening")
    if whatif_narration:
        st.info(whatif_narration.get("summary", ""))
        if whatif_narration.get("recommendation"):
            st.success(f"➡ {whatif_narration['recommendation']}")
        st.caption(f"updated {age_str('whatif_narration')}")
    else:
        st.caption("Generating summary… (change the scenario to refresh)")


# ---------------------------------------------------------------------------
# Middle row: Threat Intel + Predicted Surges + Vision
# ---------------------------------------------------------------------------

mid_cols = st.columns([2, 2, 2])

with mid_cols[0]:
    st.subheader("🌐 Local Alerts (live)")
    st.caption(f"updates every 5 min · {age_str('intel')}")
    if intel_result:
        rl = intel_result.get("overall_risk_level", "low")
        rl_color = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(rl, "⚪")
        st.markdown(f"{rl_color} **{rl.upper()}** — {intel_result.get('operator_briefing','')}")
        for t in (intel_result.get("threats") or []):
            sev_color = {"low": "#5BD96B", "medium": "#F5A623", "high": "#E94B4B", "critical": "#9B1C1C"}.get(t.get("severity", "low"), "#5BD96B")
            st.markdown(
                f"<div style='padding:10px 14px; margin:6px 0; background:#FFFFFF; border-radius:18px; box-shadow:0 4px 18px rgba(20,20,20,0.06); border-left:4px solid {sev_color}; color:#0A0A0A;'>"
                f"<b>{t.get('category','?')}</b>: {t.get('title','')}<br>"
                f"<span style='color:#8E8C87; font-size:12px;'>{t.get('summary','')}</span><br>"
                f"<span style='color:#E85A3B; font-size:12px;'>→ {t.get('recommended_action','')}</span></div>",
                unsafe_allow_html=True,
            )
    else:
        err = st.session_state.get("intel_error")
        if err:
            st.error(err)
        else:
            st.caption("Checking live news + weather…")

with mid_cols[1]:
    st.subheader("🔮 Crowding Predictions")
    st.caption(f"updates every 90s · {age_str('predictions')}")
    if predictions:
        for p in (predictions.get("predictions") or [])[:6]:
            sev_color = {"low": "#5BD96B", "medium": "#F5A623", "high": "#E94B4B", "critical": "#9B1C1C"}.get(p.get("severity", "low"), "#5BD96B")
            st.markdown(
                f"<div style='padding:10px 14px; margin:6px 0; background:#FFFFFF; border-radius:18px; box-shadow:0 4px 18px rgba(20,20,20,0.06); border-left:4px solid {sev_color}; color:#0A0A0A;'>"
                f"<b>{p['zone_id']}</b> · <span style='font-weight:700;'>{p.get('expected_density_pct','?')}%</span> in {p.get('minutes_until_peak','?')} min<br>"
                f"<span style='color:#8E8C87; font-size:12px;'>{p.get('driver','')}</span></div>",
                unsafe_allow_html=True,
            )
    else:
        err = st.session_state.get("predictions_error")
        if err:
            st.error(err)
        else:
            st.caption("Calculating predictions…")

with mid_cols[2]:
    st.subheader("👁️ Camera View (live CCTV)")
    st.caption(f"updates every 30s · {age_str('vision')}")
    if vision_result:
        density = vision_result.get("density_pct", 0)
        st.metric("How crowded", f"{density}%", vision_result.get("trend", "stable"))
        st.write(vision_result.get("summary", ""))
        if vision_result.get("anomalies"):
            st.warning("⚠ " + ", ".join(vision_result["anomalies"]))
        st.caption(f"camera: {vision_result.get('clip','?')}")
    else:
        st.caption("Waiting for camera feed…")


# ---------------------------------------------------------------------------
# Bottom row: Incident feed + Commander chat + Fan inbox
# ---------------------------------------------------------------------------

bot_cols = st.columns([2, 2, 2])

with bot_cols[0]:
    st.subheader("📋 Live Incidents")
    if supabase_client.is_enabled():
        st.caption("💾 Saved to database · survives restarts")
    incidents = commander.get_incidents(limit=15)
    if not incidents:
        st.caption("No incidents yet.")
    for inc in incidents:
        sev_emoji = {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(inc.get("severity", "low"), "⚪")
        with st.expander(f"{sev_emoji} {inc['type']} · {inc.get('zone','?')} · {inc.get('summary','')[:48]}"):
            st.caption(f"{inc.get('timestamp','')} · reported by: {inc.get('source','?')}")
            st.text(inc.get("plan", ""))
            if inc.get("security") and inc["security"].get("is_quarantined"):
                st.error(f"🛡 Blocked: {inc['security']['threats_found']} unsafe link(s) detected")
            if inc.get("fan_message"):
                fm = inc["fan_message"]
                st.code(f"From: {fm.get('from','')}\n{fm.get('body','')[:300]}", language="text")
            similar = inc.get("similar_past") or []
            if not similar and supabase_client.is_enabled() and inc.get("summary"):
                similar = supabase_client.find_similar_incidents(
                    inc["summary"], k=3, exclude_legacy_id=inc.get("legacy_id"))
            if similar:
                st.markdown("**🔗 Similar past incidents:**")
                for s in similar:
                    pct = int((s.get("similarity") or 0) * 100)
                    st.caption(f"  • {pct}% match · {s.get('type','?')} in {s.get('zone','?')}: {(s.get('summary') or '')[:90]}")

with bot_cols[1]:
    st.subheader("💬 Ask Command")
    for turn in st.session_state.chat_history[-6:]:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn.get("tools"):
                st.caption("Used: " + ", ".join(turn["tools"]))
    q = st.chat_input("Ask anything about the stadium…")
    if q:
        st.session_state.chat_history.append({"role": "user", "content": q})
        with st.spinner("Thinking…"):
            r = commander.answer_operator(q)
        st.session_state.chat_history.append({"role": "assistant", "content": r["answer"], "tools": r.get("tools_used", [])})
        st.rerun()

with bot_cols[2]:
    st.subheader("🌐 Web Lookup")
    bs = st.session_state.browser_session
    if not bs:
        st.caption("Look up live external info from the web — traffic, transit, weather. Watch it work below.")
        b_cols = st.columns(2)
        if b_cols[0].button("🚦 Live traffic", use_container_width=True):
            with st.spinner("Opening browser…"):
                st.session_state.browser_session = browser_agent.kickoff_traffic_session()
            st.rerun()
        if b_cols[1].button("🚌 Bus alerts", use_container_width=True):
            with st.spinner("Opening browser…"):
                try:
                    s = browser_agent.kickoff_traffic_session()
                    st.session_state.browser_session = s
                except Exception as e:
                    st.error(f"Browser error: {e}")
            st.rerun()
    else:
        st.caption(f"Session: {bs.get('session_id','-')[:8]}… · live below")
        if bs.get("live_url"):
            components.iframe(bs["live_url"], height=260, scrolling=True)
        if st.button("✖ Close session", use_container_width=True):
            st.session_state.browser_session = None
            st.rerun()


# ---------------------------------------------------------------------------
# Fan inbox + TTS broadcaster (final row)
# ---------------------------------------------------------------------------

st.divider()
final_cols = st.columns([2, 1])

with final_cols[0]:
    st.subheader("✉️ Fan Messages")
    st.caption(f"updates every 20s · {age_str('fanmail')} · auto-respond {'ON' if st.session_state.auto_process_mail else 'OFF'}")
    try:
        inbox_id = fan_concierge.get_inbox_id()
        from tools.agentmail_client import get_client
        recent = get_client().list_recent(inbox_id, limit=8)
        if not recent:
            st.caption("No messages yet. Reply from a demo inbox to see them appear.")
        for m in recent:
            with st.expander(f"✉ {m.sender[:30]} · {(m.subject or '')[:30]}"):
                st.text(m.body[:300])
    except Exception as e:
        st.error(f"Email service error: {e}")

with final_cols[1]:
    st.subheader("📜 Decision History")
    st.caption("Every action the system has taken, saved here. Last 8 shown.")
    if supabase_client.is_enabled():
        decisions = maybe_run("audit", ttl_seconds=15, fn=lambda: supabase_client.fetch_recent_decisions(limit=8), expensive=False) or []
        if not decisions:
            st.caption("Nothing logged yet.")
        for d in decisions:
            with st.expander(f"🤖 {d.get('agent_name','?')} · {d.get('action','?')[:48]}"):
                st.caption(f"{(d.get('created_at') or '').__str__()[:19]}")
                if d.get("reasoning"):
                    st.text(d["reasoning"][:300])
                if d.get("payload"):
                    st.json(d["payload"])
    else:
        st.caption("Database not connected — history disabled.")

st.divider()
priv_col, fan_col = st.columns([1, 1])

with priv_col:
    st.subheader("🛡 Privacy-Safe Report")
    st.caption(
        "Build an anonymised summary of every incident so far. Personal info (emails, phones, names) "
        "is removed, exact locations are blurred to general areas, and times are shifted — safe to "
        "share with analysts without exposing any individual attendee."
    )
    col_g, col_d = st.columns([1, 1])
    granularity = col_g.selectbox(
        "Group times by", ["hour", "day"], index=0, key="priv_granularity"
    )
    use_shift = col_d.toggle("Shift dates randomly", value=True, key="priv_shift_toggle", help="Hides the exact day the event happened.")

    if st.button("Build anonymised report", use_container_width=True, key="priv_btn"):
        raw = commander.get_incidents(limit=200)
        st.session_state.priv_report = privacy.build_post_event_report(
            raw,
            shift_days=None if use_shift else 0,
            granularity=granularity,
        )
        privacy.write_report_to_disk(st.session_state.priv_report)
        st.success(f"Report built · {st.session_state.priv_report['incident_count']} incidents included")

    report = st.session_state.get("priv_report")
    if report:
        m1, m2, m3 = st.columns(3)
        m1.metric("Incidents", report["incident_count"])
        m2.metric("Areas covered", len(report["by_zone_family"]))
        m3.metric("Dates shifted by", f"{report['applied_date_shift_days']}d")
        with st.expander("Breakdown (by type, severity, area, time)"):
            st.json({
                "by_type": report["by_type"],
                "by_severity": report["by_severity"],
                "by_area": report["by_zone_family"],
                "by_hour": report["by_hour"],
            })
        with st.expander("What was removed to protect privacy"):
            for note in report["privacy_notes"]:
                st.markdown(f"- {note}")
        st.download_button(
            "⬇ Download report (JSON)",
            data=json.dumps(report, indent=2, default=str),
            file_name="post_event_report.json",
            mime="application/json",
            use_container_width=True,
        )
    else:
        st.caption("Click *Build* to make a shareable report.")

with fan_col:
    st.subheader("🎮 Incoming Fan Reports")
    st.caption(
        "Live feed from the **📱 Fan App** (separate page — open it from the sidebar). "
        "Attendees report issues from their phones; urgent reports auto-route into incidents."
    )

    fr_stats = fan_reports.stats()
    s1, s2, s3 = st.columns(3)
    s1.metric("Reports filed", fr_stats["total_reports"])
    s2.metric("People reporting", fr_stats["unique_reporters"])
    s3.metric("Sent to response team", fr_stats["routed_count"])

    leaders = fan_reports.get_leaderboard(top_n=5)
    if leaders:
        st.markdown("**🏆 Top reporters**")
        for rank, p in enumerate(leaders, 1):
            st.markdown(
                f"<div style='padding:10px 14px; background:#FFFFFF; border-radius:18px; box-shadow:0 4px 18px rgba(20,20,20,0.06); "
                f"border-left:3px solid #E85A3B; color:#0A0A0A; font-size:13px; margin-bottom:6px;'>"
                f"<span style='color:#8E8C87;'>#{rank:02d}</span> "
                f"<b>{p['reporter_id']}</b> · <span style='font-weight:700;'>{p['points']}</span> pts · "
                f"{p['reports']} reports · <i style='color:#8E8C87;'>{p['badge']}</i></div>",
                unsafe_allow_html=True,
            )

    recent = fan_reports.get_recent_reports(limit=5)
    if recent:
        with st.expander(f"Latest {len(recent)} reports"):
            for r in recent:
                tag = "🚨" if r.get("routed_to_commander") else ("✅" if r.get("verified") else "•")
                st.markdown(
                    f"{tag} `{r['report_id']}` · **{r['category']}** in {r['zone']} · "
                    f"+{r['points_awarded']}pts · {r['submitted_at']}<br>"
                    f"<span style='color:#8E8C87; font-size:12px;'>{r['summary']}</span>",
                    unsafe_allow_html=True,
                )

# Auto-rerun message at bottom for context
st.caption(f"💡 This dashboard updates itself every {TICK_MS//1000} seconds. Each section refreshes on its own schedule — see the sidebar for the latest update times.")

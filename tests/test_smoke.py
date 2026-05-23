"""Smoke tests that don't require live API calls."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"


def test_data_files_present():
    for name in ["stadium_zones.json", "match_state.json", "tickets.json", "sop_library.json", "cached_vision.json"]:
        assert (DATA / name).exists(), f"missing data file: {name}"


def test_sops_have_required_fields():
    sops = json.loads((DATA / "sop_library.json").read_text())["sops"]
    for sop_id, sop in sops.items():
        assert sop["id"] == sop_id
        assert "title" in sop
        assert "severity" in sop
        assert isinstance(sop["actions"], list)
        for action in sop["actions"]:
            assert "id" in action and "label" in action
            assert "auto" in action and "requires_approval" in action


def test_tickets_have_real_demo_inboxes():
    tickets = json.loads((DATA / "tickets.json").read_text())["tickets"]
    demos = [t for t in tickets if t.get("is_demo")]
    assert len(demos) >= 2
    for t in demos:
        assert t["email"].endswith("@agentmail.to")


def test_zones_have_coordinates():
    zones = json.loads((DATA / "stadium_zones.json").read_text())["zones"]
    for z in zones:
        assert 0 <= z["x"] <= 100
        assert 0 <= z["y"] <= 100
        assert z["capacity"] > 0


def test_vision_cache_covers_all_clips():
    cache = json.loads((DATA / "cached_vision.json").read_text())
    for clip in ["normal.mp4", "dense.mp4", "panic.mp4"]:
        assert clip in cache
        assert 0 <= cache[clip]["density_pct"] <= 100


def test_virustotal_url_extraction():
    from tools.virustotal_client import extract_urls
    text = "Hey, I uploaded photos at https://example.com/photos and also http://malware.test/x.exe!"
    urls = extract_urls(text)
    assert "https://example.com/photos" in urls
    assert "http://malware.test/x.exe" in urls
    assert len(urls) == 2


def test_sop_library_has_security_sop():
    sops = json.loads((DATA / "sop_library.json").read_text())["sops"]
    assert "SECURITY_THREAT" in sops
    assert sops["SECURITY_THREAT"]["severity"] in ("medium", "high", "critical")


def test_firecrawl_client_imports():
    from tools.firecrawl_client import scrape_markdown, scrape_many
    assert callable(scrape_markdown)
    assert callable(scrape_many)


def test_intel_agent_has_topic_urls():
    from agents.intel import THREAT_TOPIC_URLS
    assert "bengaluru_news" in THREAT_TOPIC_URLS
    assert "weather_alerts" in THREAT_TOPIC_URLS
    for url in THREAT_TOPIC_URLS.values():
        assert url.startswith("http")


def test_openrouter_client_uses_openrouter():
    """Sanity: gemini_client.py is now OpenRouter under the hood."""
    from tools import gemini_client
    assert "openrouter" in gemini_client.OPENROUTER_URL.lower()
    assert ":free" in gemini_client._DEFAULT_TEXT_MODEL
    assert ":free" in gemini_client._DEFAULT_VISION_MODEL


def test_red_cell_runs_and_returns_vulnerabilities():
    from agents.red_cell import hunt
    r = hunt(risk_level="low", trials_per_scenario=30, top_k=3)
    assert r["candidates_evaluated"] >= 5
    assert len(r["top_vulnerabilities"]) <= 3
    assert "headline" in r
    for v in r["top_vulnerabilities"]:
        assert "delta_p_crush" in v
        assert "perturbation" in v


def test_browser_use_client_imports():
    from tools.browser_use_client import start_session, get_session, run_task
    from agents.browser_agent import scrape_live_traffic, kickoff_traffic_session
    assert callable(start_session)
    assert callable(scrape_live_traffic)


def test_supabase_client_health_when_configured():
    """If SUPABASE_DB_URL is set, the health check should succeed."""
    import os
    from tools.supabase_client import health, is_enabled
    if not is_enabled():
        return  # not configured — skip silently
    h = health()
    assert h.get("ok"), f"Supabase unreachable: {h}"
    assert "incidents" in h and "tickets" in h


def test_vapi_client_present():
    from tools import vapi_client
    assert callable(vapi_client.public_key)
    assert callable(vapi_client.web_widget_html)
    assert callable(vapi_client.get_or_create_assistant)
    assert callable(vapi_client.update_assistant_context)
    assert "CrowdSync" in vapi_client.BASE_SYSTEM_PROMPT
    html = vapi_client.web_widget_html(assistant_id="fake-id", operator_name="Tester")
    assert "vapi-mic-btn" in html
    assert "vapi.start" in html


def test_migration_file_exists():
    from pathlib import Path
    migrations = Path(__file__).resolve().parent.parent / "migrations" / "001_init.sql"
    assert migrations.exists()
    sql = migrations.read_text()
    for table in ("incidents", "agent_decisions", "tickets", "fan_messages_log"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql


def test_chinnaswamy_zones_and_gates():
    zones = json.loads((DATA / "stadium_zones.json").read_text())
    assert zones["stadium_name"] == "M. Chinnaswamy Stadium"
    zone_ids = {z["id"] for z in zones["zones"]}
    assert {"A_STAND", "B_UPPER", "B_LOWER", "C_UPPER", "C_LOWER",
            "G_UPPER", "G_LOWER", "N_STAND", "M1_STAND", "M3_STAND",
            "M4_STAND", "P_CORPORATE", "PAVILION_TERRACE",
            "P1_STAND", "P2_STAND", "P3_STAND", "P4_STAND", "CLUB_HOUSE"} <= zone_ids
    gate_ids = {g["id"] for g in zones["gates"]}
    assert "G14" in gate_ids and "G19" in gate_ids
    for g in zones["gates"]:
        assert "throughput_per_min" in g and g["throughput_per_min"] > 0


def test_monte_carlo_runs_and_returns_distribution():
    from agents.whatif_simulator import simulate
    r = simulate(trials=50, risk_level="low")
    assert 0.0 <= r["p_crush"] <= 1.0
    assert 0.0 <= r["p_slow_evac"] <= 1.0
    for percentile in ("p5", "p50", "p95"):
        assert percentile in r["evac_minutes"]
    assert len(r["top_crush_zones"]) > 0


def test_whatif_compare_baseline_vs_scenario():
    from agents.whatif_simulator import compare
    cmp = compare({"type": "close_gate", "gate_id": "G6"}, risk_level="medium", trials=50)
    assert "baseline" in cmp and "scenario" in cmp
    assert cmp["perturbation"]["type"] == "close_gate"


def test_streamlit_autorefresh_available():
    """Live dashboard depends on streamlit-autorefresh."""
    from streamlit_autorefresh import st_autorefresh  # noqa: F401


def test_3d_figure_builds():
    from agents.whatif_simulator import current_baseline_state
    from ui.stadium_3d import build_3d_figure
    fig = build_3d_figure(current_baseline_state())
    # ground + pitch + ~19 wedges + gates + landmarks
    assert len(fig.data) >= 20

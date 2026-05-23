"""Threat Intel Agent.

Scrapes the open web via Firecrawl for emerging threats relevant to the
stadium operation: protests, transit strikes, weather warnings, security
incidents, traffic chaos in the vicinity. Gemini summarizes raw scraped
content into structured intel that the Commander Agent can act on.

This directly addresses the "emerging threats" gap from the brief — most
crowd-control systems assume a static threat model and miss the news
cycle that frequently dictates operational risk on match day.
"""
from __future__ import annotations

import json
from typing import Optional

from tools.firecrawl_client import scrape_markdown
from tools.gemini_client import chat_json
from tools import supabase_client

SYSTEM = """You are the Threat Intel Agent inside CrowdSync. Read scraped
web content and surface incidents that could affect crowd safety today.

Care about:
- Protests / strikes / political rallies near the venue
- Transit disruption (rail/metro/bus) affecting fan arrival or exit
- Severe weather alerts (rain, lightning, heat)
- Security incidents (arrests, suspicious packages, threats)
- Traffic chaos / road closures on stadium access routes

Ignore:
- Routine match coverage / scores
- Sports gossip / transfers
- Generic city news without operational impact"""


THREAT_TOPIC_URLS = {
    "bengaluru_news": "https://news.google.com/search?q=Bengaluru+today",
    "weather_alerts": "https://news.google.com/search?q=Bengaluru+weather+alert+OR+rain",
    "transit": "https://news.google.com/search?q=Bengaluru+BMTC+OR+namma+metro+disruption",
    "stadium_news": "https://news.google.com/search?q=Chinnaswamy+stadium+OR+RCB",
}


def fetch_raw_intel(topics: Optional[list[str]] = None) -> dict:
    """Scrape configured topic URLs and return raw markdown per topic."""
    topics = topics or list(THREAT_TOPIC_URLS.keys())
    out = {}
    for topic in topics:
        url = THREAT_TOPIC_URLS.get(topic)
        if not url:
            continue
        scraped = scrape_markdown(url)
        out[topic] = {
            "url": url,
            "source": scraped.get("source"),
            "markdown": (scraped.get("markdown") or "")[:4000],
            "error": scraped.get("error"),
        }
    return out


def summarize_intel(raw_intel: dict, venue: str = "M. Chinnaswamy Stadium, Bengaluru") -> dict:
    """Use Gemini to extract structured threats from raw scraped content."""
    prompt = f"""Venue: {venue}
Match day: today.

You are reviewing scraped web content from news sources. Extract the threats
that could affect a cricket match at this venue TODAY. Return JSON:

{{
  "threats": [
    {{
      "title": "<short title>",
      "category": "PROTEST|TRANSIT|WEATHER|SECURITY|TRAFFIC|OTHER",
      "severity": "low|medium|high|critical",
      "summary": "<one sentence>",
      "recommended_action": "<one sentence>",
      "source_topic": "<which topic key from raw intel>"
    }}
  ],
  "overall_risk_level": "low|medium|high",
  "operator_briefing": "<2 sentence executive summary>"
}}

If nothing relevant, return an empty threats list with a calm briefing.

RAW SCRAPED CONTENT (truncated):
{json.dumps(raw_intel, indent=2)[:8000]}
"""
    try:
        return chat_json(prompt, system=SYSTEM)
    except Exception as e:
        return {
            "threats": [],
            "overall_risk_level": "low",
            "operator_briefing": f"Intel Agent error: {e}",
            "error": str(e),
        }


def run(venue: str = "M. Chinnaswamy Stadium, Bengaluru") -> dict:
    """One-shot: scrape + summarize. Returns the structured intel."""
    raw = fetch_raw_intel()
    summary = summarize_intel(raw, venue=venue)
    summary["sources"] = {
        topic: {"url": data["url"], "source": data["source"]}
        for topic, data in raw.items()
    }
    if supabase_client.is_enabled():
        supabase_client.log_agent_decision(
            agent_name="intel",
            action="threat_sweep",
            reasoning=summary.get("operator_briefing", "")[:500],
            payload={
                "risk_level": summary.get("overall_risk_level"),
                "threat_count": len(summary.get("threats") or []),
                "venue": venue,
            },
        )
    return summary

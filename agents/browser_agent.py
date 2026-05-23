"""Browser Agent — drives a real cloud browser to take real-world actions.

This is what turns CrowdSync from a dashboard into software. The agent
actually opens websites, reads dynamic content, and reports back. Use
cases for stadium ops:

- Live traffic to stadium access roads (Google Maps)
- Alternate route lookups when a primary road is jammed
- Public advisory broadcast to a hosted board / social feed
- Live transit status (BMTC bus / Namma Metro pages)
"""
from __future__ import annotations

import os
from typing import Optional

from tools.browser_use_client import kickoff_session, run_task

VENUE = "M. Chinnaswamy Stadium, Bengaluru"


def scrape_live_traffic() -> dict:
    """Open Google Maps and report traffic on stadium access roads."""
    task = (
        f"Open https://www.google.com/maps/search/{VENUE.replace(' ', '+')}, "
        "wait for the map to load, then read the live traffic colors on the "
        "roads immediately around the stadium (Cubbon Road, Queen's Road, "
        "Link Road, MG Road). For each road, state whether traffic is light, "
        "moderate, or heavy based on the visible color overlay. Return your "
        "answer as one short paragraph followed by a list of 4 lines, one per road."
    )
    return run_task(task, max_wait=120)


def find_alternate_route(from_zone: str = "A_STAND") -> dict:
    """Find best driving alternate from stadium to a major Bengaluru hub."""
    task = (
        "On Google Maps, get driving directions from "
        f"'{VENUE}' to 'Kempegowda International Airport, Bengaluru'. "
        "Report the recommended route, estimated travel time, and any "
        "traffic warnings shown. If multiple options are shown, list the "
        "two fastest with their respective travel times. Keep response under 100 words."
    )
    return run_task(task, max_wait=120)


def check_bmtc_disruption() -> dict:
    """Sweep BMTC + Namma Metro pages for service alerts that could affect fans."""
    task = (
        "Visit https://mybmtc.karnataka.gov.in/ and https://english.bmrc.co.in/ "
        "(Namma Metro). Look for any active service alerts, suspensions, or "
        "delays announced on these pages. Report findings as: "
        "1) Are BMTC buses running normally? 2) Is Namma Metro running normally? "
        "3) Any specific alerts for today? Keep response under 80 words."
    )
    return run_task(task, max_wait=120)


def kickoff_traffic_session() -> dict:
    """Fire-and-forget: start a live traffic session and return live_url for streaming.

    The UI embeds live_url in an iframe so the operator (and judges) can WATCH
    the browser agent click around in real time. Polling happens separately."""
    task = (
        f"Open google.com/maps, search '{VENUE}', and verbally read out the live "
        "traffic conditions on Cubbon Road, Queen's Road, Link Road, and MG Road. "
        "Report each road as light, moderate, or heavy. Return as bullet list."
    )
    session = kickoff_session(task)
    return {
        "session_id": session.session_id,
        "status": session.status,
        "live_url": session.live_url,
        "task": task,
    }

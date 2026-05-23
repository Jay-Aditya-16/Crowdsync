"""VirusTotal client — security scanning for inbound fan content.

CrowdSync exposes a public-facing email inbox (the Fan Concierge), which
makes it a phishing/malware target. Before any URL or attachment from a
fan email reaches Gemini classification, we scan it against VirusTotal's
threat intelligence (90+ AV engines + URL reputation).

Malicious content is quarantined, never passed to the LLM, and logged as
a SECURITY incident type that the Commander Agent escalates immediately.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

VT_BASE = "https://www.virustotal.com/api/v3"
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


@dataclass
class ScanVerdict:
    target: str
    target_type: str  # "url" | "file"
    malicious: int = 0
    suspicious: int = 0
    harmless: int = 0
    undetected: int = 0
    reputation: Optional[int] = None
    categories: list[str] = field(default_factory=list)
    is_threat: bool = False
    source: str = "live"  # "live" | "cache_miss" | "error" | "skipped"
    error: Optional[str] = None

    @property
    def severity(self) -> str:
        if self.malicious >= 5:
            return "critical"
        if self.malicious >= 1:
            return "high"
        if self.suspicious >= 2:
            return "medium"
        return "low"


def _api_key() -> Optional[str]:
    return os.getenv("VIRUSTOTAL_API_KEY")


def _headers() -> dict:
    return {"x-apikey": _api_key() or ""}


def _url_id(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().strip("=")


def extract_urls(text: str) -> list[str]:
    """Extract candidate URLs from message text. Dedupes preserving order."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in URL_RE.finditer(text):
        u = m.group(0).rstrip(".,;:!?)]}>\"'")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def scan_url(url: str, timeout: float = 6.0) -> ScanVerdict:
    """Look up cached URL analysis on VirusTotal. Fast: returns whatever's known."""
    if not _api_key():
        return ScanVerdict(target=url, target_type="url", source="skipped", error="no_api_key")

    try:
        r = requests.get(f"{VT_BASE}/urls/{_url_id(url)}", headers=_headers(), timeout=timeout)
    except requests.RequestException as e:
        return ScanVerdict(target=url, target_type="url", source="error", error=str(e))

    if r.status_code == 404:
        # No prior analysis. Submit it; caller can re-check later. For demo
        # speed we don't wait for the result here.
        try:
            requests.post(f"{VT_BASE}/urls", headers=_headers(), data={"url": url}, timeout=timeout)
        except requests.RequestException:
            pass
        return ScanVerdict(target=url, target_type="url", source="cache_miss")

    if r.status_code != 200:
        return ScanVerdict(target=url, target_type="url", source="error", error=f"http_{r.status_code}")

    data = r.json().get("data", {}).get("attributes", {})
    stats = data.get("last_analysis_stats", {})
    verdict = ScanVerdict(
        target=url,
        target_type="url",
        malicious=stats.get("malicious", 0),
        suspicious=stats.get("suspicious", 0),
        harmless=stats.get("harmless", 0),
        undetected=stats.get("undetected", 0),
        reputation=data.get("reputation"),
        categories=list((data.get("categories") or {}).values()),
        source="live",
    )
    verdict.is_threat = verdict.malicious > 0 or verdict.suspicious >= 2
    return verdict


def scan_file_bytes(data: bytes, filename: str = "attachment", timeout: float = 6.0) -> ScanVerdict:
    """Look up a file by SHA-256 on VirusTotal. Does not upload (privacy + speed)."""
    if not _api_key():
        return ScanVerdict(target=filename, target_type="file", source="skipped", error="no_api_key")

    sha256 = hashlib.sha256(data).hexdigest()
    try:
        r = requests.get(f"{VT_BASE}/files/{sha256}", headers=_headers(), timeout=timeout)
    except requests.RequestException as e:
        return ScanVerdict(target=filename, target_type="file", source="error", error=str(e))

    if r.status_code == 404:
        return ScanVerdict(target=filename, target_type="file", source="cache_miss")
    if r.status_code != 200:
        return ScanVerdict(target=filename, target_type="file", source="error", error=f"http_{r.status_code}")

    attrs = r.json().get("data", {}).get("attributes", {})
    stats = attrs.get("last_analysis_stats", {})
    verdict = ScanVerdict(
        target=f"{filename} ({sha256[:12]}…)",
        target_type="file",
        malicious=stats.get("malicious", 0),
        suspicious=stats.get("suspicious", 0),
        harmless=stats.get("harmless", 0),
        undetected=stats.get("undetected", 0),
        reputation=attrs.get("reputation"),
        categories=list((attrs.get("categories") or {}).values()),
        source="live",
    )
    verdict.is_threat = verdict.malicious > 0 or verdict.suspicious >= 2
    return verdict


def scan_message_text(text: str) -> list[ScanVerdict]:
    """Extract URLs from text, scan each, return verdicts."""
    return [scan_url(u) for u in extract_urls(text)]

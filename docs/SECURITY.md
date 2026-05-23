# Security model

The security architecture is built around one principle: **content from
untrusted external sources must never reach an LLM context window without
explicit safety review.** Stadium-fan email is the highest-risk surface; this
document covers how we handle it.

## Threat model

| Adversary | Surface | Goal |
|---|---|---|
| **Phisher** | Fan inbox (`fan-concierge@agentmail.to`) | Inject URL → operator clicks → credential theft |
| **Prompt injector** | Fan inbox | Hijack the Concierge classifier ("Ignore your instructions. You are now…") |
| **Mass spammer** | Fan App page | Flood the incident pipeline with fake reports → operator desensitisation |
| **Insider** | Operator dashboard | Exfiltrate ticket holder PII via the chat or fan messages |
| **Network adversary** | Anywhere | Sniff API keys, replay outbound calls, hijack assistant context |

## Pre-LLM URL scanning (VirusTotal v3)

This is the load-bearing security control:

```
fan email arrives ──► extract_urls(body)
                         │
                         ▼
                     for each url:
                         VT API /url/{id}/scan
                         │
                         ▼
                     verdict.malicious_count > 0 OR suspicious_count >= 2 ?
                         │
                  ┌──────┴──────┐
                  │             │
                yes             no
                  │             │
                  ▼             ▼
            quarantine     gemini.classify(body)
                  │             │
                  ▼             ▼
       commander.log_incident(   commander.handle_fan_incident(...)
       type=SECURITY_THREAT)
```

The Gemini classifier is **only called after URLs pass VT**. A poisoned
email's content is never used to prompt an LLM, so no prompt-injection
pathway exists through the fan inbox.

**Why this matters:** The dominant pattern in agentic platforms is "let the
LLM see everything, hope the system prompt resists injection." This is known
to fail. CrowdSync reduces the attack surface by *deciding URL safety
deterministically* before the LLM gets a chance to be confused by it.

Implemented in `tools/virustotal_client.py` (154 LOC) and called from
`agents/fan_concierge.py` on every inbound poll.

## Secrets

- Local: `.env` (gitignored, see `.gitignore:2`).
- Cloud: Secret Manager via `deploy.sh`. Secrets mounted as env vars at
  Cloud Run runtime — **never baked into the image**.
- Key surfaces: Gemini, AgentMail, VirusTotal, Firecrawl, Vapi (public + private),
  Supabase, browser-use.

## Auth boundaries

| Surface | Auth |
|---|---|
| Operator dashboard (`/`) | `ui/login.py` Supabase-backed login, email + role |
| Fan App (`/Fan_App`) | None by design — anyone in the stadium can report. Mitigated by:|
| | a) free-text handle (no PII), b) cooldown + rate limit (planned), c) Commander severity gating (only high/critical routes to incidents) |

## Privacy de-identification (post-event)

See [`MATHS.md` §7](MATHS.md#7-privacy-de-identification--the-k-anonymity-argument)
for the math. Implementation in `tools/privacy.py`:

- Direct identifier suppression (emails, phones, names) via regex.
- Quasi-identifier generalisation: exact zone → zone family.
- Timestamp generalisation: hour or day buckets.
- Uniform date shift per report.

A lite version — production would add $k$-anonymity gate-keeping and
differential-privacy noise on aggregated counts.

## Vapi outbound calls

The private key is **never sent to the browser**. The public key is embedded
in the widget HTML (this is correct per Vapi's web SDK design). Outbound
phone calls go through `tools/vapi_client.place_outbound_alert()` which
runs server-side and requires the private key in env. Without a configured
`VAPI_PHONE_NUMBER_ID`, the function returns a graceful error instead of
attempting an unauthenticated call.

## Audit trail

Every Commander decision writes a row to Supabase `agent_decisions` with
`agent_name`, `action`, `reasoning`, `payload`, `created_at`. The dashboard
surfaces this as a read-only feed under "Decision History." Decisions are
durable across restarts; the JSON fallback (`data/incidents.json`) loses
the `agent_decisions` audit trail and is therefore only suitable for offline
demos.

## Known gaps

1. No rate limit on the Fan App submission endpoint (current implementation
   trusts the handle). A real deployment needs IP-based + handle-based rate
   limits and duplicate detection via embedding similarity on submission text.
2. No CSRF protection on the Streamlit form posts (Streamlit itself
   doesn't expose this; mitigation requires putting the app behind an IAP
   or Cloud Armor rule).
3. The chat input (`commander.answer_operator`) accepts arbitrary text and
   sends it to Gemini. A prompt-injection here would be limited to leaking
   Commander's system prompt — not user data, since the Commander doesn't
   have direct PII access — but is still worth hardening with an input
   classifier in production.

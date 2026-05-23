# Architecture

CrowdSync is an 8-agent system with a Streamlit operator dashboard, a separate
fan-facing page, and a swap-in/swap-out tool layer. This document covers the
runtime topology, message flow, state model, and refresh contracts.

## 1. Process topology

```
┌─────────────────────────────────────────────────────────────────┐
│                   Browser (operator + fan)                      │
│                                                                 │
│   /                     /Fan_App                                │
│   ┌────────────────┐    ┌────────────────────┐                  │
│   │ Operator       │    │ Fan App            │                  │
│   │ - 3D twin / 2D │    │ - Submit report    │                  │
│   │ - KPIs, voice  │    │ - Points + badge   │                  │
│   │ - Chat, alerts │    │ - Leaderboard      │                  │
│   └────────┬───────┘    └────────┬───────────┘                  │
└────────────┼─────────────────────┼──────────────────────────────┘
             │ Streamlit auto-refresh (5s)                         
             │                                                     
┌────────────▼─────────────────────▼──────────────────────────────┐
│                     ui/app.py + ui/pages/                       │
│                                                                 │
│   maybe_run(name, ttl, fn, expensive)   ← per-subsystem TTL gate│
│                                                                 │
│                       ▼                                         │
│   agents/  ◄────────  Commander Agent  ──────►  tools/          │
│   ────────                  │                    ─────          │
│   match_context            calls                gemini_client   │
│   vision                                        agentmail       │
│   fan_concierge                                 virustotal      │
│   intel                                         firecrawl       │
│   whatif_simulator                              vapi_client     │
│   red_cell                                      supabase_client │
│   browser_agent                                 browser_use     │
│   fan_reports             (logs)                privacy         │
│                                                                 │
└─────────┬──────────────┬────────────┬──────────────┬───────────-┘
          │              │            │              │
   ┌──────▼─────┐ ┌──────▼─────┐ ┌────▼──────┐ ┌─────▼──────┐
   │ Supabase   │ │ AgentMail  │ │ Vapi      │ │ Gemini/    │
   │ (incidents,│ │ (fan inbox │ │ (voice    │ │ OpenRouter │
   │ audit,     │ │ + reply    │ │ widget +  │ │ (LLM)      │
   │ pgvector)  │ │ routing)   │ │ outbound) │ │            │
   └────────────┘ └────────────┘ └───────────┘ └────────────┘
        + Firecrawl (web scrape) + VirusTotal (URL safety)
```

## 2. Agent responsibilities and ownership

| Agent | LOC | Reads | Writes | Cadence |
|---|---|---|---|---|
| **commander** | 379 | sop_library, all sub-agent outputs | incidents.json + Supabase | event-driven |
| **whatif_simulator** | 368 | stadium_zones, match_state | (pure compute) | 5 s tick |
| **fan_concierge** | 291 | AgentMail inbox | sends outbound mails | 20 s tick |
| **match_context** | 231 | match_state, Cricbuzz via Firecrawl | match_state.json | 90 s tick (LLM) + 2 min (web) |
| **intel** | 121 | Firecrawl news + weather | (in-memory) | 5 min tick |
| **red_cell** | 118 | whatif_simulator | (in-memory) | 5 min tick |
| **vision** | 87 | mp4 clips via Gemini multimodal | cached_vision.json | 30 s rotation |
| **browser_agent** | 75 | browser-use cloud sessions | (session state) | on-demand |
| **fan_reports** | new | fan_reports.json | fan_reports.json, commander.log_incident | event-driven |

The **Commander** is the *only* agent that mutates the incident store. Every
other agent's "side effect" is a function call that hands a structured dict to
the Commander, which then runs the SOP dispatch and writes one canonical
record.

## 3. Scheduling contract (the `maybe_run` gate)

Every expensive operation passes through `maybe_run(name, ttl_seconds, fn, expensive)` in `ui/app.py`. This gate:

1. Reads a `*_at` timestamp out of `st.session_state`.
2. If `now - last < ttl`, returns the cached result from `*_result`.
3. Otherwise, **at most one `expensive=True` op runs per 5 s tick** — even
   if three are stale. The most stale wins; the rest wait one tick.

This is what keeps Streamlit's autorefresh from hammering free-tier LLM quotas
under load. It's a single global mutex, not per-subsystem.

```
Tick T+0:  predictions stale (90s), vision stale (30s), intel stale (5m)
           → only intel runs (most stale by ratio)
Tick T+5:  predictions stale, vision stale
           → predictions runs
Tick T+10: vision stale
           → vision runs
```

Without this gate, three concurrent Gemini calls per 5 s = ~36 calls/min → free tier (~20/min) blows up.

## 4. State model

**Three layers**, by intentional design:

| Layer | Stores | Survives | Purpose |
|---|---|---|---|
| `st.session_state` | per-session run timestamps, chat history, UI selections | until tab closes | UI ephemeral |
| `data/*.json` | incidents, match state, fan reports, vision cache | process restarts | demo persistence + offline fallback |
| Supabase (Postgres + pgvector) | incidents, agent decisions, tickets, pgvector embeddings of past incidents | forever | institutional memory, audit, cross-instance |

The Supabase layer is **optional** — `supabase_client.is_enabled()` short-
circuits if `SUPABASE_DB_URL` isn't set, falling back to JSON files. This is
intentional for hackathon judging: the app demo runs without any DB
configuration, but a production deploy switches Supabase on via env var.

When Supabase **is** enabled, the Commander writes every incident *also* to
`pgvector`, and `find_similar_incidents(summary, k=3)` uses cosine similarity
on embeddings to surface relevant past incidents — institutional memory the
operator sees inline under each new alert.

## 5. The bidirectional fan loop (the unique-to-us flow)

Most stadium tech ships a heatmap. We close the loop both ways:

```
                    ┌─────────────────────────┐
                    │ Match Context Agent     │
                    │ predicts surge in N min │
                    └────────────┬────────────┘
                                 │ predicted_surge
                                 ▼
        ┌──────────────────────────────────────────────┐
        │ Fan Concierge Agent                          │
        │ - looks up ticket holders in affected zone   │
        │ - drafts personalised email via Gemini       │
        │ - sends via AgentMail                        │
        └────────────┬─────────────────────────────────┘
                     │ "exit via East Gate, save 12 min"
                     ▼
        ┌──────────────────────────────────────────────┐
        │ Real fan inbox (AgentMail.to)                │
        └────────────┬─────────────────────────────────┘
                     │ fan replies: "my kid is missing"
                     ▼
        ┌──────────────────────────────────────────────┐
        │ Fan Concierge Agent (inbound)                │
        │ 1. VirusTotal scans every URL in body        │
        │ 2. quarantined? → SECURITY_THREAT incident   │
        │ 3. clean → Gemini classifies                 │
        │ 4. → handle_fan_incident(classification)     │
        └────────────┬─────────────────────────────────┘
                     │ structured classification
                     ▼
        ┌──────────────────────────────────────────────┐
        │ Commander Agent                              │
        │ - fires matching SOP (LOST_CHILD, MEDICAL…)  │
        │ - drafts action plan via Gemini              │
        │ - auto-replies to fan via Concierge          │
        │ - escalates high/critical to operator email  │
        └──────────────────────────────────────────────┘
```

Every link in this chain is implemented. The demo loop completes in <20 seconds
end-to-end because the Concierge poll cadence is 20 s.

## 6. Fan App as a second client

Streamlit's multi-page convention auto-discovers `ui/pages/*.py`. The fan app
at `ui/pages/2_📱_Fan_App.py` is a sibling page — same Python process, same
shared `data/fan_reports.json` store, but no operator auth. A fan visits
`/Fan_App`, picks a handle, submits a report, and the same
`fan_reports.submit_report()` function routes high/critical entries straight
into `commander.log_incident()`. The operator sees them appear in the live
incident feed with `source: fan_report`.

In production this would split into a dedicated mobile app + REST endpoint, but
the data plane stays the same.

## 7. What we deliberately do NOT do

- **No agent-to-agent direct messaging.** All cross-agent communication goes
  through Commander or a shared file store. This keeps the call graph acyclic
  and debuggable. (Agent frameworks that let agents call each other freely
  produce demo magic but become unauditable at incident-review time.)
- **No streaming LLM responses to the UI.** Every LLM call is request/response
  with structured JSON output. Streaming looks impressive but doesn't survive
  Streamlit reruns cleanly.
- **No in-process WebSocket fan-out.** Streamlit autorefresh is the primary
  mechanism; for multi-operator real-time we'd switch to Cloud Run + Pub/Sub
  + a thin WebSocket gateway. Documented; not built.

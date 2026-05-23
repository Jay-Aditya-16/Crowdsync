# CrowdSync — Multi-Agent Stadium Command Platform

> Built for the **Build with AI: Agentic Premier League** hackathon.
>
> **Problem:** Massive cricket crowds create bottlenecks, security risks, and
> chaos during pre/post-match movements. Operations rely on fragmented manual
> systems that cannot adapt to surges, weather, or emerging threats.
>
> **Solution:** A **live 3D digital twin** of M. Chinnaswamy Stadium fed by
> five cooperating AI agents — predicting surges from match context,
> watching CCTV via Gemini vision, communicating two-way with fans via
> AgentMail email, scraping the open web for emerging threats, and an
> orchestrating Commander that applies SOPs. A Monte Carlo What-If
> Simulator runs hundreds of stochastic trials per scenario to surface
> probability distributions over crush risk and evacuation time.

## What makes this different

Most teams will ship a CCTV heatmap with operator alerts. CrowdSync closes
the loop on both sides:

1. **Demand-side load balancing** — the Fan Concierge Agent emails affected
   fans *before* a bottleneck forms ("exit via East Gate, save 12 min"),
   reducing the crowd surge instead of just reacting to it.
2. **Real two-way communication** — fans can reply to nudges ("my kid is
   missing near Gate 4"). The agent reads, classifies, and routes the
   incident to the Commander, which fires the right SOP automatically.
3. **Cricket-aware predictions** — the Match Context Agent reasons over
   match state (wickets, innings break, last over) + weather to predict
   surges *5–10 minutes ahead* instead of reacting to them.
4. **VirusTotal-hardened intake** — every URL in an inbound fan email is
   scanned against 90+ AV engines *before* Gemini sees the content.
   Phishing or malware quarantines automatically and never reaches the
   LLM, directly addressing the "emerging threats" gap from the brief.
5. **Firecrawl live web ingest** — the Match Context Agent can refresh
   from a real cricket scoreboard, and a dedicated **Threat Intel Agent**
   sweeps news/weather sources for protests, transit strikes, and
   weather alerts that could affect the venue *today*. This replaces a
   static threat model with continuously-updated situational awareness.
6. **Live 3D digital twin + Monte Carlo What-If** — the entire stadium
   (19 stands, 18 gates, pitch, boundary roads) is rendered as a 3D
   model where each stand extrudes by current density. Pose any
   perturbation ("close Gate G6", "rain starts", "match ends now") and
   the **Monte Carlo Threat Predictor** runs 100-1000 stochastic trials,
   sampling density and gate throughput, to produce probability
   distributions: P(crush), P(evacuation > 10 min), and 5th/50th/95th
   percentile densities per zone. Uncertainty widens automatically when
   the Threat Intel Agent flags elevated risk. Production cadence is
   one re-run every 5 seconds, before humans even notice.

## Architecture

```
                                ┌──────────────────────┐
                                │   Operator (web UI)  │
                                │   Streamlit on       │
                                │   Cloud Run          │
                                └──────────┬───────────┘
                                           │
                                ┌──────────▼───────────┐
                                │   Commander Agent    │
                                │   (Gemini reasoning  │
                                │   + SOP library)     │
                                └──┬───────┬────────┬──┘
                                   │       │        │
              ┌────────────────────┘       │        └────────────────────┐
              │                            │                             │
   ┌──────────▼──────────┐  ┌──────────────▼─────────┐  ┌────────────────▼──────────┐
   │ Match Context Agent │  │ Fan Concierge Agent    │  │ Vision Agent              │
   │ Cricket + weather   │  │ AgentMail send / recv  │  │ Gemini multimodal on      │
   │ → surge predictions │  │ → 2-way fan comms      │  │ stadium camera clips      │
   │ (Gemini)            │  │ (Gemini + AgentMail)   │  │ (Gemini Vision)           │
   └─────────────────────┘  └────────────────────────┘  └───────────────────────────┘
                                           │
                                ┌──────────▼───────────┐
                                │   AgentMail Inboxes  │
                                │   - fan-concierge    │
                                │   - commander        │
                                └──────────┬───────────┘
                                           │
                                ┌──────────▼───────────┐
                                │   Real fan inboxes   │
                                │   (visible during    │
                                │   live demo)         │
                                └──────────────────────┘
```

## The six agents

| Agent | Role | Inputs | Tools |
|---|---|---|---|
| **Match Context** | Cricket-aware crowd surge predictor | match_state.json, weather, zones | `predict_surge(zone, mins)`, `get_match_state()`, `refresh_from_live_scoreboard()` (via Firecrawl) |
| **Vision** | CCTV anomaly + density detection | mp4 clips (sampled by Gemini) | `analyze_clip(name)` — returns density + anomalies |
| **Fan Concierge** | Personalized fan nudges + reply handling, with VirusTotal pre-scan | predicted surges, AgentMail inbox | `send_nudges_for_surge()`, `poll_replies()`, `acknowledge_fan()` |
| **Threat Intel** | Open-source intelligence sweep for emerging operational threats | news + weather URLs (via Firecrawl) | `fetch_raw_intel()`, `summarize_intel()`, `run()` |
| **What-If Simulator** | Monte Carlo crowd forecaster + perturbation analyzer | full stadium topology + risk level | `simulate()`, `compare(perturbation)`, `monte_carlo()`, `narrate_scenario()` |
| **Commander** | SOP orchestrator + human escalation | all of above + SOP library | `handle_predicted_surge()`, `handle_vision_anomaly()`, `handle_fan_incident()`, `answer_operator()` |

The Commander loads `data/sop_library.json` and picks the matching SOP
(`CROWD_SURGE`, `LOST_CHILD`, `MEDICAL`, `WEATHER_RAIN`, `PANIC_BEHAVIOR`).
For each SOP it drafts an action plan with Gemini, executes the `auto`
actions, surfaces `requires_approval` actions to the operator, and escalates
high/critical severity to a human inbox via AgentMail.

## Stadium layout — M. Chinnaswamy, Bengaluru

19 zones across A/B/C/G/M/N/P stand families + Club House + Pavilion;
18 gates around the perimeter (G1-G9 west along Queen's Road, G12-G16
north along Cubbon Road, G17-G19 east along Link Road, G20-G21 SE along
MG Road). Each gate has a `throughput_per_min` capacity used by the
Monte Carlo evacuation model. Coordinates were transcribed from the
official stadium plan.

## Live ops mode

The dashboard **auto-refreshes every 5 seconds**. Each subsystem has its
own TTL so we respect the Gemini / Firecrawl / AgentMail quotas:

| Subsystem | Refresh | Cost per cycle |
|---|---|---|
| Monte Carlo + 3D twin | 5 s | free (pure compute) |
| Fan inbox poll + auto-fire SOPs | 20 s | 1 AgentMail list call (+ Gemini classify only on new messages) |
| Vision Agent (camera rotator) | 30 s | cached / 1 Gemini multimodal |
| Predicted surges | 90 s | 1 Gemini call |
| Live scoreboard refresh | 2 min | 1 Firecrawl + 1 Gemini |
| Threat Intel sweep | 5 min | 3 Firecrawl + 1 Gemini |

Each tile shows a "X seconds ago" badge. Operators see the current
P(crush), evac time, top-5 crush zones, threat intel briefing, predicted
surges, live camera readout, and incident feed **without clicking
anything**. Pose a different What-If scenario from the sidebar and the
3D twin + Monte Carlo recompute on the next tick.

> **Auto-fire mode:** when a fan emails the Concierge inbox, the next
> 20-second tick polls the inbox, runs VirusTotal on URLs, classifies
> via Gemini, and routes incidents straight to the Commander Agent
> (which fires the matching SOP, sends the acknowledgment reply, and
> escalates if severity is high/critical). No operator intervention
> needed for the loop to close. Toggle off in the sidebar if you want
> manual approval gates instead.

## Demo flow (90 seconds)

1. Sidebar → **Refresh from LIVE scoreboard** (Firecrawl scrapes Cricbuzz,
   Gemini extracts current match state). The match state shown on screen
   is now real.
2. Sidebar → **Run Threat Intel Agent**. The agent scrapes Google News for
   Mumbai-area news + weather and surfaces real threats for today (e.g.
   "Severe bus transit disruption", "IMD storm alert"). Operator briefing
   appears top-right.
3. Sidebar → **Advance one over (+ wicket)**.
4. Sidebar → **Run Match Context Agent**. Map updates with predicted surges.
5. *"Predicted Surges"* panel → click **Route this surge** on the worst zone.
   Real emails are sent from `richperson405@agentmail.to` to
   `sillyconcept612@agentmail.to` and `youthfulunion707@agentmail.to`.
   Show them live in the AgentMail dashboard.
6. From one of those inboxes, **reply**: *"My kid is missing near Gate 4."*
7. Sidebar → **Poll Fan Inbox**. URLs in the reply are scanned by
   VirusTotal first; if clean, Gemini classifies as `LOST_CHILD`, the
   Commander fires the LOST_CHILD SOP, replies to the fan with help-desk
   location, and escalates to the operator email.
8. Sidebar → **Run Vision Agent** on `dense.mp4`. Vision Agent panel
   reports density + anomalies. Click **Route via Commander Agent →**.
9. **Commander Chat:** *"Any threats outside the stadium I should know
   about?"* — Commander invokes the Threat Intel Agent and answers with
   today's actual news.
10. **What-If Simulator:** select perturbation = *"close_gate"*, gate =
    *G6* (a main west gate), risk = pulled from Intel Agent. Hit Run.
    The 3D digital twin renders **side-by-side** before/after states,
    metrics show ΔP(crush) + Δevac time + ΔP(evac>10min) across 500
    Monte Carlo trials, and Gemini narrates the difference in plain
    English with a recommended action.

## Quickstart (local)

```bash
cd ~/apl/crowdsync
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# .env is already populated; verify GEMINI_API_KEY, AGENTMAIL_API_KEY,
# VIRUSTOTAL_API_KEY, FIRECRAWL_API_KEY
streamlit run ui/app.py
```

Open http://localhost:8501. The dashboard begins auto-refreshing
immediately — within ~30 seconds you'll see the first Monte Carlo, fan
inbox poll, vision frame, and threat intel briefing populate.

Optional: drop `normal.mp4`, `dense.mp4`, `panic.mp4` into `data/clips/`.
Without clips the Vision Agent serves cached responses so the demo never
breaks (see `data/cached_vision.json`).

## Deploy to Cloud Run

```bash
gcloud auth login
gcloud config set project platinum-loop-497205-a3
gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com
./deploy.sh
```

The script creates secrets in Secret Manager, grants the Cloud Run service
account access, then `gcloud run deploy --source .` builds the container
and exposes the dashboard publicly. The deploy URL prints at the end.

## Security & scalability

- **Inbound content scanning:** Every URL inside an incoming fan email is
  sent to the **VirusTotal v3 API** before Gemini classifies the message.
  If any URL is flagged malicious or has 2+ suspicious verdicts, the
  message is **quarantined** — its body is replaced with `[REDACTED]`
  before it reaches the LLM, so a poisoned email can never inject prompts
  or exfiltrate context. A SECURITY_THREAT incident is logged and
  escalated to the operator.
- **Secrets:** Gemini, AgentMail, and VirusTotal keys live in **Secret
  Manager**, mounted into Cloud Run as env vars at runtime. Never baked
  into the image.
- **IAM:** Each agent inbox is a separate `client_id`; AgentMail isolates
  inbox auth per agent.
- **Scalability:** Cloud Run autoscales 0→N. For 33k-fan stadiums, the path
  forward is:
  - **Pub/Sub** between Match Context → Fan Concierge to queue nudges
    instead of inline sends (current MVP sends inline, capped at 3
    recipients per surge to respect free-tier limits).
  - **Cloud Tasks** for retries on failed AgentMail sends.
  - **Vertex AI Vector Search** to dedupe similar incidents.
- **Graceful degradation:** Vision Agent falls back to cached responses if
  Gemini multimodal errors. Match Context errors return an empty
  prediction set without crashing the orchestrator.

## Rubric mapping (Phase 1 — 40 + 5 pts)

| Rubric | How CrowdSync addresses it |
|---|---|
| **Functional Fulfillment (15)** | Full bidirectional loop: predict → nudge fans → fans reply → classify → fire SOP → escalate. Live demo shows real emails arriving in real inboxes. Plus 3D digital twin renders the actual Chinnaswamy stadium, and Monte Carlo What-If gives operators a forecasted-versus-baseline view before they take any action. |
| **Scalability & Security (10)** | Secret Manager + Cloud Run autoscale + documented Pub/Sub path. IAM-isolated agent inboxes. **VirusTotal scans every inbound URL before any LLM call**, preventing prompt-injection / phishing pivots via the public-facing fan inbox. |
| **Static Code Analysis (15)** | Clear module boundaries (`agents/`, `tools/`, `ui/`, `data/`). Typed Python with dataclasses. Google AI SDKs used explicitly (`google-genai`, `agentmail`). Pytest covers Match Context. |
| **GCP Deployment (5)** | `./deploy.sh` → Cloud Run + Secret Manager + Cloud Build. Live URL in submission. |

## Project layout

```
crowdsync/
├── agents/
│   ├── match_context.py     # cricket → surge predictions (+ live scoreboard refresh)
│   ├── vision.py            # Gemini multimodal on clips
│   ├── fan_concierge.py     # AgentMail two-way fan comms (VT-gated)
│   ├── intel.py             # Threat Intel — Firecrawl news/weather sweep
│   ├── whatif_simulator.py  # Monte Carlo + What-If perturbations
│   └── commander.py         # SOP orchestrator
├── tools/
│   ├── gemini_client.py     # google-genai wrapper
│   ├── agentmail_client.py  # AgentMail SDK wrapper
│   ├── virustotal_client.py # VirusTotal URL + file reputation
│   └── firecrawl_client.py  # Firecrawl scrape wrapper (cached)
├── ui/app.py                # Streamlit dashboard
├── data/
│   ├── stadium_zones.json
│   ├── match_state.json
│   ├── tickets.json
│   ├── sop_library.json
│   ├── cached_vision.json
│   └── clips/               # mp4 samples (gitignored)
├── tests/test_smoke.py
├── Dockerfile
├── deploy.sh
├── requirements.txt
└── README.md
```

## Honest scope notes

- **WhatsApp / SMS:** The original sketch used WhatsApp; we switched to
  AgentMail email because it gave us **real two-way communication in 3
  hours** without Meta business verification. Production deployment would
  add a WhatsApp Business channel alongside email — same Fan Concierge
  Agent, different transport.
- **Real CCTV feed:** We sample short mp4 clips through Gemini multimodal.
  A production deployment would tap RTSP streams via GStreamer and sample
  one frame every 2s into the same pipeline.
- **ADK:** We use `google-genai` directly with structured-output JSON and
  keyword-based tool dispatch in the Commander, prioritizing reliability in
  a 3-hour build window. Migration to the Agent Development Kit
  orchestration model is a 1-file change in `agents/commander.py`.

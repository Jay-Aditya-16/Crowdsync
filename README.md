# CrowdSync — Multi-Agent Stadium Command Platform

> **Repo:** [github.com/Jay-Aditya-16/Crowdsync](https://github.com/Jay-Aditya-16/Crowdsync)
> Built for **Build with AI: Agentic Premier League** (Google Cloud).

## 🚀 Live demo

**Operator dashboard:** https://crowdsync-912849963950.asia-south1.run.app
**Fan App:** https://crowdsync-912849963950.asia-south1.run.app/Fan_App

Deployed on Google Cloud Run (`platinum-loop-497205-a3`, region `asia-south1`).
Secrets in Secret Manager; container built via Cloud Build; mapped to a public,
unauthenticated HTTPS endpoint. Cold start ~5 s, warm ~200 ms.

A live operations platform for stadium command. Eight cooperating agents,
two clients (operator dashboard + fan app), one Commander that owns the
incident state, and a Monte Carlo crush forecaster that runs probability
bands rather than point estimates.

```
                                  STADNIUM AI
                                       │
            ┌──────────────────────────┼──────────────────────────┐
            ▼                          ▼                          ▼
       OPERATOR                     FAN APP                   COMMANDER
       (web dashboard)              (mobile-like              (SOP dispatch,
                                     web page)                 audit trail)
            │                          │                          │
            └──────────────────────────┼──────────────────────────┘
                                       │
                       ┌───────────────┴──────────────┐
                       │   Monte Carlo + What-If      │
                       │   Match Context  • Vision    │
                       │   Threat Intel   • Red Cell  │
                       │   Fan Concierge  • Browser   │
                       └───────────────┬──────────────┘
                                       │
                ┌────────────┬─────────┼──────────┬──────────────┐
                ▼            ▼         ▼          ▼              ▼
            Gemini       AgentMail   Vapi     VirusTotal     Firecrawl
            (LLM)        (email)    (voice)   (URL safety)   (web scrape)
                                       │
                                ┌──────┴───────┐
                                │  Supabase    │
                                │  + pgvector  │
                                │  (audit,     │
                                │   memory)    │
                                └──────────────┘
```

---

## Quick links

| Doc | What's inside |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Process topology, agent ownership, scheduling contract, state model, bidirectional fan loop |
| [`docs/MATHS.md`](docs/MATHS.md) | Monte Carlo derivations, crush threshold rationale, evacuation model, $k$-anonymity argument |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Threat model, VirusTotal pre-LLM gate, auth boundaries, privacy de-identification |
| [`docs/DIFFERENTIATION.md`](docs/DIFFERENTIATION.md) | How Stadnium differs from CCTV-heatmap dashboards, generic Cloud Run agent demos, ticketing add-ons, and academic crowd simulators |

---

## The problem

M. Chinnaswamy Stadium holds ~40,000 fans. Bottlenecks form 5–10 minutes
before they become visible on CCTV — driven by **match state** (wickets,
innings break, last over), **weather**, and **external events** (transit
strikes, protests near venue). Operations today rely on radio chatter and
fragmented manual systems. The window to act on a forming surge is short,
and operators cannot reach the people whose movement actually shifts the
distribution: the **fans themselves**.

## The solution shape

Stadnium closes the loop on **both sides** of a surge:

```
Match Context Agent  ─ predicts surge in zone Z, T+5min
                      │
                      ▼
Fan Concierge Agent  ─ emails affected ticket holders ("exit East Gate")
                      │
                      ▼ (fan replies "my kid is missing")
Fan Concierge        ─ VirusTotal scans URLs ─► Gemini classifies
                      │
                      ▼
Commander Agent      ─ fires LOST_CHILD SOP, replies to fan, escalates
                      │
                      ▼
Operator dashboard   ─ sees the incident, response team converges
```

Every link is implemented. End-to-end loop time is ~20 s on the demo (gated
by the inbox poll cadence).

## What's inside

### Eight cooperating agents (`agents/`)

| Agent | LOC | Role | Key inputs | Key tools |
|---|---|---|---|---|
| **commander** | 379 | SOP orchestrator + incident store + operator chat | all sub-agent outputs | Gemini, Supabase |
| **whatif_simulator** | 368 | Monte Carlo crush + evac forecaster, What-If perturbations | stadium_zones, match_state | (pure compute) |
| **fan_concierge** | 291 | Inbound + outbound fan email, VT-gated, auto-SOP routing | AgentMail inbox, predicted surges | AgentMail, VirusTotal, Gemini |
| **match_context** | 231 | Cricket + weather → surge predictions, live scoreboard scrape | match_state, Cricbuzz | Gemini, Firecrawl |
| **intel** | 121 | Threat intel sweep — news + weather, scored by category | news/weather URLs | Firecrawl, Gemini |
| **red_cell** | 118 | Adversarial perturbation search — what's the worst What-If? | whatif_simulator | (pure compute) |
| **vision** | 87 | Gemini multimodal CCTV density + anomaly detection | mp4 clips | Gemini (multimodal) |
| **browser_agent** | 75 | Live web lookup (traffic, transit) via cloud browser | URLs | browser-use cloud |
| **fan_reports** | new | Crowd-sourced reports from attendees, points/badges, auto-route | Fan App submissions | Commander |

### Seven tool clients (`tools/`)

`gemini_client` (LLM), `agentmail_client` (2-way fan email),
`virustotal_client` (URL safety), `firecrawl_client` (web scrape),
`vapi_client` (voice — web widget + outbound calls), `supabase_client`
(Postgres + pgvector audit trail), `browser_use_client` (cloud browser),
plus `privacy` (post-event de-identification).

### Two clients (`ui/`)

- **Operator dashboard** (`ui/app.py`, 892 LOC) — KPIs, voice + PA pinned
  at top, live 3D twin + 2D OpenStreetMap heatmap (tabbed), local alerts,
  crowding predictions, camera view, live incidents, Ask Command chat,
  web lookup, fan messages, decision history, privacy report builder,
  incoming fan reports feed.
- **Fan App** (`ui/pages/2_📱_Fan_App.py`) — phone-style page; pick a
  handle, report issues, earn points, climb the leaderboard. Same data
  plane as the operator dashboard.

---

## The Monte Carlo, in one paragraph

At each tick, baseline state $s$ is constructed from match attendance and
zone capacities. A perturbation $\Phi$ (close gate G6, rain starts, match
ends, etc.) is applied. Then $N=200$ trials sample
$\tilde d_z \sim \mathcal{N}(d_z, \sigma_d^2)$ for each zone's density and
$\tilde\tau_g \sim \mathcal{N}(\tau_g, \sigma_\tau^2)$ for each gate's
throughput, with $\sigma$ scaled by the threat-intel risk level. Outputs are
**probability bands**: $\hat P_{\text{crush}}$, $\hat P_{\text{slow\_evac}}$,
per-zone $\{p_5, p_{50}, p_{95}\}$, and per-zone crush probability.
$N=200$ gives ±3.5 pp standard error at the worst case; the engine costs
~20 ms per run on a single core. Full derivation in
[`docs/MATHS.md`](docs/MATHS.md).

## The security boundary, in one paragraph

The riskiest surface is fan email — it carries URLs from arbitrary senders.
Every inbound URL is scanned by **VirusTotal v3** *before* Gemini sees the
message body. Any URL flagged malicious (or with ≥2 suspicious verdicts)
quarantines the entire message, replaces the body with `[REDACTED]`, and
logs a `SECURITY_THREAT` incident. The Gemini classifier never sees the
poisoned content — closing the prompt-injection pathway from the public
inbox. Full threat model in [`docs/SECURITY.md`](docs/SECURITY.md).

---

## Run locally

```bash
git clone https://github.com/Jay-Aditya-16/Crowdsync.git
cd stadnium-ai
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys — at minimum GEMINI_API_KEY
streamlit run ui/app.py
```

Open http://localhost:8501 for the operator dashboard, or
http://localhost:8501/Fan_App for the fan submission page.

The dashboard auto-refreshes every 5 s. First Monte Carlo, fan-inbox poll,
vision frame, and threat-intel briefing populate within ~30 s.

**Optional:** drop `normal.mp4`, `dense.mp4`, `panic.mp4` into `data/clips/`
for the Vision Agent to analyze. Without them it serves cached responses so
the demo never breaks.

## Required environment variables

| Key | Required? | What it enables |
|---|---|---|
| `GEMINI_API_KEY` | **yes** (or fallback path) | All LLM calls. Without it, the app runs in deterministic-fallback mode (still ships). |
| `AGENTMAIL_API_KEY` | for fan loop | Inbound + outbound fan email |
| `VIRUSTOTAL_API_KEY` | for security | URL safety pre-scan |
| `FIRECRAWL_API_KEY` | for web ingest | Cricbuzz scoreboard, news + weather |
| `VAPI_PUBLIC_KEY` + `VAPI_PRIVATE_KEY` | for voice | Voice assistant widget |
| `VAPI_PHONE_NUMBER_ID` | for outbound calls | Real phone calls (graceful error without) |
| `SUPABASE_DB_URL` + `SUPABASE_URL` + `SUPABASE_ANON_KEY` | optional | Durable incidents + audit trail + pgvector similarity. App falls back to JSON files when unset. |
| `BROWSER_USE_API_KEY` | optional | Live web lookup |

## Deploy to Cloud Run

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com
./deploy.sh
```

`deploy.sh` creates Secret Manager entries for each key, grants the Cloud
Run service account access, and runs `gcloud run deploy --source .`. The
deploy URL is printed at the end.

## Project layout

```
stadnium-ai/
├── agents/                      # 9 agent modules (incl. fan_reports)
│   ├── commander.py             # SOP orchestrator, incident store
│   ├── whatif_simulator.py      # Monte Carlo + perturbations
│   ├── fan_concierge.py         # 2-way fan email, VT-gated
│   ├── match_context.py         # Cricket → surge predictions
│   ├── intel.py                 # Threat intel sweep
│   ├── red_cell.py              # Adversarial perturbation search
│   ├── vision.py                # CCTV density via Gemini multimodal
│   ├── browser_agent.py         # browser-use cloud sessions
│   └── fan_reports.py           # Crowd-sourced fan reports
├── tools/                       # 8 service clients
│   ├── gemini_client.py         # LLM
│   ├── agentmail_client.py
│   ├── virustotal_client.py
│   ├── firecrawl_client.py
│   ├── vapi_client.py
│   ├── supabase_client.py
│   ├── browser_use_client.py
│   └── privacy.py               # Post-event de-identification
├── ui/
│   ├── app.py                   # Operator dashboard (Streamlit)
│   ├── theme.py                 # Light neumorphic theme
│   ├── stadium_3d.py            # 3D digital twin (Plotly)
│   ├── stadium_map.py           # 2D OpenStreetMap heatmap
│   ├── login.py
│   └── pages/
│       └── 2_📱_Fan_App.py      # Fan-facing page
├── data/
│   ├── stadium_zones.json       # M. Chinnaswamy topology
│   ├── match_state.json
│   ├── sop_library.json
│   ├── incidents.json           # Local fallback store
│   ├── fan_reports.json
│   └── cached_vision.json
├── migrations/
│   └── 001_init.sql             # Supabase schema (pgvector)
├── tests/
│   └── test_smoke.py            # Module-level smoke + Vapi surface
├── docs/
│   ├── ARCHITECTURE.md
│   ├── MATHS.md
│   ├── SECURITY.md
│   └── DIFFERENTIATION.md
├── .streamlit/config.toml       # Theme + headless server config
├── Dockerfile
├── deploy.sh
└── requirements.txt
```

## What's lite, what's production

| Component | Status |
|---|---|
| 8 agents + Commander dispatch | ✅ production-shape, hackathon-MVP-depth |
| Monte Carlo engine | ✅ production-shape, 200 trials/tick under 1 ms each |
| VirusTotal pre-LLM gate | ✅ production-grade — same code would ship |
| Fan email loop (AgentMail) | ✅ end-to-end functional |
| Voice assistant (Vapi) | ✅ web widget + outbound calls (needs phone number ID) |
| 3D twin + 2D real-map heatmap | ✅ both render the same state |
| Privacy de-id | 🟡 *lite* — `k`-anonymity gate not enforced; documented |
| Fan App auth | 🟡 *lite* — no rate limit, handle-only; documented |
| Tests | 🟡 *smoke only* — `tests/test_smoke.py` (182 LOC) |
| CI | ❌ not wired |
| Cloud Run deploy | ✅ **live at https://crowdsync-912849963950.asia-south1.run.app** |

Honest scoping. The architecture is the load-bearing piece — every gap above
is slot-in replaceable without touching agent boundaries.

## License

MIT — see [`LICENSE`](LICENSE).

## Acknowledgments

Built solo in the **Build with AI: Agentic Premier League** finale window
(May 2026). Special call-outs:

- **Google Cloud / Gemini** — LLM reasoning + multimodal vision
- **AgentMail** — programmable email inboxes that made the fan loop possible
- **Vapi** — voice infra
- **VirusTotal** — URL reputation
- **Firecrawl** — web scrape
- **browser-use** — cloud-browser sessions for live lookups
- **Supabase** — Postgres + pgvector for audit + institutional memory
- **OpenStreetMap** — real stadium geography for the map view

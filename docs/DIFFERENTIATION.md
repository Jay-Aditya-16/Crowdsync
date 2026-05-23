# What makes CrowdSync different

Stadium-management AI is a crowded category. This document compares CrowdSync
to the four shapes of competition you'll see in any hackathon or RFP:
**CCTV-heatmap dashboards, generic Cloud-Run agent demos, ticketing-app
add-ons, and academic crowd-simulation packages.** For each, we name the gap
CrowdSync closes.

## 1. vs CCTV-heatmap dashboards (the typical product)

| Capability | Heatmap dashboards | CrowdSync |
|---|---|---|
| Live density map | ✅ (operator-only) | ✅ + 3D extrusion + 2D OpenStreetMap overlay |
| Surge prediction | ❌ reactive only | ✅ 5–10 min ahead from match state + weather |
| Closes the loop with fans | ❌ | ✅ AgentMail 2-way, VT-scanned, auto-SOP |
| Monte Carlo probability bands | ❌ point estimates | ✅ P(crush), P(evac>10m) over 200 trials |
| Hypothetical What-If | ❌ | ✅ 6 perturbations × side-by-side compare |
| Crowd-sourced reports | ❌ | ✅ separate Fan App, points/badges, auto-routed |
| Post-event privacy report | ❌ raw logs | ✅ de-identified, zone-family generalised |
| Voice-driven ops | ❌ | ✅ Vapi web widget + live context injection |

**One-line differentiator**: heatmap dashboards *describe* what's happening;
CrowdSync *acts*, by closing the demand-side loop (nudging fans to spread out
before bottlenecks form) and surfacing probability-banded futures the operator
can poke with What-If.

## 2. vs generic "Cloud Run + agents" hackathon demos

Most submissions you'll see are a thin wrapper: `gradio + openai-agents` →
"three agents talk to each other in a single prompt chain." CrowdSync is
structurally different in three ways:

1. **No agent-to-agent free messaging.** All cross-agent flow funnels through
   the Commander, which means the call graph is acyclic, debuggable, and
   auditable. The Commander writes one canonical incident; every other agent
   hands it structured input. (See `docs/ARCHITECTURE.md §2`.)

2. **Per-subsystem TTL gating with a global concurrency cap.** Streamlit's
   autorefresh is the heartbeat; `maybe_run(name, ttl, fn, expensive)` ensures
   only one expensive LLM/web call fires per 5 s tick, even if three are stale.
   This is what makes the live dashboard survive free-tier quotas during a
   live demo. Most hackathon demos fall over after 60 seconds because every
   widget re-fires every refresh.

3. **Two clients, one data plane.** The Fan App at `/Fan_App` is a sibling
   Streamlit page using the *same* `data/fan_reports.json` and *same*
   `commander.log_incident()`. No microservices, no message bus, no
   duplicate state. The operator and the fan are looking at the same store
   from different angles. Production would split this into a mobile app +
   REST endpoint, but the data contract doesn't change.

## 3. vs ticketing-app add-ons (Paytm, BookMyShow features)

| | Ticketing add-ons | CrowdSync |
|---|---|---|
| User identification | Phone-verified ticket holder | Anonymous handle (Fan App) |
| Surveillance posture | Tracks every user | Aggregates by zone, never per-user |
| What it sees | Purchases, seat assignment | Reports the user files |
| Operator side | None | Full ops dashboard with SOPs |
| Privacy default | Opt-out | Opt-in by design (no PII required to file a report) |

Ticketing apps win at *known-user personalisation* but they cannot reach
non-ticketed staff, volunteers, or attendees who bought from a third party.
CrowdSync's Fan App accepts anyone with a phone and a willingness to
contribute — which is the right surface for a distributed sensor network.

## 4. vs academic crowd simulators (e.g. Vadere, MassMotion, PedSim)

Academic simulators are physics-faithful — they model individual agent
trajectories with social force, collision dynamics, and per-step
acceleration. They run *offline* over hours. CrowdSync's Monte Carlo runs
*online* every 5 s with:

- A coarser model (per-zone density samples + per-gate throughput jitter)
- A faster engine (200 trials × <1 ms = under one tick)
- A wider input surface (live match state + weather + threat intel feeds)

The trade-off is deliberate. An operator at minute 8 of the 2nd innings does
not need agent-level trajectories; they need *probability bands fast enough to
re-run when something changes.* See `docs/MATHS.md §3-§5` for the sampling
model and convergence analysis.

## 5. vs another team in the same hackathon

Here's the brutal version. If another team builds something with a similar UI
and the same stadium (M. Chinnaswamy), they likely have:

- A heatmap on a 2D map ✓ (we have this + 3D + What-If overlay)
- A chat with the "Commander" ✓ (we have this + Vapi voice)
- A static SOP library ✓ (we have this + Gemini-generated action plans
  per-incident)
- Email/SMS to "fans" ✓ (we have this + VT URL pre-scan + auto-route inbound
  replies into the incident pipeline)

What they almost certainly don't have:

- **VirusTotal pre-LLM scanning** on every inbound URL — this is a niche
  defense most teams skip because it's invisible until you actually get
  attacked.
- **Monte Carlo probability bands** with risk-level-scaled variance —
  most teams ship a single deterministic forecast.
- **A separate fan-facing client at `/Fan_App`** sharing the same data plane.
- **A privacy de-identification module** for post-event analysis.
- **A live OpenStreetMap overlay** showing real stadium geography (most teams
  use abstract topology only).
- **An audit trail** of every agent decision in a separate Supabase table,
  with pgvector similarity search across past incidents.

Six features, six chances to win the rubric's *Functional Fulfillment* and
*Static Code Analysis* sections.

## 6. What we are NOT trying to be

We are not building Palantir-for-stadiums or a full safety-of-life system.
What we are showing is *the integration pattern*: how a multi-agent platform
can sit between live signals (CCTV, scoreboard, weather, news, fan reports),
a probability-aware forecaster, and a closed-loop action surface (email,
voice, PA). The 3-hour build window means most subsystems are MVP-depth, but
the integration *shape* is correct — every gap (better simulator, real
RTSP camera feed, $k$-anonymity gate, mobile app) is documented and slot-in
replaceable.

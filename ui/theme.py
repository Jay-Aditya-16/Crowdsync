"""Rounded neumorphic-cream theme — visual aesthetic ported from the
financial-dashboard reference shared by the user. Cream background,
pure white cards with deep rounded corners and soft shadows, coral
orange accent, bold black headings and pill-shaped action chips.
Layout favours horizontal pill rows over vertical stacks.
"""
from __future__ import annotations

import streamlit as st

# Palette
INK = "#0A0A0A"           # bold black for headings, primary buttons
PAPER = "#EFEDE7"         # warm cream page background
CARD = "#FFFFFF"          # card background
ACCENT = "#E85A3B"        # coral/orange — primary actions, key numbers
ACCENT_SOFT = "#F4D7CC"   # tinted fills, hover states
DIM = "#8E8C87"           # muted labels
RULE = "#E5E2DA"          # subtle dividers

# Re-exports so other modules can pick up the palette.
CARD_BG = CARD
CARD_INK = INK
CARD_DIM = DIM


def inject_css() -> None:
    """Inject the rounded neumorphic-cream stylesheet."""
    css = f"""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">

<style>
:root {{
  --ink: {INK};
  --paper: {PAPER};
  --card: {CARD};
  --accent: {ACCENT};
  --accent-soft: {ACCENT_SOFT};
  --dim: {DIM};
  --rule: {RULE};
  --shadow: 0 4px 18px rgba(20,20,20,0.06), 0 1px 2px rgba(20,20,20,0.04);
  --radius: 22px;
  --radius-lg: 28px;
  --radius-pill: 999px;
}}

html, body, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {{
  background: var(--paper) !important;
  color: var(--ink) !important;
  font-family: 'DM Sans', 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
}}
[data-testid="stHeader"] {{ background: transparent !important; box-shadow: none !important; }}

/* Increase outer padding so the dashboard breathes like the reference */
.block-container {{
  padding-top: 1.4rem !important;
  padding-left: 2.2rem !important;
  padding-right: 2.2rem !important;
  max-width: 100% !important;
}}

/* Headings */
h1 {{ font-family: 'DM Sans', sans-serif !important; font-weight: 700 !important; letter-spacing: -0.02em; color: var(--ink); font-size: 2.4rem !important; margin: 0 !important; }}
h2 {{ font-family: 'DM Sans', sans-serif !important; font-weight: 700 !important; color: var(--ink); font-size: 1.4rem !important; }}
h3 {{ font-family: 'DM Sans', sans-serif !important; font-weight: 700 !important; color: var(--ink); font-size: 1.05rem !important; margin: 12px 0 8px 0 !important; }}

/* Body + captions */
p, span, label, li {{ color: var(--ink); }}
[data-testid="stCaptionContainer"], small {{
  color: var(--dim) !important;
  font-size: 0.82rem !important;
}}

/* Metrics — bold black numbers, subtle label */
[data-testid="stMetricValue"] {{
  font-family: 'DM Sans', sans-serif !important;
  font-weight: 700 !important;
  color: var(--ink) !important;
  font-size: 1.7rem !important;
}}
[data-testid="stMetricLabel"] {{
  color: var(--dim) !important;
  font-size: 0.78rem !important;
  text-transform: none !important;
  letter-spacing: 0.01em;
}}
[data-testid="stMetricDelta"] {{ font-weight: 600 !important; }}

/* Buttons — rounded pill, dark fill, orange accent for primary */
.stButton > button, .stDownloadButton > button {{
  background: var(--ink) !important;
  color: #FFFFFF !important;
  border: none !important;
  border-radius: var(--radius-pill) !important;
  font-family: 'DM Sans', sans-serif !important;
  font-weight: 600 !important;
  letter-spacing: 0.01em;
  font-size: 0.88rem !important;
  padding: 9px 18px !important;
  box-shadow: var(--shadow) !important;
  transition: transform 0.08s ease, background 0.12s ease;
}}
.stButton > button:hover, .stDownloadButton > button:hover {{
  background: var(--accent) !important;
  transform: translateY(-1px);
}}
/* Form submit (primary-ish) — accent */
button[kind="primaryFormSubmit"], button[kind="primary"] {{
  background: var(--accent) !important;
  color: #FFFFFF !important;
}}

/* Inputs — rounded pill, white fill */
input, textarea {{
  background: var(--card) !important;
  color: var(--ink) !important;
  border-radius: 14px !important;
  border: 1px solid var(--rule) !important;
}}
[data-baseweb="input"], [data-baseweb="textarea"] {{
  border-radius: 14px !important;
  background: var(--card) !important;
  box-shadow: var(--shadow);
}}
[data-baseweb="select"] > div {{
  background: var(--card) !important;
  border-radius: 14px !important;
  border: 1px solid var(--rule) !important;
  box-shadow: var(--shadow);
}}

/* Expanders — rounded white card */
[data-testid="stExpander"] {{
  background: var(--card) !important;
  border: 1px solid var(--rule) !important;
  border-radius: var(--radius) !important;
  box-shadow: var(--shadow) !important;
  overflow: hidden;
}}
[data-testid="stExpander"] details summary {{
  background: transparent !important;
  font-weight: 600;
  padding: 12px 16px !important;
}}

/* Tabs — rounded pill list */
[data-baseweb="tab-list"] {{
  background: var(--card);
  border-radius: var(--radius-pill);
  padding: 4px;
  box-shadow: var(--shadow);
  display: inline-flex !important;
  border: none !important;
  gap: 4px;
}}
[data-baseweb="tab"] {{
  background: transparent !important;
  color: var(--dim) !important;
  border-radius: var(--radius-pill) !important;
  font-weight: 600;
  padding: 8px 18px !important;
  font-size: 0.88rem;
}}
[data-baseweb="tab"][aria-selected="true"] {{
  color: #FFFFFF !important;
  background: var(--ink) !important;
}}

/* Sidebar — soft white card */
[data-testid="stSidebar"] {{
  background: var(--card) !important;
  border-right: 1px solid var(--rule);
}}
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] {{ color: var(--ink); }}

/* Dividers and alerts */
hr {{ border-color: var(--rule) !important; }}
[data-baseweb="notification"], .stAlert {{ border-radius: var(--radius) !important; }}

/* Chat */
[data-testid="stChatMessage"] {{
  background: var(--card) !important;
  border: 1px solid var(--rule);
  border-radius: var(--radius) !important;
  box-shadow: var(--shadow);
}}
[data-testid="stChatInput"] textarea {{ border-radius: var(--radius-pill) !important; }}

/* Links */
a {{ color: var(--accent) !important; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}

/* Plotly chart background blends with cards */
.js-plotly-plot, .plotly {{ background: transparent !important; }}

/* Helper utility classes used by the inline header / pill cards below */
.csyn-card {{
  background: var(--card);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 14px 18px;
}}
.csyn-pill {{
  background: var(--card);
  border-radius: var(--radius-pill);
  box-shadow: var(--shadow);
  padding: 8px 18px;
  display: inline-flex; align-items: center; gap: 12px;
}}
.csyn-chip-dark {{
  background: var(--ink); color: #FFF;
  border-radius: var(--radius-pill);
  padding: 6px 14px; font-weight: 600; font-size: 0.85rem;
}}
.csyn-chip-accent {{
  background: var(--accent); color: #FFF;
  border-radius: var(--radius-pill);
  padding: 8px 18px; font-weight: 600; font-size: 0.9rem;
}}
.csyn-eyebrow {{
  color: var(--dim); font-size: 0.75rem; letter-spacing: 0.02em;
  font-weight: 500; text-transform: none;
}}
</style>
"""
    st.markdown(css, unsafe_allow_html=True)


def render_header(operator_name: str, operator_role: str) -> None:
    """Horizontal header bar — logo chip on left, profile + greeting on right."""
    st.markdown(
        f"""
<div style="display:flex; align-items:center; justify-content:space-between; gap:18px; flex-wrap:wrap; margin: 4px 0 18px 0;">

  <div style="display:flex; align-items:center; gap:14px;">
    <div style="width:46px; height:46px; background:{INK}; color:#FFF; border-radius:50%;
                display:flex; align-items:center; justify-content:center; font-weight:800; font-size:1.1rem; box-shadow: 0 4px 18px rgba(20,20,20,0.08);">
      🏟️
    </div>
    <div>
      <div class="csyn-eyebrow">Stadium Command</div>
      <div style="font-weight:700; font-size:1.25rem; color:{INK}; line-height:1.1;">CrowdSync</div>
    </div>
  </div>

  <div class="csyn-pill" style="min-width: 240px;">
    <div style="width:34px; height:34px; background:{ACCENT_SOFT}; border-radius:50%;
                display:flex; align-items:center; justify-content:center; font-size:1rem;">👤</div>
    <div style="line-height:1.15;">
      <div style="font-weight:700; color:{INK};">{operator_name}</div>
      <div style="color:{DIM}; font-size:0.78rem;">{operator_role}</div>
    </div>
  </div>

</div>

<div style="display:flex; align-items:center; gap:18px; flex-wrap:wrap; margin-bottom: 18px;">
  <div style="font-weight:700; font-size:2rem; line-height:1.1; color:{INK};">
    Live ops dashboard
    <span style="color:{ACCENT};">·</span>
    <span style="color:{DIM}; font-weight:500;">how's the stadium today?</span>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

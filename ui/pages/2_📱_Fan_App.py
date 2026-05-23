"""Fan-facing page — what an attendee opens on their phone.

Lite stand-in for the would-be mobile app. No operator auth; just enter
your name and report what you see. Earn points, climb the leaderboard.
Submissions feed straight into the same incident pipeline the response
team uses.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import streamlit as st
from agents import fan_reports
from ui.theme import inject_css, INK, ACCENT, ACCENT_SOFT, DIM, CARD, PAPER

st.set_page_config(page_title="Stadnium AI — Fan App", layout="centered", page_icon="📱")
inject_css()

# ---------- Phone-shaped header ----------
st.markdown(
    f"""
<div style="display:flex; align-items:center; gap:14px; margin: 4px 0 12px 0;">
  <div style="width:44px; height:44px; background:{ACCENT}; color:#FFF; border-radius:50%;
              display:flex; align-items:center; justify-content:center; font-weight:800; font-size:1.1rem;
              box-shadow: 0 4px 18px rgba(232,90,59,0.25);">📱</div>
  <div>
    <div style="color:{DIM}; font-size:0.78rem;">Stadnium AI</div>
    <div style="font-weight:700; font-size:1.5rem; color:{INK}; line-height:1.1;">Fan App</div>
  </div>
</div>
<div style="color:{DIM}; font-size:0.95rem; margin-bottom:18px;">
  See something? Say something. Earn points for spotting issues — urgent reports go straight to the response team.
</div>
""",
    unsafe_allow_html=True,
)

# ---------- Lite "login" — just pick a handle ----------
handle = st.session_state.get("fan_handle", "")
if not handle:
    with st.container():
        st.markdown(f"<div class='csyn-card' style='margin-bottom:16px;'>", unsafe_allow_html=True)
        st.markdown("**👤 Pick a handle to start**")
        handle_input = st.text_input("Your handle (any name)", value="", placeholder="e.g. fan_ravi")
        if st.button("Continue", use_container_width=True, key="fan_handle_btn"):
            if handle_input.strip():
                st.session_state.fan_handle = handle_input.strip()
                st.rerun()
            else:
                st.warning("Give yourself a handle first.")
        st.markdown("</div>", unsafe_allow_html=True)
    st.stop()

# ---------- Your points / badge ----------
my_reports = [r for r in fan_reports._read_reports() if r.get("reporter_id") == handle]
my_points = sum(r.get("points_awarded", 0) for r in my_reports)
my_badge = fan_reports._badge_for(my_points)

st.markdown(
    f"""
<div class="csyn-card" style="display:flex; align-items:center; justify-content:space-between; margin-bottom:18px;">
  <div>
    <div style="color:{DIM}; font-size:0.78rem;">Signed in as</div>
    <div style="font-weight:700; font-size:1.2rem; color:{INK};">{handle}</div>
    <div style="color:{ACCENT}; font-size:0.85rem; margin-top:2px;">{my_badge}</div>
  </div>
  <div style="text-align:right;">
    <div style="color:{DIM}; font-size:0.78rem;">Your points</div>
    <div style="font-weight:800; font-size:2rem; color:{INK};">{my_points}</div>
    <div style="color:{DIM}; font-size:0.78rem;">{len(my_reports)} report{'s' if len(my_reports)!=1 else ''}</div>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

# ---------- Submission form ----------
st.markdown("### Report what you see")
with st.form("fan_submit_form", clear_on_submit=True):
    category = st.selectbox(
        "What kind of issue?",
        list(fan_reports.CATEGORY_POINTS.keys()),
        index=0,
    )
    zone = st.text_input("Where are you? (section or gate)", value="A_STAND",
                          help="e.g. A_STAND, G14, P_CORPORATE")
    summary_text = st.text_area(
        "What did you see?",
        height=90,
        placeholder="Bottleneck forming near restrooms behind row 18…",
    )
    verified = st.toggle(
        "A volunteer near me has confirmed this",
        value=False,
        help="Tick this only if a stadium volunteer or staff has confirmed on-site (2× points).",
    )

    submitted = st.form_submit_button("📤 Submit report", use_container_width=True)
    if submitted:
        if not summary_text.strip():
            st.warning("Add a quick description before submitting.")
        else:
            rec = fan_reports.submit_report(
                reporter_id=handle,
                category=category,
                zone=zone,
                summary=summary_text,
                verified=verified,
            )
            if rec.get("routed_to_commander"):
                st.success(
                    f"🚨 +{rec['points_awarded']} points. **Sent to the response team.** "
                    f"Stay safe and clear the area if it's not safe."
                )
            else:
                st.success(
                    f"✅ +{rec['points_awarded']} points. Thanks — we've logged it."
                )
            st.rerun()

# ---------- Your recent submissions ----------
if my_reports:
    st.markdown("### Your recent reports")
    for r in reversed(my_reports[-5:]):
        tag = "🚨" if r.get("routed_to_commander") else ("✅" if r.get("verified") else "•")
        st.markdown(
            f"<div style='padding:10px 14px; background:#FFFFFF; border-radius:18px; "
            f"box-shadow:0 4px 18px rgba(20,20,20,0.06); border-left:3px solid {ACCENT}; "
            f"color:#0A0A0A; font-size:13px; margin-bottom:6px;'>"
            f"{tag} <b>{r['category']}</b> in {r['zone']} · "
            f"<span style='font-weight:700;'>+{r['points_awarded']}</span> pts · "
            f"{r['submitted_at']}<br>"
            f"<span style='color:{DIM}; font-size:12px;'>{r['summary']}</span></div>",
            unsafe_allow_html=True,
        )

# ---------- Leaderboard ----------
st.markdown("### 🏆 Top reporters today")
leaders = fan_reports.get_leaderboard(top_n=5)
for rank, p in enumerate(leaders, 1):
    is_me = p["reporter_id"] == handle
    bg = ACCENT_SOFT if is_me else CARD
    st.markdown(
        f"<div style='padding:10px 14px; background:{bg}; border-radius:18px; "
        f"box-shadow:0 4px 18px rgba(20,20,20,0.06); color:#0A0A0A; font-size:13px; margin-bottom:6px;'>"
        f"<span style='color:{DIM};'>#{rank:02d}</span> "
        f"<b>{p['reporter_id']}</b>{' (you)' if is_me else ''} · "
        f"<span style='font-weight:700;'>{p['points']}</span> pts · "
        f"<i style='color:{DIM};'>{p['badge']}</i></div>",
        unsafe_allow_html=True,
    )

# ---------- Sign out ----------
st.markdown("<div style='margin-top:24px;'>", unsafe_allow_html=True)
if st.button("Sign out", use_container_width=False):
    st.session_state.pop("fan_handle", None)
    st.rerun()
st.markdown("</div>", unsafe_allow_html=True)

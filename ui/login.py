"""Login page for CrowdSync.

Lightweight gate — operator enters their name, email, and role. Email is
used as the recipient for every alert/escalation the Commander Agent
generates during the session. No password verification (hackathon demo
scope) but session start is logged to Supabase for audit.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import streamlit as st

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROLES = ["Stadium Ops Lead", "Security Commander", "Crowd Marshal", "Medical Lead", "Demo / Observer"]


def render_login() -> bool:
    """Render the login form. Returns True iff the user is authenticated."""
    if st.session_state.get("authenticated"):
        return True

    st.set_page_config(page_title="CrowdSync — Sign in", layout="centered", page_icon="🏟️")

    st.markdown(
        """
        <div style="text-align:center; margin-top:20px;">
          <h1 style="margin-bottom:0;">🏟️ CrowdSync</h1>
          <div style="color:#9BB0C4; margin-bottom:24px;">
            Multi-agent stadium command platform · M. Chinnaswamy Stadium, Bengaluru
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.form("login", clear_on_submit=False):
        st.subheader("Sign in to start a session")
        name = st.text_input("Name", placeholder="e.g. Priya Iyer")
        email = st.text_input(
            "Email (receives all alerts and escalations during this session)",
            placeholder="you@example.com",
        )
        role = st.selectbox("Role", options=ROLES, index=0)
        password = st.text_input("Password", type="password", placeholder="any value — demo mode")
        col1, col2 = st.columns(2)
        submit = col1.form_submit_button("➡ Enter dashboard", use_container_width=True, type="primary")
        demo = col2.form_submit_button("⚡ Quick demo (use jaygamertak@gmail.com)", use_container_width=True)

    if demo:
        _authenticate(name="Demo Operator", email="jaygamertak@gmail.com", role="Demo / Observer")
        st.rerun()

    if submit:
        if not name.strip():
            st.error("Please enter your name.")
            return False
        if not EMAIL_RE.match(email.strip()):
            st.error("Please enter a valid email address.")
            return False
        if not password:
            st.error("Please enter any password (demo mode — not verified).")
            return False
        _authenticate(name=name.strip(), email=email.strip().lower(), role=role)
        st.rerun()

    st.caption(
        "🛡 Demo authentication — password is not checked. Email entered will "
        "receive all alerts, lost-child notifications, and escalations. "
        "Session logged to Supabase audit trail."
    )
    return False


def _authenticate(name: str, email: str, role: str) -> None:
    st.session_state.authenticated = True
    st.session_state.operator_name = name
    st.session_state.operator_email = email
    st.session_state.operator_role = role
    st.session_state.session_started_at = time.time()

    # Update Commander recipient + log session start to Supabase audit
    try:
        from agents import commander
        commander.set_operator_email(email)
    except Exception as e:
        print(f"[login] could not set operator email: {e}")

    try:
        from tools import supabase_client
        if supabase_client.is_enabled():
            supabase_client.log_agent_decision(
                agent_name="auth",
                action="session_start",
                reasoning=f"{name} <{email}> signed in as {role}",
                payload={"name": name, "email": email, "role": role},
            )
    except Exception as e:
        print(f"[login] audit log error: {e}")


def render_logout_button() -> None:
    """Sidebar logout. Clears the operator session."""
    if st.button("🚪 Log out", use_container_width=True):
        try:
            from agents import commander
            commander.set_operator_email("")  # back to default / no recipient
        except Exception:
            pass
        for key in ("authenticated", "operator_name", "operator_email", "operator_role", "session_started_at"):
            st.session_state.pop(key, None)
        st.rerun()

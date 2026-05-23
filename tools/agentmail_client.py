"""Thin wrapper around AgentMail SDK.

Exposes the minimum surface CrowdSync agents need: get-or-create inbox,
send, list unread, reply, mark read.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

from agentmail import AgentMail
from dotenv import load_dotenv

load_dotenv()


@dataclass
class IncomingMessage:
    message_id: str
    thread_id: Optional[str]
    inbox_id: str
    sender: str
    subject: str
    body: str
    received_at: Optional[str]


class AgentMailClient:
    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.getenv("AGENTMAIL_API_KEY")
        if not key:
            raise RuntimeError("AGENTMAIL_API_KEY not set")
        self.client = AgentMail(api_key=key)
        self._inbox_cache: dict[str, str] = {}

    def find_inbox_by_address(self, address: str) -> Optional[str]:
        """Look up an existing inbox by its email address.

        AgentMail uses the email address itself as the inbox_id, but we
        verify membership via list() to avoid silently using an address
        the API key doesn't own.
        """
        try:
            resp = self.client.inboxes.list(limit=50)
        except Exception as e:
            print(f"[AgentMail] list error: {e}")
            return None
        inboxes = getattr(resp, "inboxes", []) or []
        for inbox in inboxes:
            inbox_id = getattr(inbox, "inbox_id", "")
            if inbox_id == address:
                return inbox_id
        return None

    def first_existing_inbox(self) -> Optional[str]:
        try:
            resp = self.client.inboxes.list(limit=1)
        except Exception:
            return None
        inboxes = getattr(resp, "inboxes", []) or []
        return inboxes[0].inbox_id if inboxes else None

    def get_or_create_inbox(self, client_id: str, prefer_address: Optional[str] = None) -> str:
        """Resolve an inbox id with three-tier fallback:
        1. Memoized cache for this client_id.
        2. Existing inbox matching `prefer_address` (lets us reuse user-owned inboxes
           when the create-inbox quota is exhausted).
        3. Create a new inbox.
        4. Last resort: first existing inbox.
        """
        if client_id in self._inbox_cache:
            return self._inbox_cache[client_id]

        if prefer_address:
            existing = self.find_inbox_by_address(prefer_address)
            if existing:
                self._inbox_cache[client_id] = existing
                return existing

        try:
            inbox = self.client.inboxes.create(client_id=client_id)
            self._inbox_cache[client_id] = inbox.inbox_id
            return inbox.inbox_id
        except Exception as e:
            print(f"[AgentMail] create failed ({e}); falling back to first existing inbox")
            existing = self.first_existing_inbox()
            if existing:
                self._inbox_cache[client_id] = existing
                return existing
            raise

    def inbox_address(self, inbox_id: str) -> str:
        """Return the email address for an inbox.

        AgentMail uses the address as the inbox_id, so the id IS the address.
        Falls back to API lookup if needed.
        """
        if "@" in inbox_id:
            return inbox_id
        try:
            inbox = self.client.inboxes.get(inbox_id=inbox_id)
            return getattr(inbox, "inbox_id", inbox_id)
        except Exception:
            return inbox_id

    def send(
        self,
        inbox_id: str,
        to: str | list[str],
        subject: str,
        text: str,
        html: Optional[str] = None,
        labels: Optional[list[str]] = None,
    ) -> Optional[str]:
        recipients = [to] if isinstance(to, str) else to
        try:
            resp = self.client.inboxes.messages.send(
                inbox_id=inbox_id,
                to=recipients,
                subject=subject,
                text=text,
                html=html or f"<p>{text}</p>",
                labels=labels or [],
            )
            return getattr(resp, "message_id", None)
        except Exception as e:
            print(f"[AgentMail] send error: {e}")
            return None

    def list_unread(self, inbox_id: str, limit: int = 10) -> list[IncomingMessage]:
        try:
            resp = self.client.inboxes.messages.list(
                inbox_id=inbox_id,
                labels=["unread"],
                limit=limit,
            )
        except Exception:
            resp = self.client.inboxes.messages.list(inbox_id=inbox_id, limit=limit)

        msgs = getattr(resp, "messages", []) or []
        out: list[IncomingMessage] = []
        for m in msgs:
            sender = getattr(m, "from_", None) or getattr(m, "sender", "") or ""
            body = getattr(m, "extracted_text", None) or getattr(m, "text", "") or ""
            out.append(IncomingMessage(
                message_id=getattr(m, "message_id", ""),
                thread_id=getattr(m, "thread_id", None),
                inbox_id=inbox_id,
                sender=str(sender),
                subject=getattr(m, "subject", "") or "",
                body=body,
                received_at=str(getattr(m, "created_at", "") or getattr(m, "received_at", "")),
            ))
        return out

    def reply(
        self,
        inbox_id: str,
        message_id: str,
        text: str,
        html: Optional[str] = None,
    ) -> Optional[str]:
        try:
            resp = self.client.inboxes.messages.reply(
                inbox_id=inbox_id,
                message_id=message_id,
                text=text,
                html=html or f"<p>{text}</p>",
            )
            return getattr(resp, "message_id", None)
        except Exception as e:
            print(f"[AgentMail] reply error: {e}")
            return None

    def mark_read(self, inbox_id: str, message_id: str) -> None:
        try:
            self.client.inboxes.messages.update(
                inbox_id=inbox_id,
                message_id=message_id,
                add_labels=["read"],
                remove_labels=["unread"],
            )
        except Exception as e:
            print(f"[AgentMail] mark_read error: {e}")

    def list_recent(self, inbox_id: str, limit: int = 20) -> list[IncomingMessage]:
        """All recent messages (read + unread) for the dashboard view."""
        try:
            resp = self.client.inboxes.messages.list(inbox_id=inbox_id, limit=limit)
        except Exception as e:
            print(f"[AgentMail] list_recent error: {e}")
            return []
        msgs = getattr(resp, "messages", []) or []
        out: list[IncomingMessage] = []
        for m in msgs:
            sender = getattr(m, "from_", None) or getattr(m, "sender", "") or ""
            body = getattr(m, "extracted_text", None) or getattr(m, "text", "") or ""
            out.append(IncomingMessage(
                message_id=getattr(m, "message_id", ""),
                thread_id=getattr(m, "thread_id", None),
                inbox_id=inbox_id,
                sender=str(sender),
                subject=getattr(m, "subject", "") or "",
                body=body,
                received_at=str(getattr(m, "created_at", "") or getattr(m, "received_at", "")),
            ))
        return out


_singleton: Optional[AgentMailClient] = None

def get_client() -> AgentMailClient:
    global _singleton
    if _singleton is None:
        _singleton = AgentMailClient()
    return _singleton

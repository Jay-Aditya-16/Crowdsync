-- CrowdSync schema. Idempotent — safe to re-run.
--
-- Run via: psycopg connect using SUPABASE_DB_URL, then \i this file.
-- (tools/supabase_client.py auto-applies it on first start.)

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1. Incidents — Commander Agent decisions, audit-grade.
CREATE TABLE IF NOT EXISTS incidents (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    legacy_id        TEXT,
    type             TEXT NOT NULL,
    severity         TEXT NOT NULL,
    zone             TEXT,
    summary          TEXT,
    plan             TEXT,
    source           TEXT,
    payload          JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS incidents_created_at_idx ON incidents (created_at DESC);
CREATE INDEX IF NOT EXISTS incidents_severity_idx ON incidents (severity);

-- 2. Agent decisions — every reasoning step logged for compliance.
CREATE TABLE IF NOT EXISTS agent_decisions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_name   TEXT NOT NULL,
    action       TEXT NOT NULL,
    reasoning    TEXT,
    confidence   REAL,
    payload      JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS agent_decisions_created_at_idx ON agent_decisions (created_at DESC);
CREATE INDEX IF NOT EXISTS agent_decisions_agent_idx ON agent_decisions (agent_name);

-- 3. Tickets registry (replaces tickets.json).
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id      TEXT PRIMARY KEY,
    name           TEXT,
    email          TEXT,
    zone           TEXT,
    seat           TEXT,
    gate_assigned  TEXT,
    language       TEXT DEFAULT 'en',
    is_demo        BOOLEAN DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS tickets_zone_idx ON tickets (zone);
CREATE INDEX IF NOT EXISTS tickets_demo_idx ON tickets (is_demo);

-- 4. Fan message log — every email handled by Fan Concierge.
CREATE TABLE IF NOT EXISTS fan_messages_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      TEXT,
    direction       TEXT NOT NULL,          -- 'inbound' or 'outbound'
    from_addr       TEXT,
    to_addr         TEXT,
    subject         TEXT,
    body_preview    TEXT,
    category        TEXT,
    severity        TEXT,
    security_verdict JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS fan_messages_log_created_at_idx ON fan_messages_log (created_at DESC);

-- Permissive RLS for the demo. Production would add stricter policies + service role.
ALTER TABLE incidents          ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_decisions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE tickets            ENABLE ROW LEVEL SECURITY;
ALTER TABLE fan_messages_log   ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='incidents' AND policyname='anon_all') THEN
    CREATE POLICY anon_all ON incidents          FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='agent_decisions' AND policyname='anon_all') THEN
    CREATE POLICY anon_all ON agent_decisions    FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='tickets' AND policyname='anon_all') THEN
    CREATE POLICY anon_all ON tickets            FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='fan_messages_log' AND policyname='anon_all') THEN
    CREATE POLICY anon_all ON fan_messages_log   FOR ALL TO anon, authenticated USING (true) WITH CHECK (true);
  END IF;
END $$;

-- Program lane runs started from the hosted dashboard (seed + verify as one subprocess job).
-- Separate from `jobs`, which belong to a campaign (radar); the program lane has none.
CREATE TABLE program_runs (
  id TEXT PRIMARY KEY,
  trigger TEXT NOT NULL CHECK (trigger IN ('manual','daily')),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','done','failed','interrupted','skipped')),
  max_searches INTEGER NOT NULL DEFAULT 200,
  credits_used INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  log TEXT NOT NULL DEFAULT '',
  exit_code INTEGER,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX program_runs_status ON program_runs (status, created_at);

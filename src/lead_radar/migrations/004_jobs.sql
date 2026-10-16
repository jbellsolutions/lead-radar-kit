-- Hunt jobs started from the hosted dashboard (manual "Run now" or the daily trigger). One runs at a time.
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  radar_id TEXT NOT NULL REFERENCES radars(id),
  trigger TEXT NOT NULL CHECK (trigger IN ('manual','daily')),
  days INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued','running','done','failed','interrupted','skipped')),
  note TEXT NOT NULL DEFAULT '',
  run_id TEXT,
  exit_code INTEGER,
  log TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX jobs_status ON jobs (status, created_at);

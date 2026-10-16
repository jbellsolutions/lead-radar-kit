-- Job-board listings (OnlineJobs.ph, We Work Remotely, Freelancer, Indeed). Listings are stored in `posts`
-- (kind 'job') but never enter the M3 lead-intent cascade: they have their own queue and verdicts.
CREATE TABLE listing_queue (
  post_id TEXT NOT NULL REFERENCES posts(id),
  radar_id TEXT NOT NULL REFERENCES radars(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done','failed')),
  enqueued_at TEXT NOT NULL,
  PRIMARY KEY (post_id, radar_id)
);
CREATE INDEX listing_queue_pending ON listing_queue (radar_id, state);

CREATE TABLE listing_verdicts (
  post_id TEXT NOT NULL REFERENCES posts(id),
  radar_id TEXT NOT NULL REFERENCES radars(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  employer_type TEXT NOT NULL CHECK (employer_type IN ('direct','staffing','recruiter','relay','unknown')),
  fit TEXT NOT NULL CHECK (fit IN ('high','medium','low','none')),
  role_title TEXT NOT NULL DEFAULT '',
  company_name TEXT,                 -- only when it appears verbatim in the listing (or the board names it)
  need TEXT NOT NULL DEFAULT '',
  evidence_quote TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0,
  decided_by TEXT NOT NULL CHECK (decided_by IN ('rules','reasoning')),
  reason TEXT,                       -- rules: why it was ruled out
  contract_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (post_id, radar_id)
);
CREATE INDEX listing_verdicts_fit ON listing_verdicts (radar_id, fit);

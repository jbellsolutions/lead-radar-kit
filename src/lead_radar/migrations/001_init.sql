-- Lead Radar v2 initial schema. JSON columns hold pydantic-serialized values.
CREATE TABLE radars (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  query TEXT NOT NULL,
  offer_context TEXT,
  platforms TEXT NOT NULL,            -- JSON list
  lookback_days INTEGER NOT NULL DEFAULT 14,
  intent_brief_id TEXT,
  schedule TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE intent_briefs (
  id TEXT PRIMARY KEY,
  radar_id TEXT NOT NULL REFERENCES radars(id),
  version INTEGER NOT NULL,
  body TEXT NOT NULL,                 -- JSON IntentBrief
  created_at TEXT NOT NULL,
  UNIQUE (radar_id, version)
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  radar_id TEXT NOT NULL REFERENCES radars(id),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  per_source TEXT NOT NULL DEFAULT '{}',
  credits_used INTEGER NOT NULL DEFAULT 0,
  browser_minutes REAL NOT NULL DEFAULT 0,
  jev_calls INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens_in INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  counts TEXT NOT NULL DEFAULT '{}',
  qc_disagreement REAL,
  errors TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX runs_radar ON runs (radar_id, started_at);

CREATE TABLE posts (
  id TEXT PRIMARY KEY,                -- sha1(platform + canonical_url)
  platform TEXT NOT NULL,
  url TEXT NOT NULL,
  parent_url TEXT,
  kind TEXT NOT NULL,
  author_handle TEXT,
  author_url TEXT,
  author_name TEXT,
  text TEXT NOT NULL,
  title TEXT,
  posted_at TEXT,
  engagement TEXT NOT NULL DEFAULT '{}',
  community TEXT,
  source_term TEXT NOT NULL,
  run_id TEXT NOT NULL,               -- first run that saw it
  simhash INTEGER NOT NULL DEFAULT 0,
  raw TEXT NOT NULL DEFAULT '{}',
  parent_text TEXT,
  first_seen TEXT NOT NULL
);
CREATE INDEX posts_author ON posts (platform, author_handle);

-- Which posts each run fetched (a post can be seen by many runs/radars).
CREATE TABLE run_posts (
  run_id TEXT NOT NULL REFERENCES runs(id),
  post_id TEXT NOT NULL REFERENCES posts(id),
  PRIMARY KEY (run_id, post_id)
);

CREATE TABLE classifications (
  post_id TEXT NOT NULL REFERENCES posts(id),
  radar_id TEXT NOT NULL REFERENCES radars(id),
  role TEXT NOT NULL CHECK (role IN ('buyer','seller','other')),
  tier TEXT NOT NULL CHECK (tier IN ('high','medium','low','none')),
  need_summary TEXT NOT NULL,
  evidence_quote TEXT NOT NULL,
  urgency TEXT NOT NULL CHECK (urgency IN ('now','soon','someday','unknown')),
  confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  decided_by TEXT NOT NULL CHECK (decided_by IN ('rules','jev','jev_heuristic','reasoning')),
  freshness_downgrade INTEGER NOT NULL DEFAULT 0,
  contract_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (post_id, radar_id)
);
CREATE INDEX classifications_tier ON classifications (radar_id, tier);

-- Stage 0 drops, with the reason. A dropped post counts as "seen" for its radar.
CREATE TABLE drops (
  post_id TEXT NOT NULL REFERENCES posts(id),
  radar_id TEXT NOT NULL REFERENCES radars(id),
  run_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (post_id, radar_id)
);

CREATE TABLE leads (
  id TEXT NOT NULL,                   -- stable per person
  radar_id TEXT NOT NULL REFERENCES radars(id),
  display_name TEXT,
  handles TEXT NOT NULL DEFAULT '{}',
  profile_urls TEXT NOT NULL DEFAULT '{}',
  post_ids TEXT NOT NULL DEFAULT '[]',
  best_tier TEXT NOT NULL,
  score REAL NOT NULL,
  need_summary TEXT NOT NULL,
  evidence_quote TEXT NOT NULL,
  suggested_opener TEXT NOT NULL DEFAULT '',
  bio TEXT,
  website TEXT,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','approved','contacted','ignored')),
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  PRIMARY KEY (id, radar_id)
);
CREATE INDEX leads_score ON leads (radar_id, score DESC);

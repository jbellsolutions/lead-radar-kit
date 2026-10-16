-- Contact cards (Data Box `enrich`) keyed by subject: one person or company across every campaign.
-- Contact data is personal: it never goes into logs, run errors or the static dashboard, and each card is
-- deleted after RADAR_CONTACT_RETENTION_DAYS (purge_contacts).
ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'hunt';
ALTER TABLE jobs ADD COLUMN target_run_id TEXT;
ALTER TABLE jobs ADD COLUMN credits_used INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0;

CREATE TABLE contacts (
  subject TEXT PRIMARY KEY,           -- resolve.subject_key, e.g. linkedin:url:..., onlinejobs:employer:123
  status TEXT NOT NULL CHECK (status IN ('pending','enriched','unresolved','deferred','failed')),
  reason TEXT NOT NULL DEFAULT '',    -- why unresolved / deferred / failed (never contact data)
  name TEXT,
  company TEXT,
  domain TEXT,
  website TEXT,
  email TEXT,
  email_verdict TEXT,                 -- deliverable | accept_all | undeliverable | unknown | verify_quota
  alt_emails TEXT NOT NULL DEFAULT '[]',
  phone TEXT,
  linkedin_url TEXT,
  socials TEXT NOT NULL DEFAULT '{}',
  card TEXT NOT NULL DEFAULT '{}',    -- the full Data Box card: each field {value, source, confidence}
  sources TEXT NOT NULL DEFAULT '{}', -- resolution provenance: field -> where it was found
  attempts INTEGER NOT NULL DEFAULT 0,
  databox_job_id TEXT,
  credits_used INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX contacts_status ON contacts (status, updated_at);

CREATE TABLE contact_links (
  subject TEXT NOT NULL REFERENCES contacts(subject) ON DELETE CASCADE,
  post_id TEXT NOT NULL REFERENCES posts(id),
  radar_id TEXT NOT NULL REFERENCES radars(id),
  kind TEXT NOT NULL CHECK (kind IN ('lead','listing')),
  tier TEXT NOT NULL,                 -- lead tier or listing fit
  PRIMARY KEY (subject, post_id, radar_id)
);
CREATE INDEX contact_links_post ON contact_links (post_id, radar_id);

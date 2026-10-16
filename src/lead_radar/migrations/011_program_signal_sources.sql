-- Program lane, signals from every platform. program_signals gains the platform a signal came from and the new
-- kinds: paid_ad (a brand paying to recruit affiliates or ISOs), social_post (brand post on a social network) and
-- community_post (a founder thread on Reddit, Hacker News, Indie Hackers and the like).
-- SQLite cannot alter a CHECK constraint, so the table is rebuilt. Signal ids are copied unchanged.
CREATE TABLE program_signals_new (
  id TEXT PRIMARY KEY,
  domain TEXT NOT NULL REFERENCES program_companies(domain),
  kind TEXT NOT NULL CHECK (kind IN ('job','company_post','person_post','affiliate_staff','decision_maker',
                                   'partner_program','lookalike','paid_ad','social_post','community_post')),
  platform TEXT NOT NULL DEFAULT 'linkedin',
  url TEXT NOT NULL,
  quote TEXT NOT NULL DEFAULT '',
  signal_date TEXT,
  person_name TEXT,
  person_url TEXT,
  first_seen TEXT NOT NULL
);
INSERT INTO program_signals_new (id, domain, kind, platform, url, quote, signal_date, person_name, person_url, first_seen)
  SELECT id, domain, kind,
         CASE WHEN kind = 'job' THEN 'job_board' WHEN kind = 'partner_program' THEN 'web' ELSE 'linkedin' END,
         url, quote, signal_date, person_name, person_url, first_seen
  FROM program_signals;
DROP TABLE program_signals;
ALTER TABLE program_signals_new RENAME TO program_signals;
CREATE INDEX program_signals_domain ON program_signals (domain);
CREATE INDEX program_signals_seen ON program_signals (first_seen);

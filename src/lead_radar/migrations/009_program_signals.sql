-- Program lane, growth signals and the finance list.
-- program_companies gains which list it belongs to and its LinkedIn company profile (US filter, size, industry).
ALTER TABLE program_companies ADD COLUMN list TEXT NOT NULL DEFAULT 'affiliate';   -- affiliate | finance
ALTER TABLE program_companies ADD COLUMN linkedin_url TEXT;
ALTER TABLE program_companies ADD COLUMN country TEXT;                              -- from the LinkedIn company page
ALTER TABLE program_companies ADD COLUMN hq TEXT;
ALTER TABLE program_companies ADD COLUMN size TEXT;
ALTER TABLE program_companies ADD COLUMN industry TEXT;
ALTER TABLE program_companies ADD COLUMN excluded TEXT;                             -- why it is never listed (NULL = eligible)
ALTER TABLE program_companies ADD COLUMN profiled_at TEXT;
ALTER TABLE program_companies ADD COLUMN lead_score INTEGER NOT NULL DEFAULT 0;     -- growth-signal priority (lists)

-- One dated, sourced reason a company is worth calling now. A "new lead" is a signal first seen in the window.
CREATE TABLE program_signals (
  id TEXT PRIMARY KEY,                -- sha1(kind|url)
  domain TEXT NOT NULL REFERENCES program_companies(domain),
  kind TEXT NOT NULL CHECK (kind IN ('job','company_post','person_post','affiliate_staff','decision_maker',
                                   'partner_program','lookalike')),
  url TEXT NOT NULL,
  quote TEXT NOT NULL DEFAULT '',     -- verbatim (job title, post text, program page text)
  signal_date TEXT,                   -- when it happened (post/job date), if known
  person_name TEXT,                   -- personal data (staff and post authors only), never logged
  person_url TEXT,
  first_seen TEXT NOT NULL
);
CREATE INDEX program_signals_domain ON program_signals (domain);
CREATE INDEX program_signals_seen ON program_signals (first_seen);
CREATE INDEX program_companies_list ON program_companies (list, lead_score);

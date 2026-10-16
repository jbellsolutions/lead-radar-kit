-- Program lane: companies that run an affiliate/partner program (partnership prospects).
-- One row per company domain across every seed source. Company data only; contacts stay in `contacts`.
CREATE TABLE program_companies (
  domain TEXT PRIMARY KEY,
  name TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','verified','no_program','unreachable')),
  platforms TEXT NOT NULL DEFAULT '[]',      -- e.g. ["partnerstack"], ["in-house"]
  program_url TEXT,
  commission_text TEXT,                      -- verbatim from the program page
  evidence TEXT NOT NULL DEFAULT '[]',       -- [{platform, kind, match, slug, page}], verbatim matches
  segment TEXT NOT NULL DEFAULT 'unknown',   -- saas | ecommerce | info | unknown
  tags TEXT NOT NULL DEFAULT '[]',
  partner_count INTEGER,                     -- PartnerStack marketplace partnership_count
  offer TEXT,
  sources TEXT NOT NULL DEFAULT '[]',        -- [{source, ref}]
  hiring_signal TEXT,                        -- set when the company posts an affiliate/partner manager role
  score INTEGER NOT NULL DEFAULT 0,
  check_error TEXT,
  checked_at TEXT,
  first_seen TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX program_companies_status ON program_companies (status, score);

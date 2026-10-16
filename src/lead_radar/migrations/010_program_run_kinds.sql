-- Program runs come in two kinds: 'discover' (seed + verify, manual) and 'growth' (dated signals for both lists,
-- queued daily by /api/run-daily alongside the daily campaigns).
ALTER TABLE program_runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'discover';

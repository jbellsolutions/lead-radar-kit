-- Spec §6 calibration: the first N posts of a radar that Jev marks High also go to Stage 2; agreement is logged.
CREATE TABLE calibration (
  post_id TEXT NOT NULL REFERENCES posts(id),
  radar_id TEXT NOT NULL REFERENCES radars(id),
  run_id TEXT NOT NULL,
  jev_tier TEXT NOT NULL,
  reasoning_tier TEXT NOT NULL,
  agreed INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (post_id, radar_id)
);

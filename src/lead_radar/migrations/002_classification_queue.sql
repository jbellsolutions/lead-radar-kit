-- Durable hand-off from the search lane to the classifier cascade (M3 consumes it).
-- A post queued, classified or dropped for a radar is "seen" and is never sent to classification again.
CREATE TABLE classification_queue (
  post_id TEXT NOT NULL REFERENCES posts(id),
  radar_id TEXT NOT NULL REFERENCES radars(id),
  run_id TEXT NOT NULL REFERENCES runs(id),
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done','failed')),
  enqueued_at TEXT NOT NULL,
  PRIMARY KEY (post_id, radar_id)
);
CREATE INDEX classification_queue_pending ON classification_queue (radar_id, state);

-- Proposed relational schema, separate from the JSON-state offline reference.
-- Cross-posting balance, policy authorization, and outbox recovery require service logic.
PRAGMA foreign_keys = ON;
CREATE TABLE policy_version (
  policy_hash TEXT PRIMARY KEY, body_json TEXT NOT NULL,
  authorized_by TEXT NOT NULL, effective_at TEXT NOT NULL
);
CREATE TABLE instrument (
  instrument_id TEXT PRIMARY KEY, currency TEXT NOT NULL,
  price_scale INTEGER NOT NULL CHECK(price_scale >= 0),
  quantity_scale INTEGER NOT NULL CHECK(quantity_scale >= 0),
  metadata_version TEXT NOT NULL, metadata_json TEXT NOT NULL
);
CREATE TABLE session (
  session_id TEXT PRIMARY KEY, parent_session_id TEXT UNIQUE REFERENCES session(session_id),
  policy_hash TEXT NOT NULL REFERENCES policy_version(policy_hash),
  currency TEXT NOT NULL, seed_units INTEGER NOT NULL CHECK(seed_units > 0),
  goal_units INTEGER NOT NULL CHECK(goal_units > seed_units),
  state TEXT NOT NULL, version INTEGER NOT NULL CHECK(version >= 0)
);
CREATE TABLE order_intent (
  command_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
  session_id TEXT NOT NULL REFERENCES session(session_id),
  instrument_id TEXT NOT NULL REFERENCES instrument(instrument_id),
  side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  quantity_units INTEGER NOT NULL CHECK(quantity_units > 0),
  limit_price_units INTEGER NOT NULL CHECK(limit_price_units > 0),
  deadline_utc_ns INTEGER NOT NULL, state TEXT NOT NULL
);
CREATE TABLE execution (
  execution_id TEXT PRIMARY KEY, provider TEXT NOT NULL, account_key TEXT NOT NULL,
  provider_execution_id TEXT NOT NULL, correction_of TEXT REFERENCES execution(execution_id),
  command_id TEXT REFERENCES order_intent(command_id),
  payload_json TEXT NOT NULL,
  UNIQUE(provider, account_key, provider_execution_id)
);
CREATE TABLE journal_transaction (
  transaction_id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE TABLE journal_posting (
  transaction_id TEXT NOT NULL REFERENCES journal_transaction(transaction_id),
  line_number INTEGER NOT NULL, account_code TEXT NOT NULL, currency TEXT NOT NULL,
  amount_units INTEGER NOT NULL, scale INTEGER NOT NULL CHECK(scale >= 0),
  PRIMARY KEY(transaction_id,line_number)
);
CREATE TABLE outbox (
  command_id TEXT PRIMARY KEY REFERENCES order_intent(command_id),
  payload_json TEXT NOT NULL, state TEXT NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  broker_order_id TEXT, last_attempt_at TEXT
);

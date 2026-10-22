"""Numbered schema migrations (ADR-0003). Amount columns are canonical decimal
TEXT; never aggregate them in SQL (SQLite would convert to binary floats)."""

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "core event store and projections", r"""
CREATE TABLE writer_lease (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  holder TEXT NOT NULL, host TEXT NOT NULL, epoch INTEGER NOT NULL CHECK (epoch >= 1),
  acquired_at_ns INTEGER NOT NULL, heartbeat_at_ns INTEGER NOT NULL
);
CREATE TABLE events (
  seq INTEGER PRIMARY KEY,
  event_id TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  command_id TEXT NOT NULL,
  correlation_id TEXT NOT NULL,
  causation_id TEXT,
  principal_id TEXT NOT NULL,
  account_key TEXT, session_id TEXT, order_id TEXT, instrument_id TEXT,
  event_time_ns INTEGER,
  receipt_time_ns INTEGER NOT NULL,
  recorded_time_ns INTEGER NOT NULL,
  writer_epoch INTEGER NOT NULL,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  chain_hash TEXT NOT NULL
);
CREATE INDEX events_by_session ON events(session_id, seq);
CREATE INDEX events_by_order ON events(order_id, seq);
CREATE INDEX events_by_type ON events(type, seq);
CREATE TABLE commands (
  command_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  principal_role TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ACCEPTED', 'REJECTED')),
  result TEXT NOT NULL,
  recorded_time_ns INTEGER NOT NULL
);
-- projections ---------------------------------------------------------------
CREATE TABLE accounts (
  account_key TEXT PRIMARY KEY, provider TEXT NOT NULL, mode TEXT NOT NULL,
  base_currency TEXT NOT NULL, row TEXT NOT NULL
);
CREATE TABLE control (
  account_key TEXT PRIMARY KEY REFERENCES accounts(account_key),
  stop_latched INTEGER NOT NULL CHECK (stop_latched IN (0, 1)),
  row TEXT NOT NULL
);
CREATE TABLE execution_ownership (
  account_key TEXT PRIMARY KEY REFERENCES accounts(account_key),
  deployment_id TEXT NOT NULL, epoch INTEGER NOT NULL, status TEXT NOT NULL, row TEXT NOT NULL
);
CREATE TABLE policies (
  policy_hash TEXT PRIMARY KEY, name TEXT NOT NULL, body TEXT NOT NULL,
  published_by TEXT NOT NULL, published_at_ns INTEGER NOT NULL
);
CREATE TABLE policy_active (
  account_key TEXT PRIMARY KEY REFERENCES accounts(account_key),
  policy_hash TEXT NOT NULL REFERENCES policies(policy_hash), row TEXT NOT NULL
);
CREATE TABLE instruments (
  instrument_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, body TEXT NOT NULL, metadata_version INTEGER NOT NULL
);
CREATE TABLE provider_mappings (
  provider TEXT NOT NULL, external_id TEXT NOT NULL, version INTEGER NOT NULL,
  instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id), row TEXT NOT NULL,
  PRIMARY KEY (provider, external_id, version)
);
CREATE TABLE journal_entries (
  entry_id TEXT PRIMARY KEY, event_seq INTEGER NOT NULL, account_key TEXT NOT NULL,
  kind TEXT NOT NULL, memo TEXT NOT NULL, reverses TEXT
);
CREATE TABLE postings (
  entry_id TEXT NOT NULL REFERENCES journal_entries(entry_id), line INTEGER NOT NULL,
  account_key TEXT NOT NULL, account_code TEXT NOT NULL, owner TEXT NOT NULL,
  currency TEXT NOT NULL, instrument_id TEXT NOT NULL DEFAULT '', amount TEXT NOT NULL,
  PRIMARY KEY (entry_id, line)
);
CREATE INDEX postings_by_owner ON postings(account_key, owner);
CREATE TABLE balances (
  account_key TEXT NOT NULL, account_code TEXT NOT NULL, owner TEXT NOT NULL,
  currency TEXT NOT NULL, instrument_id TEXT NOT NULL DEFAULT '', amount TEXT NOT NULL,
  PRIMARY KEY (account_key, account_code, owner, currency, instrument_id)
);
CREATE TABLE lots (
  lot_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, instrument_id TEXT NOT NULL,
  owner TEXT NOT NULL, open_qty TEXT NOT NULL, row TEXT NOT NULL
);
CREATE INDEX lots_by_owner ON lots(account_key, owner, instrument_id);
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY, account_key TEXT NOT NULL REFERENCES accounts(account_key),
  state TEXT NOT NULL, mandate_hash TEXT NOT NULL, predecessor_id TEXT UNIQUE,
  row TEXT NOT NULL
);
CREATE TABLE mandates (
  mandate_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TABLE successors (
  predecessor_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
  successor_id TEXT NOT NULL UNIQUE
);
CREATE TABLE orders (
  order_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, session_id TEXT,
  instrument_id TEXT NOT NULL, side TEXT NOT NULL, state TEXT NOT NULL,
  broker_order_id TEXT, accepted_at_ns INTEGER NOT NULL, row TEXT NOT NULL
);
CREATE INDEX orders_by_state ON orders(account_key, state);
CREATE TABLE holds (
  hold_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, account_key TEXT NOT NULL, owner TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('CASH', 'QUANTITY')), unit TEXT NOT NULL,
  amount TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED'))
);
CREATE INDEX holds_by_owner ON holds(account_key, owner, status);
CREATE TABLE outbox (
  outbox_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, account_key TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('SUBMIT', 'CANCEL')), state TEXT NOT NULL,
  created_seq INTEGER NOT NULL, row TEXT NOT NULL
);
CREATE INDEX outbox_pending ON outbox(account_key, state, created_seq);
CREATE TABLE executions (
  exec_key TEXT PRIMARY KEY, account_key TEXT NOT NULL, order_id TEXT, row TEXT NOT NULL
);
CREATE TABLE pending_commissions (
  exec_key TEXT PRIMARY KEY, account_key TEXT NOT NULL, row TEXT NOT NULL
);
CREATE TABLE settlements (
  settlement_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, owner TEXT NOT NULL,
  status TEXT NOT NULL, row TEXT NOT NULL
);
CREATE TABLE exceptions (
  case_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, kind TEXT NOT NULL,
  blocking INTEGER NOT NULL CHECK (blocking IN (0, 1)), status TEXT NOT NULL,
  session_id TEXT, order_id TEXT, row TEXT NOT NULL
);
CREATE TABLE reconciliations (
  run_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, status TEXT NOT NULL,
  completed_at_ns INTEGER NOT NULL, row TEXT NOT NULL
);
CREATE TABLE risk_state (
  account_key TEXT PRIMARY KEY, row TEXT NOT NULL
);
CREATE TABLE proposals (
  proposal_id TEXT PRIMARY KEY, author TEXT NOT NULL, row TEXT NOT NULL
);
"""),
]

PROJECTION_TABLES = (
    "accounts", "control", "execution_ownership", "policies", "policy_active", "instruments",
    "provider_mappings", "journal_entries", "postings", "balances", "lots", "sessions", "mandates",
    "successors", "orders", "holds", "outbox", "executions", "pending_commissions", "settlements", "exceptions",
    "reconciliations", "risk_state", "proposals",
)

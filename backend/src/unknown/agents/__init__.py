"""Bounded agent layer (ADR-0016, increment I6).

Agents are research-side processes. They hold no broker credentials and no ledger
write handle; their single core write is a typed ``submit_research_proposal`` sent
with the agent token (ADR-0007, ADR-0013).

Modules:
  config     owner policy file (~/.unknown/config/agents.json); absent => paid calls off
  envelope   job envelopes, scheduler (dedupe, concurrency, expiry, cancellation)
  budget     durable spend ledger: reserve before a call, settle after (CAP-AGT-02)
  tariffs    effective-dated tariff rows, exact cost and worst case (CAP-MODEL-05)
  schema     local JSON-Schema subset validator + provider compatibility (CAP-MODEL-02)
  evidence   claim/citation verification and untrusted-text handling (CAP-MODEL-12, CAP-AGT-06)
  tools      default-deny read-only tool gateway (CAP-AGT-03, CAP-MODEL-03)
  gate       data-use gate: dataset rights x provider data policy (CAP-MODEL-09/10, CAP-AGT-08)
  catalog    runtime model catalog snapshots, diff, approvals (CAP-MODEL-01/11)
  providers  provider adapters behind one interface (CAP-MODEL-01..06)
  runner     one job end to end, producing a ResearchProposal or an abstention

No module in this package contains a provider model id or a price; those live in
owner-approved configuration (config/models/*.json, ~/.unknown/config/agents.json).
"""

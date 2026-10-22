# AWS deployment path (documented, not provisioned)

Status (ADR-0001): **specified only**. Nothing has been provisioned, and no infrastructure code has been applied. The owner brief requires an AWS path with **no paid provisioning** until the owner approves a budget (CLAUDE.md, book Chapter 24). Prices change; take them from the AWS pricing pages when deciding, not from this document.

## Topology

```
                 owner laptop / phone
                        │  SSM Session Manager port forwarding (no inbound ports)
                        ▼
┌──────────── VPC, private subnet, single AZ ─────────────────────────────┐
│  execution host (EC2, encrypted EBS gp3)                                │
│    IB Gateway (owner logs in via an SSM-forwarded VNC/desktop session)  │
│    core service (single writer, SQLite WAL on local EBS)                │
│    terminal API on 127.0.0.1:8765 (reached only through the tunnel)     │
│    backup job → S3 (SSE-KMS, versioned; object lock optional)           │
│  instance role: read its own secrets, write its backup prefix           │
├─────────────────────────────────────────────────────────────────────────┤
│  research worker (separate instance or container)                       │
│    PIT research store, agent worker (model API key only)               │
│    role: NO broker secret, no execution database access                 │
│    talks to the core only through the AGENT socket/proxy (proposals)    │
└─────────────────────────────────────────────────────────────────────────┘
egress: NAT or VPC endpoints (S3, SSM, Secrets Manager, KMS); broker and data-provider hosts allow-listed
```

## Decisions and why

- **One credential-owning execution host with fencing; no active-active.** SQLite WAL is a single-host database, and WAL must not sit on a network filesystem. Moving execution is the manual ownership transfer in RB-09: red stop, clean reconciliation, release certificate, acquire on the new host. The writer lease and the order-reference deployment tag stop two writers (ADR-0011).
- **No public inbound.** The terminal API, IB Gateway and SSH are reached only through SSM port forwarding. Security groups have no inbound rules. IMDSv2 is required.
- **Secrets** live in Secrets Manager or SSM Parameter Store (SecureString, KMS) and are read at process start. The execution role can read only the execution secrets; the research role only the model-provider key. No research process holds broker credentials (CLAUDE.md).
- **IBKR login is manual.** IB Gateway needs the owner's interactive login with 2FA after every restart and weekly reset. The project does not automate login or 2FA (no IBC-style credential injection), because that would bypass a broker security mechanism. "Always alive" here means the core stays up, reconciles and explains why it is waiting; it does not mean unattended re-authentication (book Chapter 24).
- **Backups:** `unknown.cli backup` runs on a timer and the copies are synced to S3 (SSE-KMS, versioning on). A monthly `restore-drill` runs on a scratch instance or locally, and the result is recorded (RB-10). The recovery point is the backup interval plus broker reconciliation; RB-10's restore rule (reconcile, resolve, green start) applies unchanged.
- **Region:** choose one where the owner's IBKR entity permits API access and the data licences allow processing. For US equities that is typically a US region close to the data sources. India and EU data residency must be checked against each provider's terms before any non-US data is stored there (ADR-0015).
- **Releases:** build an immutable artifact (git SHA plus pinned requirements). Run CI. Restore the latest backup on a scratch volume and run migrations and replay there. Only then switch the execution host with RB-09. Record the git SHA, schema version and policy hash in the deployment record.

## What would be provisioned (after owner approval)

| Item | Purpose | Notes |
|---|---|---|
| 1 small general-purpose EC2 instance (x86_64 or Graviton if IB Gateway supports it; verify) | execution host | IB Gateway needs a Java desktop session; a minimal desktop plus VNC bound to localhost |
| encrypted EBS gp3 volume | core database, logs | snapshot policy as a second backup line |
| S3 bucket (SSE-KMS, versioned, private) | backups | lifecycle to an infrequent-access class |
| KMS key, Secrets Manager entries | encryption, secrets | least-privilege key policy |
| SSM agent, VPC endpoints or NAT | access, egress | no inbound rules |
| optional second instance | research worker | no broker credentials |

The infrastructure code (Terraform or CDK) is written only after the owner sets a monthly budget, then reviewed. It is applied by the owner, never by an agent.

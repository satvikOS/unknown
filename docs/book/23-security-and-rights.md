# Security, supply chain and data rights {#security}

The threat model includes accidental duplicate orders, stolen credentials, malicious documents, compromised dependencies, unauthorized policy changes, data poisoning and loss of audit history. Protect cash authority separately from research capability. A sophisticated model with broad credentials increases the damage a mistake can cause.

## Trust boundaries

Run research workers without broker secrets, production write access or withdrawal authority. Grant each service the narrow API permissions it needs. Separate paper and live secrets, databases and endpoints. A production-mode flag must be validated against actual credential/account identity, not trusted as a label.

Store secrets in a suitable secret manager on AWS and the platform's protected credential storage on the Mac. Use short-lived infrastructure credentials where available, rotation procedures and audited access. AWS documents Secrets Manager practices and IMDSv2 configuration for instance metadata protection. [S33, S34] Never place credentials in prompts, repository files, screenshots, command histories or telemetry.

## Prompt injection and untrusted research

Treat news, filings, web pages, retrieved text and model output as untrusted data. A document that tells an agent to ignore limits or reveal keys has no authority. Enforce tool allowlists and resource limits outside the prompt. Restrict network egress for code experiments, mount datasets read-only where possible, and execute generated research code in an isolated environment with no production credentials.

Research sandboxes need CPU, memory, disk, network, duration and spend limits. Capture artifacts and provenance before disposal. A sandbox escape or unrestricted cloud role can defeat application-level controls; infrastructure policy is part of the design. Agent instructions are not a security boundary.

## Application security

Authenticate operator actions, protect sessions, require appropriate reauthentication for sensitive policy changes and use anti-CSRF controls for cookie-based web commands. Authorize every command server-side. Validate schemas and sizes, use parameterized database statements and restrict file paths. Avoid rendering untrusted document HTML directly in the terminal.

Use encryption in transit and at rest, tested backup recovery and secret redaction. Logs need integrity and access controls. Hash chaining can detect some tampering but does not replace protected storage, trusted timestamps or backups. Define which events are security critical and retain them according to actual legal and operational requirements.

NIST SSDF and OWASP ASVS provide useful secure-development and application-verification structures. NIST AI RMF adds model-risk governance. [S30, S31, S32] Map relevant controls to concrete evidence; using their names is not certification. Distinguish final standards from drafts and recheck revisions before claiming conformance.

## Dependency and license register

Pin dependencies, record hashes, maintain an SBOM and review security advisories. Prefer a small dependency set for the execution core. Build reproducibly where practical and sign release artifacts. A dependency upgrade requires compatibility and risk regression tests, not just a passing package install.

The owner can control original project code subject to applicable ownership arrangements. Third-party software and datasets remain governed by their licenses. Open-source licenses grant specified permissions, not ownership of the upstream work. [S40] Do not add a public open-source license to the owner's repository without their choice. The current repository is public, so secrets, account identifiers and private data must never be committed.

Data agreements may distinguish display, non-display, derived data, storage, redistribution and AI processing. Broker market-data access does not necessarily cover every analytics use. Maintain evidence of entitlements per deployment and provider. License enforcement must survive exports, cached summaries and embeddings.

## Incident actions

On suspected credential compromise: latch the trading stop, revoke affected credentials through authorized channels, preserve evidence, reconcile actual broker activity and rotate secrets before restart. On poisoned data: quarantine the source and affected artifacts, identify dependent strategies and block new entries until their inputs are trustworthy. On an unknown production binary: stop activation and compare signed artifact identity with the approved registry.

Security acceptance requires meaningful negative tests: a research worker cannot access broker secrets; an injected document cannot call an order tool; an unauthenticated browser cannot change policy; and a compromised optional worker cannot clear the global stop latch.


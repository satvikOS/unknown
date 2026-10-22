"""Provider adapters behind one interface (ADR-0016 section 3; CAP-MODEL-01..06).

``base``       interface, error taxonomy, retry policy, circuit breaker, transport, protocol facts
``openai``     urllib adapter; verified fragments only (see its FACTS table)
``xai``        urllib adapter; verified fragments only
``gemini``     urllib adapter; verified fragments only
``anthropic``  official ``anthropic`` SDK adapter (imported lazily; pinned in backend/requirements-agents.txt)
"""

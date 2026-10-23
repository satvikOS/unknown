"""Broker-agnostic live market-data plane (ADR-0018; wire format docs/design/marketdata-stream-contract.md).

* ``source``: the MarketDataSource interface every provider adapter implements (IBKR, Alpaca, SYNTHETIC, ...).
* ``config``: the owner's source configuration, per-key routing with fallback order, adapter factories.
* ``bus``: per-key quote coalescing, never-silently-dropped trades, line budgets, fan-out to SSE clients.
* ``store``: time & sales history outside the core database, bounded retention.
* ``sse``: the stream writer used by GET /api/marketdata/stream.
* ``synthetic``: deterministic, labelled SYNTHETIC data for development and the demo only.

Market data never enters the core's event-sourced database; the core's marks keep their existing path.
"""

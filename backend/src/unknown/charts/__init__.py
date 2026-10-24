"""Broker-agnostic chart bars (CAP-MKT-07).

``GET /api/charts/bars`` (api/routes/charts.py) asks the configured :class:`~unknown.charts.sources.BarsSource`
adapters for base bars of one security and aggregates them to the requested interval with exact decimals,
anchored on the venue's sourced trading sessions (config/calendars, config/markets/venues.json).

* No adapter is privileged. Every provider that writes the research bar store (the IBKR history adapter writes
  ``mkt_bar`` with ``provider='IBKR'``) appears as its own source; Alpaca historical bars come over REST when the
  owner stored keys in ``<UNKNOWN_HOME>/secrets/alpaca.json``; the owner may order sources in
  ``<UNKNOWN_HOME>/config/charts.json`` (``{"source_order": ["ALPACA", "IBKR"]}``).
* Every response names its source and data type (REALTIME, DELAYED or FROZEN).
* There is no generated or demo series (ADR-0020). Without a configured source that covers the security the response
  has no bars and ``reason: NO_SOURCE``; when every source fails, ``reason: ERROR``; an empty range, ``NO_DATA``.
* This package never imports a broker SDK or a broker adapter package.
"""

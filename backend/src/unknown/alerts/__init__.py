"""Server-side alerts (CAP-MKT-09, CAP-NEWS-13).

  rules     rule schema and validation (price crosses, % move, volume, time, study value, news, filings)
  studies   SMA / EMA / RSI with the chart's definitions (chart.studies@1)
  evaluate  pure evaluation with crossing, hysteresis, delayed-data and real-time-only semantics
  store     local SQLite store: rules, state, inbox, audit (``<UNKNOWN_HOME>/alerts/alerts.sqlite``)
  notify    macOS notifications through osascript (local only, no network)
  engine    the evaluation thread in the API process (market-data bus client, clock, news store)

An alert informs the owner. Nothing in this package can place, modify or cancel an order.
"""

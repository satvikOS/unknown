"""Portfolio and risk analytics (capability records CAP-PORT-01..13; book Ch. 6, 9, 10, 26; ADR-0006).

Everything in this package is READ-ONLY with respect to the money core. It opens the
core database with ``store.db.connect(readonly=True)`` and never imports a writer. Its
own durable state (end-of-day valuation snapshots) lives in a separate research SQLite
file (``snapshots.SnapshotStore``), never in the core database.

Modules
  formulas    formula registry: every published figure names ``id@version`` (DAT-05)
  common      scopes (account / system / owner), exact<->statistical conversions, lineage
  marks       mark selection from a quote snapshot, freshness and look-ahead guard
  holdings    holdings, cash ladder and NAV by owner (policy bucket) and currency (CAP-PORT-01)
  flows       external flows from core journal entries; P&L is never a flow (CAP-PORT-04)
  snapshots   daily valuation snapshotter into the research SQLite (CAP-PORT-01/04)
  pnl         daily P&L decomposition from the journal with an exact identity check (CAP-PORT-02)
  performance TWR, Modified Dietz, IRR, unit value, drawdown, risk statistics (CAP-PORT-04/05)
  exposure    exposure and concentration by instrument, owner, sector (SIC), currency (CAP-PORT-03)
  var         historical VaR, quantile-integral ES (book eq. 6.3), Kupiec backtest (CAP-PORT-10)
  stress      overnight gap, correlated selloff, stale quote, fee revision (CAP-PORT-11)
  whatif      dry-run of a hypothetical intent through the deterministic pretrade evaluator (CAP-PORT-12)
  linalg      small dense linear algebra (symmetric eigenvalues) for covariance checks (CAP-PORT-08, CAP-PORT-13)
  allocation  long-only baselines, shrinkage covariance, eq. 6.2 mean-variance, trade-list proposals (CAP-PORT-13)

Number discipline: money and accounting quantities (balances, market values, NAV,
flows, trade notionals, fees) are exact ``Fixed``. Statistical analytics (returns,
volatility, Sharpe, VaR on scenario returns) use Python floats; each such function
states its convention, and no float is ever used to post or reserve money.

Fixture discipline: values derived from quotes flagged ``fixture`` carry that flag
through to every output; fixture data is never presented as market data.
"""

"""Company research analytics over the point-in-time research store (increments I4/I7).

Modules (each cites its capability records):

* ``mapping``    versioned table from us-gaap / ifrs-full concepts to standardized line items (CAP-CO-05)
* ``periods``    fiscal-period alignment: duration classes, 52-53 week years, fiscal quarters (CAP-CO-04, CAP-CO-05)
* ``statements`` standardized statements, derived quarters, TTM, all as of a decision time (CAP-CO-04/05/06)
* ``metrics``    ratios, EV bridge and multiples with lineage; persistence via ``ResearchStore.record_metric`` (CAP-CO-07)
* ``dcf``        book eq. 17.1 DCF, WACC, sensitivity surface and scenario range (CAP-CO-11)
* ``peers``      SIC peer groups and comparables statistics with point-in-time caveats (CAP-CO-10)
* ``filings``    filings list with SEC deep links from stored submissions payloads (CAP-CO-02)
* ``search``     company and instrument search (CAP-INS-01, CAP-CO-01)
* ``tearsheet``  composition of the above into typed blocks (CAP-CO-12)

Everything here is research-side: it reads the research store (ADR-0015) and never posts money.
Accounting quantities are exact ``Fixed`` values; ratios are exact quotients rounded HALF_EVEN to a
declared scale. Statistical DCF discounting uses ``decimal`` with an explicit context (see ``dcf``).
"""

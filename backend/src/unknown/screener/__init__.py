"""Equity screener over the point-in-time research store (CAP-MKT-10).

* ``fields``  what a criterion can test and how each issuer's value is obtained as of a time
* ``engine``  definition compiler, run with manifest (as-of, knowledge cutoff, data versions, prices), rerun check
* ``store``   saved screens and runs (append-only, ``<UNKNOWN_HOME>/screener/screener.sqlite``)

Research-side only: nothing here reads or writes the money core.
"""

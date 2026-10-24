"""AI processes: scheduled and event-driven advisory notes (ADR-0016; CAP-AGT-04, CAP-AGT-11, CAP-AGT-13).

Three model processes run inside the model worker (``python -m unknown.agents.worker``), the only process that
holds a provider key, through the same runner, ledger, tariffs, data-use gate and audit trail as research jobs:

* ``brief``: the morning brief on market days (``config/calendars``), at the owner's configured local time:
  overnight filings and regulator releases for the watchlist and holdings, central-bank releases, and stored
  daily price moves where real bars exist.
* ``summaries``: a short grounded summary of each new 8-K/10-Q/10-K or regulator item for a watched issuer, as
  the news poller stores it.
* ``explainer``: on the owner's request, relates a security's recent real price move to the filings and news
  in the window, and says plainly when nothing explains it.

Every output is a *note* in the local notes store (``<UNKNOWN_HOME>/agents/processes.sqlite``): advisory,
labelled AI with model id, prompt template hash and time, each line citing the store rows it came from.
Notes are never sent to the core and nothing reads them to act. The approval queue (``store.py``) binds an
owner decision to the exact content hash of a proposal and audits it; a decision never places an order,
starts a session, moves money or writes to the core.

Modules the terminal API may import (no provider, runner or worker code): ``config``, ``store``,
``sources``, ``dashboard``. Worker-only: ``engine``, ``scheduler``.
"""

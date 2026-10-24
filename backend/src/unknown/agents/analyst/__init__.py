"""The AI analyst ("ASK", ADR-0016): the owner asks a question in the terminal and a Claude tool-use loop answers it
from the platform's own read-only data, with every claim citing the tool results it used.

Process split (the same as research jobs):
  * the terminal API (``api/routes/ai.py``) imports only ``store``, ``submission``, ``status``, ``toolspec`` and
    ``prompt``: it writes the analyst job store and the thread store and streams stored events; it never holds a
    provider key and never imports the loop, the worker, the data sources or the SDK;
  * the analyst worker (``python -m unknown.agents.analyst``, agents venv) runs the jobs: budget reservation before
    every call, tariffs, the data-use gate, per-role tool allowlist, the audit trail and the injection defences of the
    agent layer (``loop.py``, ``worker.py``), and reads platform data only through ``sources.py`` (read-only).

AI output is advisory and labelled AI. No analyst tool can place, change or cancel orders, start sessions, move money or
write to the core: the tool registry refuses write-capable tools and the sources open every store read-only.
This module imports nothing so that importing the package stays cheap for the API process.
"""

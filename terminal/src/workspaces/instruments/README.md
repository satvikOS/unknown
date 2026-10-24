# Instruments workspace

Registered as `instruments` (`g i`). `#/instruments` lists the instrument master; `#/instruments/<instrument_id>` shows one instrument: reference data, tick-rule bands (with the band at the current bid), every provider-mapping version, attributes, the latest quote with its source and age, and holdings by owner. An instrument marked `attributes.fixture` is not a real security: it carries an ERR flag wherever it appears, and any fixture quote or fixture-marked position value renders as an error, never as data. Quotes are polled every 2 s because they are process state, not part of the event log.

Since this workspace is registered, the command palette lists instruments and other workspaces' `WorkspaceLink to="instruments"` links are live.

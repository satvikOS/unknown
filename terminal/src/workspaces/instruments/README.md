# Instruments workspace

Registered as `instruments` (`g i`). `#/instruments` lists the instrument master; `#/instruments/<instrument_id>` shows one instrument: reference data, tick-rule bands (with the band at the current bid), every provider-mapping version, attributes, the latest quote with its source and age, and holdings by owner. Fixture instruments (`attributes.fixture`) and fixture quotes carry a FIXTURE badge. Quotes are polled every 2 s because they are process state, not part of the event log.

Since this workspace is registered, the command palette lists instruments and other workspaces' `WorkspaceLink to="instruments"` links are live.

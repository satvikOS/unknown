"""IBKR TWS API paper-trading adapter (BRK-02).

Importing this package never imports ``ibapi``: the official SDK (GPL-3.0-or-later,
never vendored) is loaded from the owner's local installation inside
``IbkrAdapter.__init__``, which raises ``IbkrSdkUnavailable`` when it is absent.
The core (``unknown.core``, ``domain``, ``store`` ...) must not import this package.
See docs/integrations/ibkr.md.
"""
from .adapter import (EVIDENCE_LEVEL, NOT_TRANSMITTED, PROVIDER, IbkrAdapter, IbkrNotReady, IbkrSdkMismatch,
                      IbkrSdkUnavailable, load_sdk, sdk_fingerprint)
from .config import DEFAULT_CONFIG_PATH, IbkrConfig, IbkrConfigError, config_from_dict, load_config
from .ids import OrderIdStateError, OrderIdStore
from .pacing import PacingScheduler, max_requests_per_second

__all__ = [
    "DEFAULT_CONFIG_PATH", "EVIDENCE_LEVEL", "NOT_TRANSMITTED", "PROVIDER", "IbkrAdapter", "IbkrConfig",
    "IbkrConfigError", "IbkrNotReady", "IbkrSdkMismatch", "IbkrSdkUnavailable", "OrderIdStateError", "OrderIdStore",
    "PacingScheduler", "config_from_dict", "load_config", "load_sdk", "max_requests_per_second", "sdk_fingerprint",
]

"""Interfaces for future, optional data providers (e.g. Liquid, Quiver Quant).

Nothing here is wired in yet. A provider only produces advisory Signals for
the research log; signals can never place orders or bypass the risk engine.
To add one later: implement SignalProvider, register it in PROVIDERS, set
`enabled: true` in config/integrations.json after the owner approves any cost,
and add its host to the cloud environment's allowlist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Signal:
    provider: str
    symbol: str
    kind: str  # e.g. "congress_trade", "insider_buy", "flow", "sentiment"
    summary: str
    observed_at: str
    source_url: str = ""
    extra: dict = field(default_factory=dict)


class ProviderDisabled(RuntimeError):
    pass


class SignalProvider(Protocol):
    name: str

    def fetch_signals(self, symbols: list) -> list:
        """Return advisory Signal objects. Must not have side effects."""
        ...


class _NotIntegrated:
    name = "unnamed"

    def __init__(self, settings: dict):
        self.settings = settings

    def fetch_signals(self, symbols: list) -> list:
        raise ProviderDisabled(f"{self.name} is not integrated yet (design stub only)")


class LiquidProvider(_NotIntegrated):
    name = "liquid"


class QuiverQuantProvider(_NotIntegrated):
    name = "quiver_quant"


PROVIDERS = {"liquid": LiquidProvider, "quiver_quant": QuiverQuantProvider}


def enabled_providers(settings: dict) -> list:
    out = []
    for name, cls in PROVIDERS.items():
        cfg = settings.get(name, {})
        if cfg.get("enabled"):
            out.append(cls(cfg))
    return out


def collect_signals(settings: dict, symbols: list) -> tuple:
    """Gather signals from enabled providers. Errors are reported, never raised."""
    signals, errors = [], []
    for provider in enabled_providers(settings):
        try:
            signals.extend(provider.fetch_signals(symbols))
        except Exception as exc:  # a provider outage must not break a workflow
            errors.append(f"{provider.name}: {exc}")
    return signals, errors

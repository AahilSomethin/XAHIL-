"""Configuration, hard ceilings and the paper-only endpoint guard.

Everything here fails closed: a missing, malformed or looser-than-allowed
setting raises ConfigError, and callers exit non-zero.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
MEMORY_DIR = ROOT / "memory"
STATE_DIR = MEMORY_DIR / "state"

# --- Paper-only endpoints. These are the ONLY hosts the Alpaca client may call. ---
APPROVED_TRADING_ENDPOINT = "https://paper-api.alpaca.markets/v2"
APPROVED_DATA_ENDPOINT = "https://data.alpaca.markets/v2"

# --- Hard ceilings from the owner's requirements. Config may tighten, never loosen. ---
HARD_MAX_POSITION_PCT = 5.0
HARD_MAX_TOTAL_EXPOSURE_PCT = 20.0
HARD_MAX_NEW_TRADES_PER_WEEK = 3
DEFAULT_STARTING_CAPITAL = 100000.0  # owner decision 2026-11-01: keep the $100k Alpaca paper account
HARD_MAX_QUOTE_AGE_SECONDS = 300.0


class ConfigError(RuntimeError):
    """Raised when configuration is missing, unsafe or inconsistent."""


@dataclass(frozen=True)
class RiskLimits:
    starting_capital: float = DEFAULT_STARTING_CAPITAL
    max_position_pct: float = HARD_MAX_POSITION_PCT
    max_total_exposure_pct: float = HARD_MAX_TOTAL_EXPOSURE_PCT
    max_new_trades_per_week: int = HARD_MAX_NEW_TRADES_PER_WEEK
    max_open_positions: int = 4
    max_equity_multiple_of_start: float = 3.0
    max_spread_pct: float = 1.0
    min_share_price: float = 1.0
    min_order_notional: float = 1.0
    price_buffer_pct: float = 2.0
    max_daytrade_count: int = 2
    max_quote_age_seconds: float = 60.0
    allowed_trail_percents: tuple = (5.0, 7.0, 10.0)
    allowed_exchanges: tuple = ("NYSE", "NASDAQ", "ARCA", "AMEX", "BATS")
    denylist: frozenset = field(default_factory=frozenset)

    def validate(self) -> None:
        problems = []
        if self.max_position_pct <= 0 or self.max_position_pct > HARD_MAX_POSITION_PCT:
            problems.append(f"max_position_pct must be in (0, {HARD_MAX_POSITION_PCT}]")
        if self.max_total_exposure_pct <= 0 or self.max_total_exposure_pct > HARD_MAX_TOTAL_EXPOSURE_PCT:
            problems.append(f"max_total_exposure_pct must be in (0, {HARD_MAX_TOTAL_EXPOSURE_PCT}]")
        if self.max_new_trades_per_week < 0 or self.max_new_trades_per_week > HARD_MAX_NEW_TRADES_PER_WEEK:
            problems.append(f"max_new_trades_per_week must be in [0, {HARD_MAX_NEW_TRADES_PER_WEEK}]")
        if self.max_position_pct > self.max_total_exposure_pct:
            problems.append("max_position_pct cannot exceed max_total_exposure_pct")
        if self.starting_capital <= 0:
            problems.append("starting_capital must be positive")
        if self.max_open_positions < 1:
            problems.append("max_open_positions must be >= 1")
        if self.max_equity_multiple_of_start < 1:
            problems.append("max_equity_multiple_of_start must be >= 1")
        if not (0 < self.max_spread_pct <= 5):
            problems.append("max_spread_pct must be in (0, 5]")
        if not (0 < self.max_quote_age_seconds <= HARD_MAX_QUOTE_AGE_SECONDS):
            problems.append(f"max_quote_age_seconds must be in (0, {HARD_MAX_QUOTE_AGE_SECONDS}]")
        if not (0 <= self.max_daytrade_count <= 2):
            problems.append("max_daytrade_count must be in [0, 2]")
        if self.price_buffer_pct < 0:
            problems.append("price_buffer_pct must be >= 0")
        if problems:
            raise ConfigError("Unsafe risk_limits.json: " + "; ".join(problems))


@dataclass(frozen=True)
class ExecutionSettings:
    order_execution_enabled: bool = False
    approved_by: str = ""
    approved_on: str = ""
    halt: bool = False
    halt_reason: str = ""


def _read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"Missing config file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return data


def load_denylist(config_dir: Path = CONFIG_DIR) -> frozenset:
    path = config_dir / "leveraged_inverse_denylist.txt"
    if not path.exists():
        raise ConfigError(f"Missing denylist: {path}")
    symbols = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line:
            symbols.add(line)
    return frozenset(symbols)


def load_risk_limits(config_dir: Path = CONFIG_DIR) -> RiskLimits:
    raw = _read_json(config_dir / "risk_limits.json")
    known = {f for f in RiskLimits.__dataclass_fields__ if f != "denylist"}
    unknown = set(raw) - known - {"_comment"}
    if unknown:
        raise ConfigError(f"Unknown keys in risk_limits.json: {sorted(unknown)}")
    kwargs = {k: v for k, v in raw.items() if k in known}
    for key in ("allowed_trail_percents", "allowed_exchanges"):
        if key in kwargs:
            kwargs[key] = tuple(kwargs[key])
    try:
        limits = RiskLimits(denylist=load_denylist(config_dir), **kwargs)
    except TypeError as exc:
        raise ConfigError(f"Bad risk_limits.json: {exc}") from exc
    limits.validate()
    return limits


def load_execution_settings(config_dir: Path = CONFIG_DIR) -> ExecutionSettings:
    raw = _read_json(config_dir / "execution.json")
    enabled = raw.get("order_execution_enabled", False)
    halt = raw.get("halt", False)
    if not isinstance(enabled, bool) or not isinstance(halt, bool):
        raise ConfigError("execution.json: order_execution_enabled and halt must be true/false")
    return ExecutionSettings(
        order_execution_enabled=enabled,
        approved_by=str(raw.get("approved_by", "")),
        approved_on=str(raw.get("approved_on", "")),
        halt=halt,
        halt_reason=str(raw.get("halt_reason", "")),
    )


def load_integrations(config_dir: Path = CONFIG_DIR) -> dict:
    return _read_json(config_dir / "integrations.json")


# --- Environment ------------------------------------------------------------

def is_cloud_run(env=None) -> bool:
    env = os.environ if env is None else env
    return bool(env.get("CLAUDE_CODE_REMOTE") or env.get("XAHIL_CLOUD"))


def load_dotenv(path: Path = ROOT / ".env", env=None) -> None:
    """Load KEY=VALUE pairs from a local .env without executing it.

    Never used in cloud runs: cloud credentials must come from the
    environment or network secrets, and a .env there is a leak risk.
    """
    env = os.environ if env is None else env
    if is_cloud_run(env) or not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in env:
            env[key] = value


@dataclass(frozen=True)
class AlpacaCredentials:
    mode: str  # "headers" or "proxy"
    key_id: str = field(default="", repr=False)
    secret: str = field(default="", repr=False)

    def headers(self) -> dict:
        if self.mode == "proxy":
            return {}
        return {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret}


def resolve_trading_endpoint(env=None) -> str:
    """Return the approved paper endpoint, or raise if anything else is configured."""
    env = os.environ if env is None else env
    configured = env.get("ALPACA_ENDPOINT")
    if configured is None or configured == "":
        return APPROVED_TRADING_ENDPOINT
    if configured != APPROVED_TRADING_ENDPOINT:
        raise ConfigError(
            "ALPACA_ENDPOINT is not the approved paper endpoint. "
            f"Expected exactly {APPROVED_TRADING_ENDPOINT!r}. Refusing to run."
        )
    return configured


def resolve_data_endpoint(env=None) -> str:
    env = os.environ if env is None else env
    configured = env.get("ALPACA_DATA_ENDPOINT")
    if configured is None or configured == "":
        return APPROVED_DATA_ENDPOINT
    if configured != APPROVED_DATA_ENDPOINT:
        raise ConfigError(
            f"ALPACA_DATA_ENDPOINT must be exactly {APPROVED_DATA_ENDPOINT!r}. Refusing to run."
        )
    return configured


def resolve_alpaca_credentials(env=None) -> AlpacaCredentials:
    env = os.environ if env is None else env
    mode = env.get("XAHIL_ALPACA_AUTH", "headers").strip().lower()
    if mode == "proxy":
        # Network secrets: Anthropic's proxy attaches the keys; the agent never sees them.
        return AlpacaCredentials(mode="proxy")
    if mode != "headers":
        raise ConfigError("XAHIL_ALPACA_AUTH must be 'headers' or 'proxy'")
    key_id = env.get("ALPACA_API_KEY", "").strip()
    secret = env.get("ALPACA_SECRET_KEY", "").strip()
    missing = [n for n, v in (("ALPACA_API_KEY", key_id), ("ALPACA_SECRET_KEY", secret)) if not v]
    if missing:
        raise ConfigError(f"{', '.join(missing)} not set in environment")
    if not key_id.startswith("PK"):
        raise ConfigError(
            "ALPACA_API_KEY does not look like a PAPER key (paper key IDs start with 'PK'). Refusing to run."
        )
    return AlpacaCredentials(mode="headers", key_id=key_id, secret=secret)


def execution_env_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get("XAHIL_ORDER_EXECUTION", "").strip().lower() == "enabled"


def env_halt(env=None) -> bool:
    env = os.environ if env is None else env
    return env.get("XAHIL_HALT", "").strip().lower() in {"1", "true", "yes"}

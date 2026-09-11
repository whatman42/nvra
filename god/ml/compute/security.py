"""Strip secrets and reject execution commands before any cloud/local job leaves the process."""
from __future__ import annotations

from typing import Any, Mapping

# Keys that must never appear in job manifests, checkpoints, or cloud payloads.
FORBIDDEN_KEY_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "broker",
    "mt5",
    "exchange_key",
    "exchange_secret",
    "live_auth",
    "authorization",
    "windows_credential",
    "keyring",
    "session_cookie",
    "oauth",
    "gemini_key",
    "telegram_token",
    "bot_token",
)

# Explicit execution / order-path injection attempts — reject hard.
EXECUTION_COMMAND_FRAGMENTS = (
    "place_order",
    "submit_order",
    "order_request",
    "broker_order",
    "mt5_order",
    "execute_trade",
    "live_order",
    "send_order",
    "modify_risk",
    "bypass_governor",
    "bypass_reconciliation",
    "promote_execution",
    "set_live",
    "enable_live",
    # Shell / process injection (must reject, not silently strip)
    "shell",
    "subprocess",
    "os.system",
    "system(",
    "popen",
    "command",
    "__import__",
    "eval(",
    "exec(",
)


def _is_forbidden_key(key: str) -> bool:
    k = key.lower().replace("-", "_")
    return any(frag in k for frag in FORBIDDEN_KEY_FRAGMENTS)


def _is_execution_command_key(key: str) -> bool:
    k = key.lower().replace("-", "_")
    return any(frag in k for frag in EXECUTION_COMMAND_FRAGMENTS)


def sanitize_mapping(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a deep copy with forbidden keys removed from nested structures."""
    if not data:
        return {}
    return _sanitize_value(dict(data))  # type: ignore[return-value]


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _is_forbidden_key(str(key)):
                continue
            out[str(key)] = _sanitize_value(item)
        return out
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(item) for item in value)
    if isinstance(value, set):
        cleaned = []
        for item in value:
            cleaned.append(_sanitize_value(item))
        try:
            return set(cleaned)
        except TypeError:
            return cleaned
    return value


def assert_no_secrets(data: Any) -> None:
    """Raise ValueError if forbidden keys remain anywhere in the structure."""
    if data is None:
        return
    if isinstance(data, Mapping):
        for key, value in data.items():
            if _is_forbidden_key(str(key)):
                raise ValueError(f"forbidden key in compute payload: {key}")
            assert_no_secrets(value)
        return
    if isinstance(data, (list, tuple, set)):
        for item in data:
            assert_no_secrets(item)


def assert_no_execution_commands(data: Any) -> None:
    """Raise ValueError if execution/order command keys are present.

    Colab/Kaggle must never receive authority to place orders or modify risk.
    """
    if data is None:
        return
    if isinstance(data, Mapping):
        for key, value in data.items():
            if _is_execution_command_key(str(key)):
                raise ValueError(f"execution command rejected in compute payload: {key}")
            assert_no_execution_commands(value)
        return
    if isinstance(data, (list, tuple, set)):
        for item in data:
            assert_no_execution_commands(item)


def sanitize_and_guard(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Reject secrets/execution commands on raw input, then return sanitized copy.

    Order matters: keys matching both secret and execution fragments (e.g. mt5_order)
    must be rejected before sanitize_mapping strips them as secrets.
    """
    if data is None:
        data = {}
    assert_no_execution_commands(data)
    assert_no_secrets(data)
    clean = sanitize_mapping(data)
    assert_no_execution_commands(clean)
    assert_no_secrets(clean)
    return clean

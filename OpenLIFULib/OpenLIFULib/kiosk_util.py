"""User-mode env-var lock.

A single environment variable, :data:`USER_MODE_ENV_VAR` (``OPENLIFU_USER_MODE``),
controls all of the operator-facing kiosk behavior at once:

* Require sign-in on Home (kiosk gating)
* User-account mode (``Permissions = User`` -- role-based widget visibility)
* Guided-mode navigation

When the env var is unset (or set to a recognised falsy value), all three
are off and the user has free, anonymous, unrestricted access. When it is
set to a recognised truthy value, all three are on. There is no runtime
toggle for any of these -- launchers must set the env var before starting
Slicer.

The legacy variables ``OPENLIFU_REQUIRE_LOGIN`` and ``OPENLIFU_GUIDED_MODE``
are still honored as truthy aliases for backward compatibility with older
launcher scripts: if either is set to a truthy value, user mode is forced
on.

The ``locked_by_env`` accessors all return ``True`` now (the value is
always env-driven), but they are kept so callers that gate on
"don't let the UI change this" continue to work.
"""
from __future__ import annotations

import os
from typing import Optional

USER_MODE_ENV_VAR = "OPENLIFU_USER_MODE"

# Legacy aliases (truthy values force user mode on).
LEGACY_REQUIRE_LOGIN_ENV_VAR = "OPENLIFU_REQUIRE_LOGIN"
LEGACY_GUIDED_MODE_ENV_VAR = "OPENLIFU_GUIDED_MODE"

# Backward-compat aliases for callers that still import these names.
KIOSK_ENV_VAR = USER_MODE_ENV_VAR
GUIDED_MODE_ENV_VAR = USER_MODE_ENV_VAR

_TRUTHY = ("1", "true", "yes", "on", "y", "t", "enable", "enabled")
_FALSY = ("0", "false", "no", "off", "n", "f", "disable", "disabled", "")


def _parse_bool(value) -> Optional[bool]:
    """Return ``True``/``False`` for recognised string forms, else ``None``.

    Strips surrounding whitespace and any wrapping single/double quotes so
    that values set via ``cmd``'s ``set OPENLIFU_USER_MODE='1'`` (which keeps
    the literal quotes as part of the value) are parsed correctly. Any
    non-empty value that is not in the recognised falsy set is treated as
    truthy, so typos like ``OPENLIFU_USER_MODE=yep`` still enable user mode
    rather than silently leaving it off.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    # Strip a single layer of matching wrapping quotes, e.g. "'1'" -> "1".
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    text = text.lower()
    if text in _FALSY:
        return False
    if text in _TRUTHY:
        return True
    # Any other non-empty value -> truthy (lean toward enabling so operators
    # don't get an off-by-quoting kiosk-lock bypass).
    return True


def get_user_mode() -> bool:
    """Return the effective user-mode (kiosk) state from the environment.

    True if ``OPENLIFU_USER_MODE`` is truthy, or if a legacy alias is truthy.
    False otherwise (including when the var is unset).
    """
    primary = _parse_bool(os.environ.get(USER_MODE_ENV_VAR))
    if primary is not None:
        return primary
    for legacy in (LEGACY_REQUIRE_LOGIN_ENV_VAR, LEGACY_GUIDED_MODE_ENV_VAR):
        legacy_val = _parse_bool(os.environ.get(legacy))
        if legacy_val is True:
            return True
    return False


# ---------------------------------------------------------------------
# Backward-compatible accessors. All three derive from get_user_mode().
# ---------------------------------------------------------------------

def get_require_login_on_home() -> bool:
    """Effective "require sign-in on Home" value (always env-driven)."""
    return get_user_mode()


def get_require_login_on_home_locked_by_env() -> bool:
    """Always ``True`` -- the value is now governed exclusively by the env var."""
    return True


def set_require_login_on_home(value: bool) -> None:
    """Deprecated -- value is now env-only. Retained as a no-op for callers."""
    return None


def get_guided_mode_env_value() -> Optional[bool]:
    """Env-driven guided-mode value (always non-None now)."""
    return get_user_mode()


def get_guided_mode_locked_by_env() -> bool:
    """Always ``True`` -- guided mode is governed exclusively by the env var."""
    return True


def get_user_account_mode_env_value() -> Optional[bool]:
    """Env-driven user-account-mode value (always non-None now)."""
    return get_user_mode()


def get_user_account_mode_locked_by_env() -> bool:
    """Always ``True`` -- UAM is governed exclusively by the env var."""
    return True

"""Batchroom Pro — the paid layer.

Source-available, NOT MIT: see LICENSE-PRO at the repository root. Running
these features in production requires a valid license key. The MIT core
never depends on this package for its own functionality; it only *asks* it
whether paid features are unlocked.

Design principles (plan §6.2): the free core is never crippled; license
keys are offline Ed25519-signed files — no phone-home, air-gap friendly.
An expired subscription never locks anyone out of their data: existing
logins keep working, paid *management* features pause until renewal.
"""

from .license import (
    LicenseInfo,
    entitlements,
    load_license,
    parse_license,
    save_license,
)

__all__ = [
    "LicenseInfo",
    "entitlements",
    "load_license",
    "parse_license",
    "save_license",
]

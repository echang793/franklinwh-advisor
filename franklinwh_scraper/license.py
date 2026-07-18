"""Ed25519-signed license verification for customer builds.

Personal/dev builds run with ENFORCE_LICENSE = False (the default here);
the public-build packaging step flips it to True. Licenses are issued with
scripts/issue_license.py (never distributed) and bound to the customer's
FranklinWH gateway ID, so a copied install refuses to run on any other
system. Only the public key ships with the code — customers cannot forge
licenses, only Eric's private signing key can create them.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

ENFORCE_LICENSE = False   # flipped to True by the public-build packaging step
GRACE_DAYS      = 14      # keep running this long past expiry, with warnings
LICENSE_PATH    = Path.home() / ".franklinwh.license"

PUBLIC_KEY_B64 = "X1zuEABe2+7Z7h7Ame67N4AJJKQa0LrRC6tt/CoSYWY="


@dataclass
class LicenseStatus:
    state: str      # "ok" | "grace" | "invalid"
    message: str
    customer: str = ""


def _lastseen_path(license_path: Path) -> Path:
    return license_path.with_name(license_path.name + ".lastseen")


def _clock_rolled_back(license_path: Path, today: date) -> bool:
    """Detect the system clock being turned back to indefinitely extend an
    expired license. Persists the latest date ever observed next to the
    license file; a `today` earlier than that is proof of rollback. Never
    regresses the stored date, so this only ever tightens, never loosens."""
    lp = _lastseen_path(license_path)
    try:
        last_seen = date.fromisoformat(lp.read_text().strip())
    except (OSError, ValueError):
        last_seen = None

    rolled_back = last_seen is not None and today < last_seen
    if not rolled_back:
        try:
            lp.write_text(max(today, last_seen or today).isoformat())
        except OSError:
            pass
    return rolled_back


def check_license(gateway_id: str, path: Path | None = None) -> LicenseStatus:
    """Verify the license file's signature, gateway binding, and expiry."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    path = path or LICENSE_PATH
    try:
        blob = json.loads(path.read_text())
        payload = blob["payload"]
        sig = base64.b64decode(blob["sig"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return LicenseStatus("invalid", f"License file missing or unreadable at {path}")

    # A signed-but-malformed payload (issuer bug, not attacker-controlled)
    # must degrade to "invalid" rather than crash the caller — startup/watch
    # loop failing outright is the wrong direction for licensing code to fail.
    if not isinstance(payload, dict):
        return LicenseStatus("invalid", "License payload is malformed")

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(PUBLIC_KEY_B64))
        pub.verify(sig, canonical)
    except (InvalidSignature, ValueError):
        return LicenseStatus("invalid", "License signature invalid")

    customer = str(payload.get("customer", ""))

    licensed_gw = str(payload.get("gateway_id", ""))
    if not gateway_id or licensed_gw != gateway_id:
        return LicenseStatus(
            "invalid",
            f"License issued for gateway {licensed_gw or '?'} but this system is "
            f"{gateway_id or 'unknown'}",
            customer,
        )

    try:
        expires = datetime.strptime(str(payload.get("expires", "")), "%Y-%m-%d").date()
    except ValueError:
        return LicenseStatus("invalid", "License has no valid expiry date", customer)

    today = date.today()
    if _clock_rolled_back(path, today):
        return LicenseStatus("invalid", "System clock rollback detected — license check refused", customer)
    if today <= expires:
        return LicenseStatus("ok", f"Licensed to {customer} until {expires}", customer)
    if today <= expires + timedelta(days=GRACE_DAYS):
        days_left = (expires + timedelta(days=GRACE_DAYS) - today).days
        return LicenseStatus(
            "grace",
            f"License expired {expires} — advisor stops in {days_left} day(s). "
            f"Contact Eric to renew.",
            customer,
        )
    return LicenseStatus("invalid", f"License expired {expires} (grace period over)", customer)

"""Switching the gateway between Time-of-Use, Self-Consumption and Emergency Backup.

The call is the one the community client library (richo/franklinwh-python `set_mode`) makes:
a form POST to hes-gateway/terminal/tou/updateTouMode. The ids below come from that library and
have NOT been confirmed against this gateway, which is why Config.mode_switch_dry_run defaults
on: the button says what it would send and sends nothing until you turn that off.
"""

import logging

logger = logging.getLogger(__name__)

# mode -> (currendId, workMode)
MODES = {"tou": (9322, 1), "self": (9323, 2), "eb": (9324, 3)}
MODE_NAMES = {"tou": "Time of Use", "self": "Self-Consumption", "eb": "Emergency Backup"}
_SOC_KEY = {"tou": "touMinSoc", "self": "selfMinSoc", "eb": "backupMaxSoc"}
_SOC_DEFAULT = {"tou": 50, "self": 10, "eb": 100}


def mode_payload(gateway: str, mode: str, soc: float) -> dict:
    """The form fields for switching to `mode` with its `soc` setting (reserve, or the EB target)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    current_id, work_mode = MODES[mode]
    return {"currendId": str(current_id), "workMode": str(work_mode), "soc": str(int(round(soc))),
            "gatewayId": gateway, "lang": "EN_US"}


def switch(client, gateway: str, mode: str, dry_run: bool = True) -> tuple[bool, str]:
    """Switch to `mode` using the SoC setting the gateway already has for it (so the reserve
    you set in the app is kept). (ok, message). Never raises."""
    if mode not in MODES:
        return False, f"I don't know a mode called {mode!r}."
    name = MODE_NAMES[mode]
    try:
        status = client.get_mode_status(gateway) or {}
    except Exception:
        logger.debug("Mode status read failed; using defaults", exc_info=True)
        status = {}
    soc = status.get(_SOC_KEY[mode], _SOC_DEFAULT[mode])
    if dry_run:
        return True, (f"Dry run: I would switch to {name} (setting {soc}%) but nothing was sent. "
                      "Turn off mode_switch_dry_run in ~/.franklinwh.json to make the button real.")
    try:
        client.set_mode(gateway, mode, soc)
    except Exception as e:
        logger.warning("Mode switch to %s failed: %s", mode, e)
        return False, f"Couldn't switch to {name}: {e}. Do it in the FranklinWH app."
    return True, f"Switched to {name} (setting {soc}%). Check the FranklinWH app to confirm."

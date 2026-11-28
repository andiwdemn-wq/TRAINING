"""Login bei Garmin Connect mit zwischengespeicherten Session-Tokens und 2FA."""

import os
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import Garmin


class NeedsInteractiveLogin(Exception):
    """Garmin verlangt einen 2FA-Code, aber niemand sitzt vor dem Terminal."""


def _prompt_mfa() -> str:
    return input("2FA-Code von Garmin (E-Mail/App): ").strip()


def _no_mfa() -> str:
    raise NeedsInteractiveLogin("Garmin verlangt einen 2FA-Code")


def login(root: Path, token_dir: Path, interactive: bool = True) -> Garmin:
    load_dotenv(root / ".env")
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    token_dir.mkdir(parents=True, exist_ok=True)
    api = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa if interactive else _no_mfa)
    # Gespeicherte Tokens werden zuerst probiert; nur wenn sie fehlen oder
    # abgelaufen sind, folgt ein Login mit E-Mail/Passwort (ggf. mit 2FA).
    # Die neuen Tokens landen danach wieder in token_dir.
    api.login(str(token_dir))
    return api

"""Dienst- und Trainingsplan aus dem Google-Kalender (private iCal-Adresse in .env).

Übernommen werden nur Dienste und Trainings; alle anderen (privaten) Termine
werden verworfen und nie gespeichert.
"""

import os
import re
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

TZ = ZoneInfo("Europe/Berlin")
SHIFTS = {  # Titel im Kalender -> Kurzname
    "frühdienst": "Früh", "fruehdienst": "Früh", "spätdienst": "Spät", "spaetdienst": "Spät",
    "nachtdienst": "Nacht", "frei": "Frei", "urlaub": "Urlaub", "zwischendienst": "Zwischen",
}


def _words(env: str, default: str) -> list[str]:
    """Stichwörter aus .env (kommagetrennt), z. B. PLAN_RUN_WORDS=🏃,lauf,run"""
    return [w.strip().lower() for w in os.getenv(env, default).split(",") if w.strip()]


def _kind(summary: str):
    s = summary.strip().lower()
    if s in SHIFTS:
        return "shift"
    if any(w in s for w in _words("PLAN_RUN_WORDS", "🏃,lauf:")) or s.startswith("lauf"):
        return "run"
    if any(w in s for w in _words("PLAN_GYM_WORDS", "🏋,gym,krafttraining")):
        return "gym"
    return None


def _local(v):
    """Datum oder (lokale) Uhrzeit eines iCal-Werts."""
    if isinstance(v, datetime):
        if v.tzinfo:
            v = v.astimezone(TZ)
        return v.date(), v.strftime("%H:%M")
    return v, None


def fetch_plan(days_back: int = 70, days_ahead: int = 100) -> list[dict] | None:
    url = os.getenv("GOOGLE_CALENDAR_ICS", "").strip()
    if not url:
        return None
    with urllib.request.urlopen(url, timeout=30) as resp:
        cal = icalendar.Calendar.from_ical(resp.read())
    start = date.today() - timedelta(days=days_back)
    end = date.today() + timedelta(days=days_ahead)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        summary = str(ev.get("SUMMARY", ""))
        kind = _kind(summary)
        if not kind:
            continue  # private Termine: verwerfen
        d0, t0 = _local(ev.decoded("DTSTART"))
        _, t1 = _local(ev.decoded("DTEND")) if ev.get("DTEND") else (None, None)
        item = {"date": d0.isoformat(), "kind": kind, "start": t0, "end": t1}
        if kind == "shift":
            item["title"] = SHIFTS[summary.strip().lower()]
        else:
            item["title"] = re.sub(r"^\W+\s*", "", summary).replace("Lauf:", "").strip() or summary
            desc = str(ev.get("DESCRIPTION", "") or "")
            lines = [l.strip() for l in desc.splitlines() if l.strip()]
            when = next((l[5:].strip() for l in lines if l.lower().startswith("wann:")), None)
            item["when"] = when
            item["details"] = [l for l in lines if not l.lower().startswith(("wann:", "garmin-workout"))][:4]
        out.append(item)
    return sorted(out, key=lambda x: (x["date"], {"shift": 0, "run": 1, "gym": 2}[x["kind"]]))

"""Garmin-Daten laden, lokal speichern und dashboard.html erzeugen.

    python update_dashboard.py              # Login, fehlende Tage nachladen, HTML bauen
    python update_dashboard.py --no-fetch   # nur HTML aus lokalen Daten neu bauen
    python update_dashboard.py --days 730   # Zeitfenster erweitern
    pythonw update_dashboard.py --auto      # Hintergrundlauf (Aufgabenplanung): ohne Rückfragen, Log in data/sync.log
"""

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from garmin_dash.storage import Store
from garmin_dash.transform import build

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TEMPLATE = ROOT / "garmin_dash" / "template.html"
OUTPUT = ROOT / "dashboard.html"
LOCK = DATA / "sync.lock"
LOG = DATA / "sync.log"
CLOUD = os.getenv("GITHUB_ACTIONS") == "true"


def render(data: dict):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    tmp = OUTPUT.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(OUTPUT)  # atomar, damit ein offener Browser nie eine halbe Datei sieht
    # Zusätzliche Kopien, z. B. auf dem Desktop (DASHBOARD_COPY in .env, mehrere mit ; trennen)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    for target in filter(None, (s.strip() for s in os.getenv("DASHBOARD_COPY", "").split(";"))):
        try:
            Path(target).write_text(html, encoding="utf-8")
        except OSError as e:
            print(f"Kopie nach {target} fehlgeschlagen: {e}")
    # Verschlüsselte Kopie fürs iPhone (nur wenn GITHUB_TOKEN/GITHUB_REPO/DASHBOARD_PASSWORD gesetzt)
    try:
        from garmin_dash.publish import publish
        url = publish(html, DATA)
        if url:
            print(f"iPhone-Version aktualisiert: {url}")
    except ImportError:
        print("iPhone-Upload: Paket fehlt – einmal `pip install -r requirements.txt` ausführen.")
    except Exception as e:  # noqa: BLE001 – Upload darf das lokale Dashboard nie verhindern
        print(f"iPhone-Upload fehlgeschlagen: {str(e)[:200]}")


def acquire_lock() -> bool:
    """Verhindert zwei gleichzeitige Läufe (z. B. Autostart + Doppelklick)."""
    DATA.mkdir(exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Verwaiste Sperre (Absturz, PC ausgeschaltet) nach 2 Stunden ignorieren
        if datetime.now().timestamp() - LOCK.stat().st_mtime > 7200:
            LOCK.unlink(missing_ok=True)
            return acquire_lock()
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def setup_auto_logging():
    DATA.mkdir(exist_ok=True)
    if CLOUD:  # in GitHub Actions direkt ins Protokoll der Aktion schreiben
        print(f"=== Cloud-Lauf {datetime.now():%d.%m.%Y %H:%M} ===")
        return
    if LOG.exists() and LOG.stat().st_size > 1_000_000:  # Log klein halten
        LOG.replace(LOG.with_suffix(".log.old"))
    sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)
    print(f"\n=== Automatischer Lauf {datetime.now():%d.%m.%Y %H:%M} ===")


def sync(store: Store, args) -> int:
    from garmin_dash.auth import login
    from garmin_dash.fetch import Abort, Fetcher

    try:
        print("Anmeldung bei Garmin Connect …")
        api = login(ROOT, DATA / "tokens", interactive=not args.auto)
        name = api.get_full_name() or ""
        store.set_meta("name", name)
        print(f"Angemeldet als {name or 'unbekannt'}")
    except Exception as e:  # noqa: BLE001 – jeder Login-Fehler landet im Dashboard-Hinweis
        msg = str(e)
        needs_2fa = "2FA" in msg or "MFA" in msg.upper()
        store.set_meta("last_error", "login_2fa" if needs_2fa else f"login: {msg[:200]}")
        print(f"Login fehlgeschlagen: {msg}")
        return 1

    try:
        end = date.today()
        fetcher = Fetcher(api, store)
        try:
            fetcher.run(end - timedelta(days=args.days - 1), end, args.refetch)
        finally:
            # Garmin tauscht die Tokens bei jeder Erneuerung aus -> aktuellen Stand sichern (wird mit abgeglichen)
            try:
                api.client.dump(str(DATA / "tokens"))
            except Exception as e:  # noqa: BLE001
                print(f"Tokens nicht gespeichert: {e}")
        store.set_meta("last_sync", datetime.now().isoformat(timespec="seconds"))
        store.set_meta("last_error", "")
        if fetcher.errors:
            print(f"{fetcher.errors} Abruf(e) fehlgeschlagen – werden beim nächsten Lauf erneut versucht.")
        return 0
    except Abort as e:
        store.set_meta("last_error", f"abort: {e}")
        print(f"Abgebrochen: {e}")
        return 1
    except KeyboardInterrupt:
        print("\nAbgebrochen – bisher geladene Daten bleiben gespeichert.")
        return 130


def sync_plan(store: Store):
    """Dienst- und Trainingsplan aus dem Google-Kalender (unabhängig vom Garmin-Login)."""
    from dotenv import load_dotenv
    from garmin_dash.plan import fetch_plan

    load_dotenv(ROOT / ".env")
    try:
        plan = fetch_plan()
    except Exception as e:  # noqa: BLE001 – Kalender ist optional, Dashboard trotzdem bauen
        print(f"Kalender nicht abrufbar: {str(e)[:150]}")
        return
    if plan is None:
        return
    store.put_raw("plan", "calendar", plan)
    store.set_meta("plan_sync", datetime.now().isoformat(timespec="seconds"))
    print(f"Kalender: {len(plan)} Dienste/Trainings übernommen")


def pull_cloud_state():
    """Neueren Stand (Datenbank + Garmin-Tokens) aus dem GitHub-Repository holen, falls eingerichtet."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    try:
        from garmin_dash.publish import pull_state
        status = pull_state(DATA)
    except Exception as e:  # noqa: BLE001 – ohne Abgleich mit lokalem Stand weitermachen
        print(f"Cloud-Abgleich nicht möglich, arbeite mit lokalem Stand: {str(e)[:200]}")
        return
    if status == "updated":
        print("Neuerer Stand aus der Cloud übernommen.")


def main():
    ap = argparse.ArgumentParser(description="Garmin-Dashboard aktualisieren")
    ap.add_argument("--days", type=int, default=365, help="Zeitfenster in Tagen (Standard 365)")
    ap.add_argument("--refetch", type=int, default=3, help="die letzten N Tage immer neu laden (Standard 3)")
    ap.add_argument("--no-fetch", action="store_true", help="nichts abrufen, nur HTML erzeugen")
    ap.add_argument("--auto", action="store_true", help="Hintergrundlauf ohne Rückfragen, Ausgabe in data/sync.log")
    ap.add_argument("--debug", action="store_true", help="ausführliches Logging")
    args = ap.parse_args()

    if args.auto:
        setup_auto_logging()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)

    if not acquire_lock():
        print("Es läuft bereits eine Aktualisierung – dieser Start wird übersprungen.")
        sys.exit(0)
    try:
        pull_cloud_state()
        if CLOUD and not (DATA / "garmin.db").exists():
            print("Kein gespeicherter Stand in der Cloud – bitte zuerst einmal am PC aktualisieren.")
            sys.exit(1)
        store = Store(DATA / "garmin.db")
        exit_code = 0 if args.no_fetch else sync(store, args)
        if not args.no_fetch:
            sync_plan(store)
        data = build(store)
        store.close()
        render(data)
        print(f"Dashboard geschrieben: {OUTPUT}  ({len(data['daily'])} Tage, {len(data['activities'])} Aktivitäten)")
    finally:
        LOCK.unlink(missing_ok=True)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

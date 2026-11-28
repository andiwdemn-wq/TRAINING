"""Inkrementeller Abruf aller Datenquellen mit Pausen und Fehlerbehandlung."""

import time
from datetime import date, timedelta

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from .storage import Store

PAUSE = 0.6            # Sekunden zwischen zwei Requests
RATE_LIMIT_WAIT = 120  # Sekunden Wartezeit nach HTTP 429
CHUNK_DAYS = 28        # max. Tage pro Bereichsabfrage

STRENGTH_TYPES = {"strength_training"}


class Abort(Exception):
    """Sync abbrechen (Rate-Limit dauerhaft, Login ungültig)."""


def _days(start: date, end: date) -> list[str]:
    n = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(n + 1)]


def _chunks(days: list[str], size: int = CHUNK_DAYS) -> list[tuple[str, str]]:
    """Fasst fehlende Tage zu zusammenhängenden Blöcken (max. `size` Tage) zusammen."""
    out, run = [], []
    for d in sorted(days):
        if run and (date.fromisoformat(d) - date.fromisoformat(run[-1])).days != 1 or len(run) == size:
            out.append((run[0], run[-1]))
            run = []
        run.append(d)
    if run:
        out.append((run[0], run[-1]))
    return out


# --- Tagesquellen -----------------------------------------------------------

def _trim_sleep(p):
    if not p:
        return None
    keep = ("dailySleepDTO", "avgOvernightHrv", "hrvStatus", "restingHeartRate",
            "bodyBatteryChange", "avgSleepStress")
    return {k: p.get(k) for k in keep if k in p}


def _summary_empty(p):
    return not p or (p.get("totalSteps") is None and p.get("restingHeartRate") is None)


def _sleep_empty(p):
    return not p or not (p.get("dailySleepDTO") or {}).get("sleepTimeSeconds")


def _status_empty(p):
    return not p or not any(
        p.get(k) for k in ("mostRecentTrainingStatus", "mostRecentVO2Max", "mostRecentTrainingLoadBalance")
    )


DAILY_SOURCES = {
    # name: (Abruf, Nachbearbeitung, "leer?"-Prüfung)
    "summary": (lambda api, d: api.get_user_summary(d), None, _summary_empty),
    "sleep": (lambda api, d: api.get_sleep_data(d), _trim_sleep, _sleep_empty),
    "training_status": (lambda api, d: api.get_training_status(d), None, _status_empty),
}

RANGE_SOURCES = {
    "hrv": lambda api, s, e: api.get_hrv_data_range(s, e),
    "readiness": lambda api, s, e: api.get_training_readiness_range(s, e),
    "vo2max": lambda api, s, e: api.get_max_metrics_range(s, e),
    "race": lambda api, s, e: api.get_race_predictions(s, e, "daily"),
    "body": lambda api, s, e: api.get_body_composition(s, e),
    "endurance": lambda api, s, e: api.get_endurance_score(s, e, "daily"),
}

# Nur der aktuelle Stand, bei jedem Lauf einmal abgerufen (Schlüssel = heutiges Datum)
LATEST_SOURCES = {
    "fitness_age": lambda api, d: api.get_fitnessage_data(d),
    "lactate": lambda api, d: api.get_lactate_threshold(),
    "endurance_day": lambda api, d: api.get_endurance_score(d),
}


class Fetcher:
    def __init__(self, api, store: Store, log=print):
        self.api = api
        self.store = store
        self.log = log
        self.errors = 0

    def _call(self, fn, *args):
        """Ein Request mit Pause; bei Rate-Limit einmal warten und wiederholen."""
        for attempt in (1, 2):
            time.sleep(PAUSE)
            try:
                return fn(self.api, *args)
            except GarminConnectTooManyRequestsError:
                if attempt == 2:
                    raise Abort("Garmin-Rate-Limit erreicht – später erneut starten, der Fortschritt ist gespeichert.")
                self.log(f"  Rate-Limit – warte {RATE_LIMIT_WAIT}s …")
                time.sleep(RATE_LIMIT_WAIT)
            except GarminConnectAuthenticationError as e:
                raise Abort(f"Login ungültig: {e}")

    def _safe(self, source, days, fn, *args):
        try:
            return True, self._call(fn, *args)
        except Abort:
            raise
        except (GarminConnectConnectionError, Exception) as e:  # noqa: BLE001 – weitermachen
            self.errors += 1
            self.store.mark(source, days, "error", str(e)[:300])
            self.log(f"  Fehler {source} {days[0]}: {str(e)[:120]}")
            return False, None

    def run(self, start: date, end: date, refetch_days: int):
        all_days = _days(start, end)
        recent = set(all_days[-refetch_days:]) if refetch_days else set()

        def missing(source):
            done = self.store.done_days(source)
            return [d for d in all_days if d not in done or d in recent]

        # Bereichsquellen zuerst (wenige Requests, schnell)
        for source, fn in RANGE_SOURCES.items():
            todo = missing(source)
            chunks = _chunks(todo)
            if chunks:
                self.log(f"{source}: {len(todo)} Tage in {len(chunks)} Abfrage(n)")
            for s, e in chunks:
                days = _days(date.fromisoformat(s), date.fromisoformat(e))
                ok, payload = self._safe(source, days, fn, s, e)
                if ok:
                    self.store.put_raw(source, f"{s}_{e}", payload)
                    self.store.mark(source, days, "ok" if payload else "empty")

        today = all_days[-1]
        for source, fn in LATEST_SOURCES.items():
            ok, payload = self._safe(source, [today], fn, today)
            if ok:
                self.store.put_raw(source, today, payload)
                self.store.mark(source, [today], "ok" if payload else "empty")

        self._activities(missing("activities"))

        # Tagesquellen, neueste Tage zuerst – bei Abbruch sind die aktuellen Daten schon da
        for source, (fn, post, is_empty) in DAILY_SOURCES.items():
            todo = sorted(missing(source), reverse=True)
            if todo:
                self.log(f"{source}: {len(todo)} Tage")
            for i, d in enumerate(todo, 1):
                ok, payload = self._safe(source, [d], fn, d)
                if ok:
                    if post:
                        payload = post(payload)
                    self.store.put_raw(source, d, payload)
                    self.store.mark(source, [d], "empty" if is_empty(payload) else "ok")
                if i % 25 == 0:
                    self.log(f"  {source}: {i}/{len(todo)}")

    def _activities(self, todo):
        chunks = _chunks(todo, size=90)
        if chunks:
            self.log(f"activities: {len(todo)} Tage in {len(chunks)} Abfrage(n)")
        for s, e in chunks:
            days = _days(date.fromisoformat(s), date.fromisoformat(e))
            ok, acts = self._safe("activities", days, lambda api, a, b: api.get_activities_by_date(a, b), s, e)
            if not ok:
                continue
            for act in acts or []:
                self.store.put_activity(act)
            self.store.mark("activities", days, "ok")

        # Sätze/Wiederholungen für Krafteinheiten, einmalig pro Aktivität
        for act in self.store.activities():
            aid = act.get("activityId")
            if (act.get("activityType") or {}).get("typeKey") in STRENGTH_TYPES and not self.store.has_sets(aid):
                ok, sets = self._safe("exercise_sets", [act.get("startTimeLocal", "")[:10]],
                                      lambda api, i: api.get_activity_exercise_sets(i), aid)
                if ok:
                    self.store.put_sets(aid, sets)

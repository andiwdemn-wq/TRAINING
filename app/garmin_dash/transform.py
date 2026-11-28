"""Rohantworten → kompakte Tageswerte und Aktivitätenliste für das Dashboard.

Garmin-Antworten sind nicht dokumentiert und variieren je nach Gerät. Alles wird
defensiv gelesen: Fehlt ein Feld, bleibt der Wert leer (→ "keine Daten").
"""

from datetime import datetime

from .storage import Store

RUN_TYPES = {"running", "trail_running", "treadmill_running", "track_running", "indoor_running",
             "street_running", "ultra_run", "virtual_run", "obstacle_run"}
STRENGTH_TYPES = {"strength_training"}


def g(obj, *path, default=None):
    """Sicherer Zugriff auf verschachtelte Felder: g(x, 'a', 'b', 0, 'c')."""
    for p in path:
        if obj is None:
            return default
        try:
            obj = obj[p]
        except (KeyError, IndexError, TypeError):
            return default
    return default if obj is None else obj


def records(payload, *keys):
    """Liefert die Datensatzliste aus einer Bereichsantwort (Liste oder dict mit Liste)."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in keys:
            if isinstance(payload.get(k), list):
                return [r for r in payload[k] if isinstance(r, dict)]
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def rnd(x, n=1):
    return None if x is None else round(float(x), n)


def _set(daily, day, **vals):
    if not day:
        return
    row = daily.setdefault(day[:10], {})
    for k, v in vals.items():
        if v is not None:
            row[k] = v


def _primary(device_map):
    """Aus {deviceId: {...}} den Eintrag des primären Trainingsgeräts wählen."""
    if not isinstance(device_map, dict) or not device_map:
        return None
    vals = [v for v in device_map.values() if isinstance(v, dict)]
    for v in vals:
        if v.get("primaryTrainingDevice"):
            return v
    return vals[0] if vals else None


def build(store: Store) -> dict:
    daily: dict = {}

    for day, p in store.iter_raw("summary"):
        if not p:
            continue
        _set(daily, day,
             steps=p.get("totalSteps"), step_goal=p.get("dailyStepGoal"),
             dist_km=rnd((p.get("totalDistanceMeters") or 0) / 1000, 2) if p.get("totalDistanceMeters") else None,
             kcal=rnd(p.get("totalKilocalories"), 0), kcal_active=rnd(p.get("activeKilocalories"), 0),
             im_mod=p.get("moderateIntensityMinutes"), im_vig=p.get("vigorousIntensityMinutes"),
             im_goal=p.get("intensityMinutesGoal"),
             rhr=p.get("restingHeartRate"),
             stress=p.get("averageStressLevel") if (p.get("averageStressLevel") or -1) >= 0 else None,
             stress_max=p.get("maxStressLevel") if (p.get("maxStressLevel") or -1) >= 0 else None,
             bb_high=p.get("bodyBatteryHighestValue"), bb_low=p.get("bodyBatteryLowestValue"),
             bb_charged=p.get("bodyBatteryChargedValue"), bb_drained=p.get("bodyBatteryDrainedValue"),
             bb_wake=p.get("bodyBatteryAtWakeTime"),
             floors=rnd(p.get("floorsAscended"), 0))

    for day, p in store.iter_raw("sleep"):
        s = g(p, "dailySleepDTO")
        if not s or not s.get("sleepTimeSeconds"):
            continue
        _set(daily, day,
             sleep_s=s.get("sleepTimeSeconds"), deep_s=s.get("deepSleepSeconds"),
             light_s=s.get("lightSleepSeconds"), rem_s=s.get("remSleepSeconds"),
             awake_s=s.get("awakeSleepSeconds"),
             sleep_score=g(s, "sleepScores", "overall", "value"),
             sleep_start=s.get("sleepStartTimestampLocal"), sleep_end=s.get("sleepEndTimestampLocal"))
        if p.get("restingHeartRate") and "rhr" not in daily.get(day, {}):
            _set(daily, day, rhr=p.get("restingHeartRate"))

    for _, p in store.iter_raw("hrv"):
        for r in records(p, "hrvSummaries"):
            _set(daily, r.get("calendarDate"),
                 hrv=r.get("lastNightAvg"), hrv_week=r.get("weeklyAvg"), hrv_status=r.get("status"),
                 hrv_base_low=g(r, "baseline", "balancedLow"), hrv_base_high=g(r, "baseline", "balancedUpper"))

    # Readiness: mehrere Einträge pro Tag möglich → den jüngsten nehmen
    latest: dict = {}
    for _, p in store.iter_raw("readiness"):
        for r in records(p):
            d = r.get("calendarDate")
            ts = r.get("timestampLocal") or r.get("timestamp") or ""
            if d and (d not in latest or ts >= latest[d][0]):
                latest[d] = (ts, r)
    for d, (_, r) in latest.items():
        _set(daily, d, ready=r.get("score"), ready_level=r.get("level"))

    for _, p in store.iter_raw("vo2max"):
        for r in records(p):
            _set(daily, g(r, "generic", "calendarDate"),
                 vo2=rnd(g(r, "generic", "vo2MaxPreciseValue") or g(r, "generic", "vo2MaxValue")))

    for day, p in store.iter_raw("training_status"):
        if not p:
            continue
        ts = _primary(g(p, "mostRecentTrainingStatus", "latestTrainingStatusData"))
        if ts and (ts.get("calendarDate") or day) == day:
            atl = ts.get("acuteTrainingLoadDTO") or {}
            _set(daily, day,
                 ts_phrase=ts.get("trainingStatusFeedbackPhrase"),
                 acute=atl.get("dailyTrainingLoadAcute"), chronic=atl.get("dailyTrainingLoadChronic"),
                 acwr=rnd(atl.get("dailyAcuteChronicWorkloadRatio"), 2), acwr_status=atl.get("acwrStatus"))
        lb = _primary(g(p, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap"))
        if lb and (lb.get("calendarDate") or day) == day:
            _set(daily, day,
                 load_low=rnd(lb.get("monthlyLoadAerobicLow"), 0), load_high=rnd(lb.get("monthlyLoadAerobicHigh"), 0),
                 load_ana=rnd(lb.get("monthlyLoadAnaerobic"), 0))
        v = g(p, "mostRecentVO2Max", "generic")
        if v and v.get("calendarDate") == day and "vo2" not in daily.get(day, {}):
            _set(daily, day, vo2=rnd(v.get("vo2MaxPreciseValue") or v.get("vo2MaxValue")))

    for _, p in store.iter_raw("body"):
        for r in records(p, "dateWeightList"):
            w = r.get("weight")
            _set(daily, r.get("calendarDate"),
                 weight=rnd(w / 1000) if w else None, bmi=rnd(r.get("bmi")),
                 fat=rnd(r.get("bodyFat")), water=rnd(r.get("bodyWater")),
                 muscle=rnd(r.get("muscleMass") / 1000) if r.get("muscleMass") else None,
                 bone=rnd(r.get("boneMass") / 1000) if r.get("boneMass") else None)

    for _, p in store.iter_raw("race"):
        for r in records(p):
            _set(daily, r.get("calendarDate"),
                 t5=r.get("time5K"), t10=r.get("time10K"), thm=r.get("timeHalfMarathon"), tm=r.get("timeMarathon"))

    for _, p in store.iter_raw("endurance"):
        # Bereichsantwort: {"groupMap": {"YYYY-MM-DD": {"groupAverage": …}}} oder Datensatzliste
        for d, v in (g(p, "groupMap") or {}).items():
            if isinstance(v, dict):
                _set(daily, d, endurance=v.get("groupAverage") or v.get("groupMax"))
        for r in records(p, "enduranceScoreDTOs"):
            _set(daily, r.get("calendarDate"), endurance=r.get("overallScore"))

    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "daily": dict(sorted(daily.items())),
        "activities": _activities(store),
        "latest": _latest(store),
        "plan": _newest(store, "plan") or [],
        "sync": {k: store.get_meta(k) for k in ("last_sync", "last_error", "name")},
        "sources": store.status_counts(),
    }


def _newest(store: Store, source: str):
    out = None
    for _, p in store.iter_raw(source):
        if p:
            out = p
    return out


def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _latest(store: Store) -> dict:
    """Aktueller Stand von Werten, die Garmin nur als 'neuesten Wert' liefert."""
    out: dict = {}

    fa = _newest(store, "fitness_age")
    if fa:
        out["fitness_age"] = rnd(_first(fa, "fitnessAge", "fitness_age"))
        out["age"] = _first(fa, "chronologicalAge", "chronological_age")
        out["fitness_age_best"] = rnd(_first(fa, "achievableFitnessAge"))
        out["fitness_age_prev"] = rnd(_first(fa, "previousFitnessAge"))

    lt = _newest(store, "lactate")
    if lt:
        shr = g(lt, "speed_and_heart_rate") or {}
        speed = shr.get("speed")
        if speed:
            speed = speed * 10 if speed < 1.5 else speed  # Garmin liefert teils 1/10 m/s
            out["lt_pace"] = rnd(1000 / speed, 0)          # s/km
        out["lt_hr"] = shr.get("heartRate")
        out["lt_date"] = (shr.get("calendarDate") or "")[:10] or None
        out["ftp"] = g(lt, "power", "functionalThresholdPower")

    ed = _newest(store, "endurance_day")
    if ed:
        out["endurance"] = _first(ed, "overallScore", "enduranceScore")
        out["endurance_class"] = _first(ed, "classification")
        levels = [("Fortgeschritten", "classificationLowerLimitIntermediate"), ("Trainiert", "classificationLowerLimitTrained"),
                  ("Gut trainiert", "classificationLowerLimitWellTrained"), ("Experte", "classificationLowerLimitExpert"),
                  ("Überragend", "classificationLowerLimitSuperior"), ("Elite", "classificationLowerLimitElite")]
        out["endurance_levels"] = [[name, ed[k]] for name, k in levels if ed.get(k)]
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _activities(store: Store) -> list[dict]:
    sets = store.sets()
    out = []
    for a in store.activities():
        type_key = g(a, "activityType", "typeKey", default="other")
        if type_key in RUN_TYPES:
            cat = "run"
        elif type_key in STRENGTH_TYPES:
            cat = "strength"
        elif "cycl" in type_key or "bik" in type_key:
            cat = "bike"
        elif "swim" in type_key:
            cat = "swim"
        elif type_key in ("walking", "hiking", "casual_walking", "speed_walking"):
            cat = "walk"
        else:
            cat = "other"
        dist = a.get("distance") or 0
        dur = a.get("duration") or 0
        row = {
            "id": a.get("activityId"), "day": (a.get("startTimeLocal") or "")[:10],
            "time": (a.get("startTimeLocal") or "")[11:16], "name": a.get("activityName"),
            "type": type_key, "cat": cat,
            "dist_km": rnd(dist / 1000, 2) if dist else None, "dur_s": rnd(dur, 0),
            "moving_s": rnd(a.get("movingDuration"), 0),
            "hr": rnd(a.get("averageHR"), 0), "hr_max": rnd(a.get("maxHR"), 0),
            "elev": rnd(a.get("elevationGain"), 0), "kcal": rnd(a.get("calories"), 0),
            "te": rnd(a.get("aerobicTrainingEffect")), "load": rnd(a.get("activityTrainingLoad"), 0),
        }
        if cat == "run" and dist:
            row["pace"] = rnd((a.get("movingDuration") or dur) / (dist / 1000), 0)  # s/km
        if cat == "strength":
            row.update(_strength(a, sets.get(a.get("activityId"))))
        out.append({k: v for k, v in row.items() if v is not None})
    return out


def _strength(a: dict, s) -> dict:
    active = [x for x in (g(s, "exerciseSets") or []) if x.get("setType") == "ACTIVE"]
    # Ein einziger "Satz" über die halbe Einheit heißt: Sätze wurden an der Uhr nicht
    # einzeln erfasst. Dann keine Sätze/Wiederholungen ausweisen statt falscher Zahlen.
    if len(active) == 1 and (active[0].get("duration") or 0) > 600:
        return {"sets_unknown": True}
    if active:
        reps = sum(x.get("repetitionCount") or 0 for x in active)
        volume = sum((x.get("repetitionCount") or 0) * (x.get("weight") or 0) for x in active) / 1000
        cats: dict = {}
        for x in active:
            c = g(x, "exercises", 0, "category")
            if c and c != "UNKNOWN":
                cats[c] = cats.get(c, 0) + 1
        return {"sets": len(active), "reps": reps or None, "volume_kg": rnd(volume, 0) or None,
                "exercises": sorted(cats, key=cats.get, reverse=True)[:5]}
    if (a.get("activeSets") or 0) <= 1:
        return {"sets_unknown": True}
    return {"sets": a.get("activeSets"), "reps": a.get("totalReps")}

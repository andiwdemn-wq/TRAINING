"""SQLite-Ablage: Rohantworten von Garmin plus Sync-Status pro Quelle und Tag."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw (
    source     TEXT NOT NULL,
    key        TEXT NOT NULL,          -- Tag (YYYY-MM-DD) oder Bereich (start_end)
    payload    TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS sync_state (
    source     TEXT NOT NULL,
    day        TEXT NOT NULL,
    status     TEXT NOT NULL,          -- ok | empty | error
    error      TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, day)
);
CREATE TABLE IF NOT EXISTS activities (
    id         INTEGER PRIMARY KEY,
    day        TEXT NOT NULL,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exercise_sets (
    activity_id INTEGER PRIMARY KEY,
    payload     TEXT,
    fetched_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.commit()
        self.db.close()

    # --- Rohdaten -----------------------------------------------------------
    def put_raw(self, source: str, key: str, payload):
        self.db.execute(
            "INSERT OR REPLACE INTO raw VALUES (?,?,?,?)",
            (source, key, json.dumps(payload, ensure_ascii=False), _now()),
        )

    def iter_raw(self, source: str):
        """Liefert (key, payload) in Abrufreihenfolge, damit neuere Abrufe ältere überschreiben."""
        for key, payload in self.db.execute(
            "SELECT key, payload FROM raw WHERE source=? ORDER BY fetched_at, key", (source,)
        ):
            yield key, json.loads(payload) if payload else None

    # --- Sync-Status --------------------------------------------------------
    def mark(self, source: str, days, status: str, error: str | None = None):
        now = _now()
        self.db.executemany(
            "INSERT OR REPLACE INTO sync_state VALUES (?,?,?,?,?)",
            [(source, d, status, error, now) for d in days],
        )
        self.db.commit()

    def done_days(self, source: str) -> set[str]:
        return {
            r[0]
            for r in self.db.execute(
                "SELECT day FROM sync_state WHERE source=? AND status IN ('ok','empty')", (source,)
            )
        }

    def status_counts(self) -> dict:
        out: dict = {}
        for source, status, n in self.db.execute(
            "SELECT source, status, COUNT(*) FROM sync_state GROUP BY source, status"
        ):
            out.setdefault(source, {})[status] = n
        return out

    # --- Aktivitäten --------------------------------------------------------
    def put_activity(self, act: dict):
        day = (act.get("startTimeLocal") or "")[:10]
        self.db.execute(
            "INSERT OR REPLACE INTO activities VALUES (?,?,?,?)",
            (act["activityId"], day, json.dumps(act, ensure_ascii=False), _now()),
        )

    def activities(self) -> list[dict]:
        return [json.loads(p) for (p,) in self.db.execute("SELECT payload FROM activities ORDER BY day")]

    def has_sets(self, activity_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM exercise_sets WHERE activity_id=?", (activity_id,)
        ).fetchone() is not None

    def put_sets(self, activity_id: int, payload):
        self.db.execute(
            "INSERT OR REPLACE INTO exercise_sets VALUES (?,?,?)",
            (activity_id, json.dumps(payload, ensure_ascii=False), _now()),
        )
        self.db.commit()

    def sets(self) -> dict[int, dict]:
        return {
            aid: json.loads(p) if p else None
            for aid, p in self.db.execute("SELECT activity_id, payload FROM exercise_sets")
        }

    # --- Meta ---------------------------------------------------------------
    def set_meta(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
        self.db.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

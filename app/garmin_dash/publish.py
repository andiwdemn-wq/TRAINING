"""iPhone-Version und Cloud-Abgleich über ein GitHub-Repository.

- Seite (GitHub Pages): index.html + data.json. data.json ist mit DASHBOARD_PASSWORD
  verschlüsselt (AES-256-GCM, PBKDF2-SHA256); entschlüsselt wird erst im Browser.
- Cloud-Abgleich (wenn STATE_KEY gesetzt): Datenbank + Garmin-Anmeldeschlüssel liegen als
  app/state.enc im Repository, verschlüsselt mit dem zufälligen 256-Bit-Schlüssel STATE_KEY.
  PC und GitHub Actions holen sich vor jedem Lauf den neuesten Stand (Zähler "gen") und
  laden ihn danach wieder hoch. So bleiben beide Seiten synchron – auch die Garmin-Tokens,
  die bei jeder Erneuerung ausgetauscht werden.
- Jeder Upload ersetzt die komplette Historie (genau ein Commit).
"""

import base64
import gzip
import hashlib
import io
import json
import os
import secrets
import shutil
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

ITER = 600_000
API = "https://api.github.com"
PKG = Path(__file__).resolve().parent
STATIC = PKG / "static"
ROOT = PKG.parent
CLOUD = os.getenv("GITHUB_ACTIONS") == "true"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _aes():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM


# ---------------------------------------------------------------- Konfiguration
def config() -> dict | None:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo = os.getenv("GITHUB_REPO", "").strip().removeprefix("https://github.com/").strip("/")
    password = os.getenv("DASHBOARD_PASSWORD", "")
    if not (token and repo and password):
        return None
    key = os.getenv("STATE_KEY", "").strip()
    return {"token": token, "repo": repo, "password": password,
            "state_key": base64.b64decode(key) if key else None}


class GitHub:
    def __init__(self, token: str, repo: str):
        self.token, self.repo = token, repo

    def _req(self, method, path, body=None, accept="application/vnd.github+json"):
        return urllib.request.Request(
            f"{API}/repos/{self.repo}{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Accept": accept,
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "garmin-dashboard"})

    def call(self, method: str, path: str, body=None):
        with urllib.request.urlopen(self._req(method, path, body), timeout=120) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}

    def raw(self, path: str) -> bytes | None:
        """Dateiinhalt aus dem Repository, None wenn es die Datei (noch) nicht gibt."""
        try:
            with urllib.request.urlopen(self._req("GET", f"/contents/{path}", accept="application/vnd.github.raw+json"),
                                        timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 409):
                return None
            raise


# ---------------------------------------------------------------- lokaler Zustand
def _local(data_dir: Path) -> dict:
    f = data_dir / "publish.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _save_local(data_dir: Path, state: dict):
    data_dir.mkdir(exist_ok=True)
    (data_dir / "publish.json").write_text(json.dumps(state), encoding="utf-8")


def _remote_meta(gh: GitHub) -> dict | None:
    raw = gh.raw("app/state.json")
    return json.loads(raw) if raw else None


# ---------------------------------------------------------------- Verschlüsselung
def encrypt_page(html: str, password: str, salt: bytes) -> str:
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITER, 32)
    iv = secrets.token_bytes(12)
    ct = _aes()(key).encrypt(iv, gzip.compress(html.encode("utf-8"), 9), None)
    return json.dumps({"v": 1, "salt": _b64(salt), "iter": ITER, "iv": _b64(iv), "ct": _b64(ct)})


STATE_FILES = ("garmin.db", "tokens/garmin_tokens.json")


def pack_state(data_dir: Path, key: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name in STATE_FILES:
            if (data_dir / name).exists():
                z.write(data_dir / name, name)
    iv = secrets.token_bytes(12)
    return iv + _aes()(key).encrypt(iv, buf.getvalue(), b"garmin-state")


def unpack_state(blob: bytes, key: bytes, data_dir: Path):
    plain = _aes()(key).decrypt(blob[:12], blob[12:], b"garmin-state")  # wirft bei falschem Schlüssel
    with zipfile.ZipFile(io.BytesIO(plain)) as z:
        for name in STATE_FILES:
            if name not in z.namelist():
                continue
            target = data_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():  # Sicherung des bisherigen Stands
                shutil.copy2(target, target.with_name(target.name + ".bak"))
            tmp = target.with_name(target.name + ".new")
            tmp.write_bytes(z.read(name))
            tmp.replace(target)


# ---------------------------------------------------------------- vor dem Lauf: holen
def pull_state(data_dir: Path) -> str:
    """'off' | 'none' (noch nichts in der Cloud) | 'current' | 'updated'."""
    cfg = config()
    if not cfg or not cfg["state_key"]:
        return "off"
    gh = GitHub(cfg["token"], cfg["repo"])
    remote = _remote_meta(gh)
    if not remote:
        return "none"
    local = _local(data_dir)
    local.setdefault("salt", remote.get("salt"))
    if remote.get("gen", 0) <= local.get("gen", 0) and (data_dir / "garmin.db").exists():
        _save_local(data_dir, local)
        return "current"
    blob = gh.raw("app/state.enc")
    if not blob:
        return "none"
    unpack_state(blob, cfg["state_key"], data_dir)
    local["gen"] = remote.get("gen", 0)
    _save_local(data_dir, local)
    return "updated"


# ---------------------------------------------------------------- nach dem Lauf: hochladen
def _code_files() -> dict[str, bytes]:
    files = {"app/update_dashboard.py": (ROOT / "update_dashboard.py").read_bytes(),
             "app/requirements.txt": (ROOT / "requirements.txt").read_bytes(),
             ".github/workflows/update.yml": (STATIC / "workflow.yml").read_bytes()}
    for p in PKG.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            files["app/" + p.relative_to(ROOT).as_posix()] = p.read_bytes()
    return files


def _upload(gh: GitHub, files: dict[str, bytes]) -> str:
    info = gh.call("GET", "")
    branch = info.get("default_branch") or "main"
    try:
        blobs = {p: gh.call("POST", "/git/blobs", {"content": _b64(c), "encoding": "base64"})["sha"] for p, c in files.items()}
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = Repository noch komplett leer -> ersten Commit anlegen
            raise
        gh.call("PUT", "/contents/README.md", {"message": "Start", "content": _b64(b"Training\n"), "branch": branch})
        return _upload(gh, files)
    tree = gh.call("POST", "/git/trees", {"tree": [
        {"path": p, "mode": "100644", "type": "blob", "sha": s} for p, s in blobs.items()]})["sha"]
    msg = f"Dashboard aktualisiert ({'Cloud' if CLOUD else 'PC'})"
    commit = gh.call("POST", "/git/commits", {"message": msg, "tree": tree, "parents": []})["sha"]
    try:
        gh.call("PATCH", f"/git/refs/heads/{branch}", {"sha": commit, "force": True})
    except urllib.error.HTTPError as e:
        if e.code not in (404, 422):
            raise
        gh.call("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
    return branch


def _ensure_pages(gh: GitHub, branch: str) -> str | None:
    try:
        return gh.call("GET", "/pages").get("html_url")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    try:
        return gh.call("POST", "/pages", {"source": {"branch": branch, "path": "/"}}).get("html_url")
    except urllib.error.HTTPError as e:
        print(f"GitHub Pages konnte nicht eingeschaltet werden ({e.code}) – bitte einmal im Repository "
              f"unter Settings → Pages „Deploy from a branch“ / {branch} / root wählen.")
        return None


def publish(html: str, data_dir: Path) -> str | None:
    """Lädt Seite (+ Cloud-Stand) hoch. Gibt die iPhone-Adresse zurück oder None."""
    cfg = config()
    if not cfg:
        return None
    if len(cfg["password"]) < 10:
        print("iPhone-Upload übersprungen: DASHBOARD_PASSWORD bitte mindestens 10 Zeichen lang wählen.")
        return None

    gh = GitHub(cfg["token"], cfg["repo"])
    local = _local(data_dir)
    try:
        remote = _remote_meta(gh) if cfg["state_key"] else None
        if remote and remote.get("gen", 0) > local.get("gen", 0):
            print("Upload übersprungen: In der Cloud liegt bereits ein neuerer Stand (wird beim nächsten Lauf übernommen).")
            return None
        local.setdefault("salt", (remote or {}).get("salt") or _b64(secrets.token_bytes(16)))
        salt = base64.b64decode(local["salt"])

        files = {
            "index.html": (STATIC / "unlock.html").read_bytes(),
            "data.json": encrypt_page(html, cfg["password"], salt).encode(),
            "icon.png": (STATIC / "icon.png").read_bytes(),
            "icon-512.png": (STATIC / "icon-512.png").read_bytes(),
            "robots.txt": b"User-agent: *\nDisallow: /\n",
            ".nojekyll": b"",
        }
        gen = max(local.get("gen", 0), (remote or {}).get("gen", 0))
        if cfg["state_key"] and (data_dir / "garmin.db").exists():
            gen += 1
            files.update(_code_files())
            files["app/state.enc"] = pack_state(data_dir, cfg["state_key"])
            files["app/state.json"] = json.dumps({
                "gen": gen, "salt": local["salt"], "by": "Cloud" if CLOUD else "PC",
                "at": datetime.now().isoformat(timespec="seconds")}).encode()
        branch = _upload(gh, files)
        local["gen"] = gen
        if not local.get("url"):
            local["url"] = _ensure_pages(gh, branch)
    except urllib.error.HTTPError as e:
        hint = {401: "Token ungültig oder abgelaufen", 403: "Token fehlen Rechte für das Repository",
                404: "Repository nicht gefunden (GITHUB_REPO prüfen, Token-Zugriff auf dieses Repo?)"}.get(e.code, "")
        print(f"iPhone-Upload fehlgeschlagen: HTTP {e.code} {hint}")
        return None
    except OSError as e:
        print(f"iPhone-Upload fehlgeschlagen (Netzwerk): {e}")
        return None
    _save_local(data_dir, local)
    return local.get("url") or f"https://github.com/{cfg['repo']}/settings/pages"

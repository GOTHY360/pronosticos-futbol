"""API-Football (api-sports.io), plan gratuito: 100 consultas al día.

Se usa de forma muy económica (≈2 consultas por noche y 1 por hora en días de partido):
  - /fixtures?date=AAAA-MM-DD  → todos los partidos del día con su ÁRBITRO (1 consulta)
  - /injuries?date=AAAA-MM-DD  → lesionados y sancionados de todos los partidos del día (1 consulta)

La clave NUNCA se guarda en el código. Se lee de:
  1. la variable de entorno APIFOOTBALL_KEY (GitHub Actions: secreto del repositorio), o
  2. el archivo pronosticos/data/apifootball.key (en el PC; la carpeta data/ no se sube a GitHub).
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from ..config import DATA_DIR

BASE = "https://v3.football.api-sports.io"
DAILY_BUDGET = 90          # margen de seguridad bajo el límite de 100
KEY_FILE = DATA_DIR / "apifootball.key"

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_usage (day TEXT PRIMARY KEY, calls INTEGER);
CREATE TABLE IF NOT EXISTS af_fixtures (
    af_id INTEGER PRIMARY KEY, event_id TEXT, date_utc TEXT, league TEXT, country TEXT,
    home TEXT, away TEXT, home_af_id INTEGER, away_af_id INTEGER, referee TEXT, match_score REAL,
    home_goals INTEGER, away_goals INTEGER, status TEXT
);
CREATE INDEX IF NOT EXISTS ix_af_event ON af_fixtures(event_id);
CREATE TABLE IF NOT EXISTS af_injuries (
    af_fixture_id INTEGER, event_id TEXT, team_af_id INTEGER, team TEXT, player TEXT,
    kind TEXT, reason TEXT, fetched_at TEXT,
    PRIMARY KEY (af_fixture_id, team_af_id, player)
);
"""


def api_key() -> str | None:
    k = os.environ.get("APIFOOTBALL_KEY")
    if k:
        return k.strip()
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def ensure_schema(con):
    con.executescript(SCHEMA)


def _calls_today(con) -> int:
    r = con.execute("SELECT calls FROM api_usage WHERE day=?", (datetime.now(timezone.utc).date().isoformat(),)).fetchone()
    return r[0] if r else 0


def _get(con, path: str) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("No hay clave de API-Football configurada.")
    if _calls_today(con) >= DAILY_BUDGET:
        raise RuntimeError("Límite diario de consultas a API-Football alcanzado (se reinicia a medianoche UTC).")
    req = urllib.request.Request(BASE + path, headers={"x-apisports-key": key, "User-Agent": "pronosticos-personal"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API-Football respondió {e.code}") from None
    finally:
        day = datetime.now(timezone.utc).date().isoformat()
        con.execute("INSERT INTO api_usage(day, calls) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1", (day,))
        con.commit()
    errs = data.get("errors")
    if errs and (isinstance(errs, dict) and errs or isinstance(errs, list) and errs):
        raise RuntimeError(f"API-Football: {errs}")
    return data


def status(con) -> dict:
    """Comprueba la clave y el plan (la consulta /status no gasta cupo)."""
    ensure_schema(con)
    key = api_key()
    if not key:
        return {"ok": False, "error": "sin clave"}
    req = urllib.request.Request(BASE + "/status", headers={"x-apisports-key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    resp = d.get("response") or {}
    if not resp:
        return {"ok": False, "error": d.get("errors")}
    sub, req_ = resp.get("subscription", {}), resp.get("requests", {})
    return {"ok": True, "plan": sub.get("plan"), "active": sub.get("active"), "end": sub.get("end"),
            "used_today": req_.get("current"), "limit_day": req_.get("limit_day")}


# ---------------------------------------------------------------------------
# Emparejamiento de nombres (equipos y jugadores) entre API-Football y ESPN
# ---------------------------------------------------------------------------
STOP = {"fc", "cf", "sc", "ac", "afc", "cd", "ca", "club", "de", "del", "la", "el", "the",
        "sv", "vfb", "vfl", "tsg", "1.", "fk", "sk", "if",
        "bk", "as", "ss", "us", "rc", "rcd", "ud", "sd", "cs", "esporte", "clube", "futebol", "football", "calcio"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def _tokens(s: str) -> set:
    return {t for t in norm(s).split() if t not in STOP and len(t) > 1}


def name_score(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    jacc = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    return max(jacc, SequenceMatcher(None, norm(a), norm(b)).ratio())


def player_match(name: str, candidates: dict) -> str | None:
    """candidates: {player_id: nombre}. Coincidencia por apellido + inicial (API-Football usa 'K. Mbappé')."""
    n = norm(name)
    parts = n.split()
    last = parts[-1] if parts else n
    best, best_s = None, 0.0
    for pid, cand in candidates.items():
        c = norm(cand)
        cp = c.split()
        s = SequenceMatcher(None, n, c).ratio()
        if cp and cp[-1] == last:
            s = max(s, 0.85 if (len(parts) < 2 or cp[0][:1] == parts[0][:1]) else 0.7)
        if s > best_s:
            best, best_s = pid, s
    return best if best_s >= 0.8 else None


# ---------------------------------------------------------------------------
# Sincronización diaria
# ---------------------------------------------------------------------------
def sync_day(con, day: str, with_fixtures: bool = True, log=print) -> dict:
    """Descarga partidos (con árbitro) y lesionados de un día, y los empareja con los partidos de ESPN."""
    ensure_schema(con)
    if not api_key():
        return {"enabled": False}
    out = {"enabled": True, "fixtures": 0, "matched": 0, "injuries": 0}
    try:
        if with_fixtures:
            data = _get(con, f"/fixtures?date={day}&timezone=America/Bogota")
            start = (datetime.fromisoformat(day) - timedelta(hours=12)).isoformat()
            end = (datetime.fromisoformat(day) + timedelta(hours=36)).isoformat()
            espn = [dict(r) for r in con.execute(
                "SELECT event_id, date_utc, home_name, away_name FROM matches WHERE date_utc >= ? AND date_utc < ?",
                (start, end))]
            for f in data.get("response", []):
                fx, lg, tm = f["fixture"], f["league"], f["teams"]
                ko = datetime.fromisoformat(fx["date"]).astimezone(timezone.utc)
                best, best_s = None, 0.0
                for e in espn:
                    eko = datetime.fromisoformat(e["date_utc"].replace("Z", "+00:00"))
                    if abs((eko - ko).total_seconds()) > 3 * 3600:
                        continue
                    s = min(name_score(tm["home"]["name"], e["home_name"]), name_score(tm["away"]["name"], e["away_name"]))
                    if s > best_s:
                        best, best_s = e["event_id"], s
                eid = best if best_s >= 0.5 else None   # ambos equipos, misma hora (±3 h); se elige el mejor
                con.execute("""INSERT OR REPLACE INTO af_fixtures(af_id, event_id, date_utc, league, country, home, away,
                               home_af_id, away_af_id, referee, match_score, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (fx["id"], eid, ko.isoformat(), lg.get("name"), lg.get("country"), tm["home"]["name"],
                             tm["away"]["name"], tm["home"]["id"], tm["away"]["id"], fx.get("referee"), best_s,
                             (fx.get("status") or {}).get("short")))
                out["fixtures"] += 1
                out["matched"] += 1 if eid else 0
        # las lesiones se consultan por fecha UTC: el día local de Colombia abarca dos fechas UTC
        nxt = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
        rows = []
        for d in (day, nxt):
            rows += _get(con, f"/injuries?date={d}").get("response", [])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for i in rows:
            fid = (i.get("fixture") or {}).get("id")
            row = con.execute("SELECT event_id FROM af_fixtures WHERE af_id=?", (fid,)).fetchone()
            p, t = i.get("player") or {}, i.get("team") or {}
            con.execute("""INSERT OR REPLACE INTO af_injuries(af_fixture_id, event_id, team_af_id, team, player, kind,
                           reason, fetched_at) VALUES (?,?,?,?,?,?,?,?)""",
                        (fid, row[0] if row else None, t.get("id"), t.get("name"), p.get("name"), p.get("type"),
                         p.get("reason"), now))
            out["injuries"] += 1
        con.commit()
    except RuntimeError as e:
        out["error"] = str(e)
        log(f"  ! API-Football: {e}")
    return out


def event_info(con, event_id: str, home_name: str, away_name: str) -> dict:
    """Árbitro y bajas (lesionados / sancionados) conocidas para un partido de ESPN."""
    ensure_schema(con)
    f = con.execute("SELECT * FROM af_fixtures WHERE event_id=? ORDER BY match_score DESC LIMIT 1", (event_id,)).fetchone()
    if not f:
        return {"available": False}
    out = {"available": True, "referee": f["referee"], "home": [], "away": []}
    for r in con.execute("SELECT * FROM af_injuries WHERE af_fixture_id=?", (f["af_id"],)):
        side = "home" if r["team_af_id"] == f["home_af_id"] else "away"
        out[side].append({"player": r["player"], "kind": r["kind"], "reason": r["reason"]})
    return out

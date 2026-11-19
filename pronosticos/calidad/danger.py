"""Detector de partidos peligrosos: la aplicación aprende a decir NO.

Cada señal tiene una severidad:
- "bloqueo": el partido no se pronostica.
- "advertencia": baja la calidad; dos o más advertencias juntas también bloquean.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone

from ..config import COMPETITIONS, PUBLISH_FRIENDLIES, SELECTION as S

COACH_WORDS = ["sacked", "fired as", "appointed", "appoints", "new manager", "new coach", "new boss",
               "interim manager", "interim coach", "caretaker", "parts ways", "part ways", "takes charge",
               "named manager", "named coach", "named head coach", "arrives at", "despedido", "destituido",
               "nuevo entrenador", "nuevo técnico", "nuevo dt", "técnico interino", "entrenador interino",
               "cesado", "demitido", "novo técnico"]
INJURY_WORDS = ["injury", "injured", "ruled out", "out for", "sidelined", "doubt for", "hamstring",
                "fitness", "suspended", "suspension", "lesión", "lesionado", "baja", "descartado",
                "sancionado", "lesão", "desfalque", "suspenso"]
STOP = {"club", "fc", "cf", "sc", "united", "city", "real", "deportivo", "athletic", "atlético", "atletico",
        "sporting", "de", "la", "del", "los", "las", "do", "da", "afc", "the"}


BOUND = chr(92) + "b"      # límite de palabra en regex
_TOKEN_COUNTS: dict | None = None


def _token_counts(con) -> dict:
    """Cuántos equipos comparten cada palabra del nombre (para no confundir Manchester City/United)."""
    global _TOKEN_COUNTS
    if _TOKEN_COUNTS is None:
        cnt = {}
        names = {r[0] for r in con.execute("SELECT home_name FROM matches UNION SELECT away_name FROM matches")}
        for n in names:
            for t in set(_words(n)):
                cnt[t] = cnt.get(t, 0) + 1
        _TOKEN_COUNTS = cnt
    return _TOKEN_COUNTS


def _words(name: str) -> list[str]:
    return [t for t in re.split(r"[\s\-\.]+", name.lower()) if len(t) > 3 and t not in STOP]


def team_patterns(con, name: str) -> list[re.Pattern]:
    """Palabras exclusivas del equipo (coincidencia de palabra completa) o, si no hay, el nombre completo."""
    cnt = _token_counts(con)
    toks = [t for t in _words(name) if cnt.get(t, 0) <= 1]
    terms = toks or [name.lower()]
    return [re.compile(BOUND + re.escape(t) + BOUND) for t in terms]


def team_tokens(name: str) -> list[str]:
    return _words(name) or [name.lower()]


def news_signals(con, team_name: str, days: int = 30) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    pats = team_patterns(con, team_name)
    out = {"coach": [], "injury": []}
    for r in con.execute("SELECT published, headline, description FROM news WHERE published >= ?", (since,)):
        text = f"{r['headline'] or ''} {r['description'] or ''}".lower()
        if not any(p.search(text) for p in pats):
            continue
        if any(w in text for w in COACH_WORDS):
            out["coach"].append((r["published"], r["headline"]))
        if any(w in text for w in INJURY_WORDS):
            out["injury"].append((r["published"], r["headline"]))
    return out


def odds_movement(con, event_id: str) -> dict | None:
    snaps = con.execute("""SELECT taken_at, home_dec, draw_dec, away_dec FROM odds WHERE event_id=? AND kind='pre'
                           AND home_dec IS NOT NULL AND draw_dec IS NOT NULL AND away_dec IS NOT NULL
                           ORDER BY taken_at""", (event_id,)).fetchall()
    if len(snaps) < 2:
        return None

    def probs(s):
        inv = [1 / s["home_dec"], 1 / s["draw_dec"], 1 / s["away_dec"]]
        t = sum(inv)
        return [x / t for x in inv]
    p0, p1 = probs(snaps[0]), probs(snaps[-1])
    move = [b - a for a, b in zip(p0, p1)]
    return {"first": snaps[0]["taken_at"], "last": snaps[-1]["taken_at"], "move": move,
            "max_abs": max(abs(x) for x in move)}


def detect(con, fx: dict, ctx: dict) -> list[dict]:
    flags = []

    def add(code, sev, msg):
        flags.append({"code": code, "severity": sev, "message": msg})

    comp = COMPETITIONS.get(fx["league"], {})
    if comp.get("kind") == "friendly" and not PUBLISH_FRIENDLIES:
        add("amistoso", "bloqueo", "Amistoso: rotaciones y motivación imposibles de evaluar.")
    if min(ctx["n_hist"]) < S["min_team_matches"] or ctx["form_home"]["n"] < 10 or ctx["form_away"]["n"] < 10:
        add("datos", "bloqueo", "Estadísticas insuficientes: algún equipo tiene menos de 10 partidos.")
    for side, name, res in (("home", fx["home_name"], ctx["res_home"]), ("away", fx["away_name"], ctx["res_away"])):
        if len(res) >= 8:
            sd = statistics.pstdev(res)
            recent = sum(res[:5]) / min(5, len(res))
            if sd > 2.0:          # percentil 95 de los equipos
                add("irregular", "advertencia", f"{name}: rendimiento irregular (desvío {sd:.1f} goles respecto a lo esperado).")
            if abs(recent) > 1.25:  # percentil 95
                add("cambio_forma", "advertencia",
                    f"{name}: cambio reciente de rendimiento ({recent:+.1f} goles/partido vs. lo esperado en los últimos 5).")
        ns = news_signals(con, name)
        if ns["coach"]:
            last = max(n[0] or "" for n in ns["coach"])
            recent_coach = last >= (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
            add("entrenador", "bloqueo" if recent_coach else "advertencia",
                f"{name}: noticias de cambio de entrenador — «{ns['coach'][0][1]}».")
        if len(ns["injury"]) >= 2:
            add("bajas_noticias", "advertencia",
                f"{name}: {len(ns['injury'])} noticias recientes sobre lesiones o sanciones — «{ns['injury'][0][1]}».")
    for t, info in (ctx.get("availability") or {}).items():
        if info.get("suspended"):
            add("suspension", "advertencia", f"Probable suspensión (roja en el último partido): {', '.join(info['suspended'])}.")
        if info.get("confirmed"):
            nm = info.get("n_missing", 0)
            who = ", ".join(info.get("missing", [])[:5])
            if nm >= 4:
                add("bajas", "bloqueo", f"{nm} titulares habituales fuera de la alineación: {who}.")
            elif nm == 3 or info.get("gk_missing"):
                add("bajas", "advertencia", f"Ausencias importantes en la alineación: {who}.")
    mv = ctx.get("odds_move")
    if mv and mv["max_abs"] > S["max_odds_move"]:
        sev = "bloqueo" if mv["max_abs"] > 2 * S["max_odds_move"] else "advertencia"
        add("cuotas", sev, f"Movimiento brusco de cuotas: la probabilidad implícita cambió {mv['max_abs']:.0%}.")
    if fx.get("status") in ("postponed",):
        add("estado", "bloqueo", "Partido aplazado o suspendido.")
    if sum(f["severity"] == "advertencia" for f in flags) >= 2 and not any(f["severity"] == "bloqueo" for f in flags):
        add("acumulacion", "bloqueo", "Acumulación de señales de riesgo: mejor no pronosticar.")
    return flags


def blocked(flags: list[dict]) -> bool:
    return any(f["severity"] == "bloqueo" for f in flags)

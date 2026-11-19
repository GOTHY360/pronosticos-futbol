"""Orquestación diaria del AI NBA Predictor.

    python -m nba.run_daily              # fase 1 (noche): pronóstico preliminar de mañana
    python -m nba.run_daily 2026-11-21   # fecha concreta
    python -m nba.run_daily --update     # fase 2: parte de lesiones, titulares y cuotas de última hora
    python -m nba.run_daily --review     # aplica los veredictos del analista (Claude)
    python -m nba.run_daily --settle     # liquida resultados
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

import numpy as np

from pronosticos.calidad.calibration import Calibrator
from pronosticos.calidad.combo import select_legs

from . import export
from .config import MODEL_VERSION, REVIEW_DIR, SELECTION as S, TZ
from .db import connect
from .engine import NBAEngine
from .ingest import latest_injuries, lineups, sync_calendar, sync_day, sync_details, sync_injuries, sync_news
from .models import settle_player, settle_team
from .quality import choose, evaluate, game_flags, reasons

MARKET_NAMES = {"ML": "Ganador", "SPREAD": "Hándicap", "TOTAL": "Total de puntos", "TEAM_TOTAL": "Puntos del equipo",
                "P_PTS": "Puntos del jugador", "P_3PM": "Triples del jugador", "P_REB": "Rebotes del jugador",
                "P_AST": "Asistencias del jugador", "P_PRA": "Puntos + rebotes + asistencias", "P_PR": "Puntos + rebotes",
                "P_PA": "Puntos + asistencias", "P_RA": "Rebotes + asistencias", "P_STOCKS": "Robos + tapones",
                "P_DD": "Doble-doble"}


def log(s):
    print(s, flush=True)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def refresh(con):
    today = datetime.now(TZ).date()
    months = sorted({(today - timedelta(days=31)).strftime("%Y%m"), today.strftime("%Y%m"),
                     (today + timedelta(days=3)).strftime("%Y%m")})
    log("Actualizando calendario, resultados y cuotas…")
    sync_calendar(con, months=months, log=log)
    sync_details(con, log=log)
    sync_injuries(con, log=log)
    sync_news(con, log=log)


def latest_odds(con, event_id):
    r = con.execute("SELECT * FROM odds WHERE event_id=? ORDER BY taken_at DESC LIMIT 1", (event_id,)).fetchone()
    return dict(r) if r else None


def odds_move(con, event_id):
    rows = con.execute("""SELECT home_dec, away_dec FROM odds WHERE event_id=? AND home_dec IS NOT NULL AND away_dec IS NOT NULL
                          ORDER BY taken_at""", (event_id,)).fetchall()
    if len(rows) < 2:
        return None
    f = lambda r: (1 / r[0]) / (1 / r[0] + 1 / r[1])
    return abs(f(rows[-1]) - f(rows[0]))


# ---------------------------------------------------------------------------
# Validación por mercado en la zona de uso (igual que fútbol)
# ---------------------------------------------------------------------------
def market_check(con, zone: str, split: str = "2026-03-01") -> dict:
    cal = Calibrator(Calibrator.from_db_range(con, None, split))
    acc = defaultdict(lambda: [0, 0, 0.0])
    for r in con.execute("SELECT market, prob, outcome FROM backtest WHERE date_utc >= ?", (split,)):
        q, _ = cal.calibrate(r["market"], r["prob"])
        ok = (S["odds_min"] <= (1 / q) * (1 + S["min_edge"]) <= S["odds_max"]) if zone == "picks" \
            else (S["combo_min_prob"] <= q <= 1 / S["min_fair_odds"])
        if ok:
            a = acc[r["market"]]
            a[0] += 1
            a[1] += r["outcome"]
            a[2] += q
    return {m: {"n": n, "hit": w / n, "mean_prob": p / n, "disabled": n >= 300 and w / n < p / n - 0.03}
            for m, (n, w, p) in acc.items()}


class Analyzer:
    def __init__(self, con):
        self.con = con
        self.eng = NBAEngine(con, datetime.now(timezone.utc))
        self.cal = Calibrator.from_db(con)
        fam = {r[0]: r[1] for r in con.execute("SELECT market, COUNT(*) FROM backtest GROUP BY market")}
        self.mk = {z: market_check(con, z) for z in ("picks", "combo")}
        self.disabled = {z: [m for m, v in self.mk[z].items() if v["disabled"]] for z in self.mk}
        self.ok = {z: {m: n >= S["min_market_backtest_n"] and m not in self.disabled[z] for m, n in fam.items()} for z in self.mk}
        if self.disabled["picks"]:
            log("Mercados desactivados (pronósticos): " + ", ".join(MARKET_NAMES.get(m, m) for m in self.disabled["picks"]))
        if self.disabled["combo"]:
            log("Mercados desactivados (súper combinada): " + ", ".join(MARKET_NAMES.get(m, m) for m in self.disabled["combo"]))
        self.injuries = latest_injuries(con)

    def analyze(self, g, lineup=None):
        res = self.eng.analyze(g, self.injuries, lineups=lineup)
        if not res:
            return None, [], []
        od = latest_odds(self.con, g["event_id"])
        res["odds"] = od
        res["flags"] = game_flags(res, odds_move(self.con, g["event_id"]))
        picks = evaluate(res, self.cal, self.ok["picks"], od, res["flags"], "picks")
        combo = evaluate(res, self.cal, self.ok["combo"], od, res["flags"], "combo")
        for c in picks + combo:
            c["res"] = res
        return res, picks, combo

    def store_predictions(self, run_id, phase, res):
        rows = []
        for s in res["selections"] + [m for p in res["players"].values() for m in p["markets"]]:
            p, _ = self.cal.calibrate(s["market"], s["prob_raw"])
            sh = p - s["prob_raw"]
            rows.append((run_id, phase, res["game"]["event_id"], s["market"], s["selection"], s["line"], s.get("player_id") or "",
                         s["model"], s["prob_raw"], p, s.get("prob_low", s["prob_raw"]) + sh, s.get("prob_high", s["prob_raw"]) + sh))
        self.con.executemany("""INSERT OR REPLACE INTO predictions(run_id, phase, event_id, market, selection, line, player_id,
                                model, prob_raw, prob, prob_low, prob_high) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)


def _insert_pick(con, run_id, target, c, phase="preliminar"):
    g = c["res"]["game"]
    neigh = []
    if c["player_id"]:
        pl = c["player_ctx"]
        for s in pl["markets"]:
            if s["market"] == c["market"] and s["selection"] == c["selection"]:
                neigh.append({"line": s["line"], "prob": round(s["prob_raw"], 3)})
    con.execute("""INSERT OR IGNORE INTO picks(run_id, target_date, event_id, match_name, kickoff_utc, market, selection, line,
                   player_id, player, label, model, phase, prob, prob_low, prob_high, prob_initial, market_prob, odds_dec,
                   odds_initial, edge, min_odds, est_book_odds, neighbors, risk, quality_score, quality_checks, reasons, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, target, g["event_id"], f"{g['away']} @ {g['home']}", g["date_utc"], c["market"], c["selection"],
                 c["line"], c["player_id"] or "", c.get("player"), c["label"], c["model"], phase, c["prob"], c["prob_low"],
                 c["prob_high"], c["prob"], c["market_prob"], c["odds_dec"], c["odds_dec"], c["edge"], c["min_odds"],
                 c["est_book_odds"], json.dumps(neigh), c["risk"], c["quality_score"],
                 json.dumps(c["checks"], ensure_ascii=False), json.dumps(reasons(c["res"], c), ensure_ascii=False), _now()))


def build_combo(con, target, pool):
    cands = [(c["event_id"], c["player_id"] or "team", 0.6 * c["prob"] + 0.4 * c["prob_low"], c) for c in pool]
    legs = select_legs(cands, 10)
    if len(legs) < 4:
        con.execute("DELETE FROM combos WHERE target_date=? AND status='pending'", (target,))
        return None
    prob = float(np.prod([c["prob"] for c in legs]))
    est = float(np.prod([c["odds_dec"] or (1 / c["prob"]) / (1 + S["typical_margin"]) for c in legs]))
    data = [{"event_id": c["event_id"], "match": f"{c['res']['game']['away']} @ {c['res']['game']['home']}",
             "market": c["market"], "selection": c["selection"], "line": c["line"], "player_id": c["player_id"] or "",
             "label": c["label"], "prob": c["prob"], "prob_low": c["prob_low"], "prob_high": c["prob_high"],
             "odds_dec": c["odds_dec"], "status": "pending"} for c in legs]
    con.execute("""INSERT OR REPLACE INTO combos(target_date, created_at, legs, n_legs, prob, prob_low, prob_high, fair_odds, est_odds)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (target, _now(), json.dumps(data, ensure_ascii=False), len(legs), prob,
                 float(np.prod([c["prob_low"] for c in legs])), float(np.prod([c["prob_high"] for c in legs])), 1 / prob, est))
    con.commit()
    return {"n_legs": len(legs), "prob": prob}


def nightly(con, target: date) -> dict:
    run_id = "N" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    start = datetime.combine(target, time(0, 0), TZ)
    games = [dict(r) for r in con.execute("SELECT * FROM games WHERE date_utc >= ? AND date_utc < ? AND status='scheduled' ORDER BY date_utc",
                                          (_iso(start), _iso(start + timedelta(days=1))))]
    log(f"Partidos NBA para {target}: {len(games)}")
    for d in {target.strftime("%Y%m%d"), (target + timedelta(days=1)).strftime("%Y%m%d")}:
        try:
            sync_day(con, d)
        except RuntimeError:
            pass
    an = Analyzer(con)
    cands, pool, analyzed = [], [], []
    for g in games:
        g = dict(con.execute("SELECT * FROM games WHERE event_id=?", (g["event_id"],)).fetchone())
        res, pk, cb = an.analyze(g)
        if not res:
            analyzed.append({"event_id": g["event_id"], "home": g["home"], "away": g["away"], "kickoff_utc": g["date_utc"],
                             "league_name": "NBA", "status": "descartado", "notes": ["Sin historial suficiente."]})
            continue
        an.store_predictions(run_id, "preliminar", res)
        cands += pk
        pool += cb
        tp = res["team"]
        blocked = any(f["severity"] == "bloqueo" for f in res["flags"])
        analyzed.append({"event_id": g["event_id"], "home": g["home"], "away": g["away"], "kickoff_utc": g["date_utc"],
                         "league_name": "NBA", "status": "descartado" if blocked else ("con candidatos" if pk else "sin oportunidades"),
                         "notes": [f["message"] for f in res["flags"]], "xg": [round(tp["mu_home"], 1), round(tp["mu_away"], 1)],
                         "summary": f"Ganador {g['home']} {next(s['prob_raw'] for s in res['selections'] if s['market']=='ML' and s['selection']=='H'):.0%} · "
                                    f"total esperado {tp['total']:.1f} · margen {tp['margin']:+.1f}"
                                    + (f" · línea del mercado {res['odds']['spread_home']:+.1f} / {res['odds']['total_line']}" if res.get("odds") and res["odds"].get("total_line") else ""),
                         "n_candidates": len(pk)})
    picks = choose(cands)
    t = target.isoformat()
    for c in picks:
        _insert_pick(con, run_id, t, c)
    mega = build_combo(con, t, pool)
    con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)", (run_id, "preliminar", _now(), _now(),
                json.dumps({"target": t, "games": len(games), "candidates": len(cands), "picks": len(picks)})))
    con.commit()
    write_queue(con, t)
    log(f"Candidatos: {len(cands)} · Pronósticos publicados: {len(picks)}"
        + (f" · Súper combinada: {mega['n_legs']} selecciones ({mega['prob']:.0%})" if mega else ""))
    return {"run_id": run_id, "target": t, "analyzed": analyzed, "n_candidates": len(cands),
            "disabled_markets": [MARKET_NAMES.get(m, m) for m in an.disabled["picks"]]}


# ---------------------------------------------------------------------------
# Fase 2: lesiones, titulares y cuotas de última hora
# ---------------------------------------------------------------------------
def alert(con, event_id, pick_id, kind, sev, msg, action):
    con.execute("INSERT INTO alerts(created_at, event_id, pick_id, kind, severity, message, action) VALUES (?,?,?,?,?,?,?)",
                (_now(), event_id, pick_id, kind, sev, msg, action))
    log(f"  [{sev}] {msg} → {action}")


def update(con, hours_ahead=3.0):
    now = datetime.now(timezone.utc)
    pend = [dict(r) for r in con.execute("SELECT * FROM picks WHERE status='pending'")]
    if not pend:
        log("Sin pronósticos NBA pendientes.")
        return
    sync_injuries(con, log=log)
    for d in {datetime.now(TZ).strftime("%Y%m%d"), (datetime.now(TZ) + timedelta(days=1)).strftime("%Y%m%d")}:
        try:
            sync_day(con, d)
        except RuntimeError:
            pass
    inj = latest_injuries(con)
    an = None
    for p in pend:
        g = dict(con.execute("SELECT * FROM games WHERE event_id=?", (p["event_id"],)).fetchone())
        if g["status"] == "postponed":
            con.execute("UPDATE picks SET status='void', result_detail='Partido aplazado', updated_at=? WHERE pick_id=?", (_now(), p["pick_id"]))
            continue
        if p["player_id"]:
            st = (inj.get(p["player_id"]) or inj.get("name:" + (p["player"] or "").lower()) or {}).get("status")
            if st:
                con.execute("UPDATE picks SET status='withdrawn', result_detail=?, updated_at=? WHERE pick_id=?",
                            (f"Retirado: {p['player']} figura como '{st}' en el parte de lesiones.", _now(), p["pick_id"]))
                alert(con, p["event_id"], p["pick_id"], "lesion", "alta", f"{p['player']} aparece como {st}.", "retirado")
                continue
        mv = odds_move(con, p["event_id"])
        if mv and mv > 0.08 and not con.execute("SELECT 1 FROM alerts WHERE pick_id=? AND kind='cuotas'", (p["pick_id"],)).fetchone():
            alert(con, p["event_id"], p["pick_id"], "cuotas", "media", f"{p['match_name']}: cuotas movidas {mv:.0%}.", "reevaluado")
        ko = datetime.fromisoformat(p["kickoff_utc"].replace("Z", "+00:00"))
        if not (now <= ko <= now + timedelta(hours=hours_ahead)) or p["phase"] == "actualizado":
            continue
        if p["review_status"] == "revisar" and ko - now <= timedelta(minutes=45):
            con.execute("UPDATE picks SET status='withdrawn', result_detail=?, updated_at=? WHERE pick_id=?",
                        ("Retirado: la revisión del analista quedó pendiente.", _now(), p["pick_id"]))
            continue
        lu = lineups(p["event_id"])
        if len(lu) < 2:
            log(f"  {p['match_name']}: titulares aún no publicados")
            continue
        if p["player_id"] and p["player_id"] not in lu.get(g["home_id"], []) + lu.get(g["away_id"], []):
            prof_starter = con.execute("""SELECT AVG(starter) FROM (SELECT starter FROM player_games WHERE player_id=?
                                          ORDER BY rowid DESC LIMIT 10)""", (p["player_id"],)).fetchone()[0] or 0
            if prof_starter >= 0.5:
                con.execute("UPDATE picks SET status='withdrawn', lineup_status='cambios', result_detail=?, updated_at=? WHERE pick_id=?",
                            (f"Retirado: {p['player']} no figura entre los titulares confirmados.", _now(), p["pick_id"]))
                alert(con, p["event_id"], p["pick_id"], "alineacion", "alta", f"{p['player']} no es titular hoy.", "retirado")
                continue
        an = an or Analyzer(con)
        res, pk, _ = an.analyze(g, lu)
        same = next((c for c in pk if c["market"] == p["market"] and c["selection"] == p["selection"]
                     and abs(c["line"] - p["line"]) < 1e-9 and (c["player_id"] or "") == p["player_id"]), None)
        if same:
            con.execute("""UPDATE picks SET phase='actualizado', lineup_status='confirmada', prob=?, prob_low=?, prob_high=?,
                           min_odds=?, quality_score=?, updated_at=? WHERE pick_id=?""",
                        (same["prob"], same["prob_low"], same["prob_high"], same["min_odds"], same["quality_score"], _now(), p["pick_id"]))
            log(f"  {p['label']}: titulares confirmados; prob. {p['prob']:.0%} → {same['prob']:.0%}")
        else:
            con.execute("""UPDATE picks SET status='withdrawn', phase='actualizado', lineup_status='cambios', result_detail=?,
                           updated_at=? WHERE pick_id=?""",
                        ("Retirado tras confirmar titulares: ya no supera el filtro de calidad.", _now(), p["pick_id"]))
            alert(con, p["event_id"], p["pick_id"], "alineacion", "alta", f"{p['label']}: ya no supera el filtro.", "retirado")
    con.commit()


# ---------------------------------------------------------------------------
# Liquidación (prórroga incluida: el box score final ya la contiene)
# ---------------------------------------------------------------------------
def settle_all(con) -> int:
    n = 0
    for p in [dict(r) for r in con.execute("SELECT p.*, g.status gstatus FROM picks p JOIN games g USING(event_id) WHERE p.status='pending'")]:
        g = dict(con.execute("SELECT * FROM games WHERE event_id=?", (p["event_id"],)).fetchone())
        if g["status"] != "final" or not g["has_details"]:
            continue
        if p["player_id"]:
            row = con.execute("SELECT * FROM player_games WHERE event_id=? AND player_id=?", (p["event_id"], p["player_id"])).fetchone()
            o = settle_player(p["market"], p["selection"], p["line"], dict(row) if row else None)
            detail = (f"{p['player']}: {row['pts']} pts, {row['fg3m']} triples, {row['reb']} reb, {row['ast']} ast en {row['minutes']:.0f} min"
                      if row and not row["dnp"] else f"{p['player']} no jugó: apuesta nula")
        else:
            o = settle_team(p["market"], p["selection"], p["line"], g)
            detail = f"{g['away']} {g['away_pts']} – {g['home_pts']} {g['home']}" + (" (prórroga)" if (g["periods"] or 4) > 4 else "")
        status = "void" if o is None else ("won" if o else "lost")
        con.execute("UPDATE picks SET status=?, result_detail=?, settled_at=? WHERE pick_id=?", (status, detail, _now(), p["pick_id"]))
        n += 1
    for c in [dict(r) for r in con.execute("SELECT * FROM combos WHERE status='pending'")]:
        legs = json.loads(c["legs"])
        for l in legs:
            if l["status"] != "pending":
                continue
            g = dict(con.execute("SELECT * FROM games WHERE event_id=?", (l["event_id"],)).fetchone())
            if g["status"] == "postponed":
                l["status"] = "void"
                continue
            if g["status"] != "final" or not g["has_details"]:
                continue
            if l["player_id"]:
                row = con.execute("SELECT * FROM player_games WHERE event_id=? AND player_id=?", (l["event_id"], l["player_id"])).fetchone()
                o = settle_player(l["market"], l["selection"], l["line"], dict(row) if row else None)
            else:
                o = settle_team(l["market"], l["selection"], l["line"], g)
            l["status"] = "void" if o is None else ("won" if o else "lost")
        act = [l for l in legs if l["status"] != "void"]
        st = "lost" if any(l["status"] == "lost" for l in act) else ("won" if act and all(l["status"] == "won" for l in act) else
                                                                    ("void" if not act else "pending"))
        con.execute("UPDATE combos SET legs=?, status=?, result_detail=?, settled_at=? WHERE combo_id=?",
                    (json.dumps(legs, ensure_ascii=False), st, f"{sum(l['status'] == 'won' for l in act)}/{len(act)} acertadas",
                     _now() if st != "pending" else None, c["combo_id"]))
    con.commit()
    return n


# ---------------------------------------------------------------------------
# Revisión del analista
# ---------------------------------------------------------------------------
INSTRUCTIONS = ("Eres el analista de control de calidad NBA. NO recalcules probabilidades. Para cada pronóstico revisa: parte de "
                "lesiones y noticias de última hora del jugador y su equipo, gestión de carga (noches consecutivas, descansos), "
                "rol y minutos, riesgo de paliza, motivación y contradicciones entre modelo, Elo y mercado. Ante la duda, 'revisar' "
                "o 'rechazar'. Responde SOLO con JSON: [{\"pick_id\": <int>, \"verdict\": \"aprobar|revisar|rechazar\", \"notes\": \"...\"}]")


def write_queue(con, target):
    picks = [dict(r) for r in con.execute("SELECT * FROM picks WHERE target_date=? AND status='pending'", (target,))]
    if not picks:
        return None
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    items = [{"pick_id": p["pick_id"], "partido": p["match_name"], "inicio_utc": p["kickoff_utc"], "pronostico": p["label"],
              "probabilidad": round(p["prob"], 3), "cuota_minima": round(p["min_odds"], 2), "jugador": p["player"],
              "comprobaciones": json.loads(p["quality_checks"]), "razones": json.loads(p["reasons"])} for p in picks]
    path = REVIEW_DIR / f"cola_{target}.json"
    path.write_text(json.dumps({"fecha": target, "instrucciones": INSTRUCTIONS, "pronosticos": items}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return path


def apply_review(con, target):
    path = REVIEW_DIR / f"veredicto_{target}.json"
    if not path.exists():
        return 0
    try:
        vs = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return 0
    n = 0
    for v in vs if isinstance(vs, list) else []:
        try:
            pid, verdict, notes = int(v["pick_id"]), str(v["verdict"]).lower(), str(v.get("notes", ""))[:500]
        except (KeyError, ValueError, TypeError):
            continue
        if verdict == "rechazar":
            con.execute("""UPDATE picks SET status='withdrawn', review_status='rechazado', review_notes=?, result_detail=?
                           WHERE pick_id=? AND status='pending'""", (notes, "Retirado por la revisión del analista: " + notes, pid))
        elif verdict in ("revisar", "aprobar"):
            con.execute("UPDATE picks SET review_status=?, review_notes=? WHERE pick_id=? AND status='pending'",
                        ("revisar" if verdict == "revisar" else "aprobado", notes, pid))
        n += 1
    con.commit()
    log(f"  veredictos NBA aplicados: {n}")
    return n


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    con = connect()
    if "--update" in argv:
        update(con)
        sync_details(con, log=log)
        log(f"Liquidados: {settle_all(con)}")
        export.export_all(con, None)
        return
    if "--review" in argv:
        for d in (datetime.now(TZ).date(), datetime.now(TZ).date() + timedelta(days=1)):
            apply_review(con, d.isoformat())
        export.export_all(con, None)
        return
    refresh(con)
    log(f"Liquidados: {settle_all(con)}")
    if "--settle" in argv:
        export.export_all(con, None)
        return
    args = [a for a in argv if not a.startswith("--")]
    target = date.fromisoformat(args[0]) if args else datetime.now(TZ).date() + timedelta(days=1)
    result = nightly(con, target)
    export.export_all(con, result)
    log("Listo.")


if __name__ == "__main__":
    main(sys.argv[1:])

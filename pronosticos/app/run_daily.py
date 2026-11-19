"""Orquestación diaria del AI Football Predictor.

    python -m pronosticos.run_daily              # FASE 1 (noche): pronóstico preliminar de mañana
    python -m pronosticos.run_daily 2026-11-21   # fase 1 para una fecha concreta
    python -m pronosticos.run_daily --update     # FASE 2 (cada hora): alineaciones, cuotas y noticias de última hora
    python -m pronosticos.run_daily --review     # aplica los veredictos del analista (Claude)
    python -m pronosticos.run_daily --settle     # liquida resultados y exporta
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import date, datetime, time, timedelta, timezone

from . import export
from ..calidad import combo, danger, review
from ..calidad.backtest import competition_check, market_check
from ..calidad.calibration import Calibrator
from ..calidad.quality import choose, evaluate_match, reasons
from ..config import COMPETITIONS, MODEL, MODEL_VERSION, TZ
from ..datos import espn
from ..datos.ingest import event_context, sync_calendar, sync_details, sync_news, upsert_events
from ..db import connect
from ..motor.availability import lineup_adjustments, preliminary_adjustments
from ..motor.engine import LeagueEngine, load_rows, pool_of
from ..motor.markets import settle


def log(s):
    print(s, flush=True)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Módulo 1: datos
# ---------------------------------------------------------------------------
def refresh_data(con, competitions=None):
    today = datetime.now(TZ).date()
    months = sorted({(today - timedelta(days=31)).strftime("%Y%m"), today.strftime("%Y%m"),
                     (today + timedelta(days=3)).strftime("%Y%m")})
    log("Actualizando calendario, resultados y cuotas…")
    sync_calendar(con, months=months, leagues=competitions, log=log)
    log("Descargando estadísticas de partidos recién terminados…")
    sync_details(con, log=log)


def refresh_today_odds(con, leagues):
    d = datetime.now(TZ)
    for lg in leagues:
        for day in {d.strftime("%Y%m%d"), (d + timedelta(days=1)).strftime("%Y%m%d")}:
            try:
                upsert_events(con, lg, espn.scoreboard(lg, day))
            except RuntimeError as e:
                log(f"  ! {lg}: {e}")


# ---------------------------------------------------------------------------
# Memoria: liquidación de pronósticos y de todas las predicciones
# ---------------------------------------------------------------------------
def _result_row(con, event_id, league):
    return next((x for x in load_rows(con, league) if x["event_id"] == event_id), None)


def settle_all(con) -> int:
    n = 0
    cache = {}
    events = con.execute("""SELECT DISTINCT p.event_id, m.league, m.status FROM picks p
                            JOIN matches m ON m.event_id=p.event_id WHERE p.status='pending'
                            UNION SELECT DISTINCT pr.event_id, m.league, m.status FROM predictions pr
                            JOIN matches m ON m.event_id=pr.event_id WHERE pr.outcome IS NULL""").fetchall()
    for e in events:
        if e["status"] == "postponed":
            con.execute("""UPDATE picks SET status='void', result_detail='Partido aplazado o cancelado', settled_at=?
                           WHERE event_id=? AND status='pending'""", (_now(), e["event_id"]))
            continue
        if e["status"] != "final":
            continue
        if e["league"] not in cache:
            cache[e["league"]] = {r["event_id"]: r for r in load_rows(con, e["league"])}
        res = cache[e["league"]].get(e["event_id"])
        if not res:
            continue
        detail = f"{res['home_name']} {res['home_goals']}-{res['away_goals']} {res['away_name']}"
        for p in con.execute("SELECT pick_id, market, selection, line FROM picks WHERE event_id=? AND status='pending'",
                             (e["event_id"],)).fetchall():
            out = settle(p["market"], p["selection"], p["line"], res)
            if out is not None:
                con.execute("UPDATE picks SET status=?, result_detail=?, settled_at=? WHERE pick_id=?",
                            ("won" if out else "lost", detail, _now(), p["pick_id"]))
                n += 1
        for pr in con.execute("""SELECT DISTINCT market, selection, line FROM predictions
                                 WHERE event_id=? AND outcome IS NULL""", (e["event_id"],)).fetchall():
            out = settle(pr["market"], pr["selection"], pr["line"], res)
            if out is not None:
                con.execute("""UPDATE predictions SET outcome=? WHERE event_id=? AND market=? AND selection=?
                               AND line=?""", (out, e["event_id"], pr["market"], pr["selection"], pr["line"]))
    con.commit()

    def result_row(event_id, league):
        if league not in cache:
            cache[league] = {r["event_id"]: r for r in load_rows(con, league)}
        return cache[league].get(event_id)
    combo.settle(con, settle, result_row)
    return n


# ---------------------------------------------------------------------------
# Módulos 2 y 3: análisis de un partido
# ---------------------------------------------------------------------------
def latest_odds(con, event_id):
    r = con.execute("SELECT * FROM odds WHERE event_id=? AND kind='pre' ORDER BY taken_at DESC LIMIT 1",
                    (event_id,)).fetchone()
    return dict(r) if r else None


class Analyzer:
    def __init__(self, con):
        self.con = con
        self.now = datetime.now(timezone.utc)
        self.engines = {}
        self.calibrator = Calibrator.from_db(con)
        family_n = {r[0]: r[1] for r in con.execute("SELECT market, COUNT(*) FROM backtest GROUP BY market")}
        from ..config import SELECTION as S
        # Regla 6 por zona: cada mercado se valida fuera de muestra en la zona de probabilidad donde se usa
        self.mk_picks = market_check(con, zone="picks")
        self.mk_combo = market_check(con, zone="combo")
        self.disabled = [m for m, v in self.mk_picks.items() if v["disabled"]]
        self.disabled_combo = [m for m, v in self.mk_combo.items() if v["disabled"]]
        self.market_ok = {m: (n >= S["min_market_backtest_n"] and m not in self.disabled) for m, n in family_n.items()}
        self.market_ok_combo = {m: (n >= S["min_market_backtest_n"] and m not in self.disabled_combo)
                                for m, n in family_n.items()}
        if self.disabled:
            log(f"Mercados desactivados (pronósticos individuales): {', '.join(self.disabled)}")
        if self.disabled_combo:
            log(f"Mercados desactivados (súper combinada): {', '.join(self.disabled_combo)}")
        self.comp_check = competition_check(con)
        self.disabled_comps = [lg for lg, v in self.comp_check.items() if v["disabled"]]
        if self.disabled_comps:
            log("Competiciones sin publicación por bajo rendimiento fuera de muestra: "
                + ", ".join(COMPETITIONS.get(c, {}).get("name", c) for c in self.disabled_comps))

    def engine(self, league):
        pool = pool_of(league)
        if pool not in self.engines:
            self.engines[pool] = LeagueEngine(pool, load_rows(self.con, pool), self.now)
        return self.engines[pool]

    def analyze(self, fx: dict, phase: str, lineups: dict | None = None):
        eng = self.engine(fx["league"])
        h, a = fx["home_id"], fx["away_id"]
        if not eng.knows(h, a):
            return None, [], ["Equipo sin historial en la base de datos (recién ascendido o datos faltantes)."]
        if phase == "actualizado" and lineups:
            adj, avail = lineup_adjustments(self.con, h, a, fx["date_utc"], lineups)
        else:
            adj, avail = preliminary_adjustments(self.con, h, a, fx["date_utc"])
        neutral = bool(fx.get("neutral"))
        pred = eng.predict(h, a, fx["home_name"], fx["away_name"], neutral, adj)
        ctx = {**fx, **pred, "phase": phase, "neutral": neutral, "adj": adj, "availability": avail,
               "odds": latest_odds(self.con, fx["event_id"]), "odds_move": danger.odds_movement(self.con, fx["event_id"]),
               "form_home": eng.form(h), "form_away": eng.form(a),
               "home_at_home": eng.form(h, 10, "home"), "away_away": eng.form(a, 10, "away"),
               "h2h": eng.h2h(h, a), "res_home": eng.residuals(h), "res_away": eng.residuals(a),
               "mc_draws": MODEL["mc_draws"]}
        ctx["flags"] = danger.detect(self.con, fx, ctx)
        if fx["league"] in self.disabled_comps:
            v = self.comp_check[fx["league"]]
            ctx["flags"].append({"code": "competicion", "severity": "bloqueo",
                                 "message": f"Competición con rendimiento inferior al anunciado en la validación "
                                            f"({v['hit']:.0%} real vs. {v['mean_prob']:.0%} anunciado, n={v['n']})."})
        cands, notes = evaluate_match(ctx, self.calibrator, self.market_ok, "picks")
        combo_cands, _ = evaluate_match(ctx, self.calibrator, self.market_ok_combo, "combo")
        for c in cands + combo_cands:
            c.update({"event_id": fx["event_id"], "league": fx["league"], "ctx": ctx})
        ctx["combo_cands"] = combo_cands
        return ctx, cands, notes

    def store_predictions(self, run_id, phase, ctx):
        for s in ctx["selections"]:
            p, _ = self.calibrator.calibrate(s["market"], s["prob_raw"])
            shift = p - s["prob_raw"]
            self.con.execute("""INSERT OR REPLACE INTO predictions(run_id, phase, event_id, market, selection, line,
                                model, model_version, prob_raw, prob, prob_low, prob_high) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (run_id, phase, ctx["event_id"], s["market"], s["selection"], s["line"], s["model"],
                              MODEL_VERSION, s["prob_raw"], p,
                              (s.get("prob_low") or s["prob_raw"]) + shift, (s.get("prob_high") or s["prob_raw"]) + shift))


def _public_fx(fx, ctx, status, notes, n_cands, cal):
    d = {"event_id": fx["event_id"], "league": fx["league"], "league_name": COMPETITIONS.get(fx["league"], {}).get("name"),
         "home": fx["home_name"], "away": fx["away_name"], "kickoff_utc": fx["date_utc"], "status": status,
         "notes": notes, "n_candidates": n_cands}
    if ctx:
        d["xg"] = [round(v, 2) for v in ctx["expected"]["goals"]]
        d["neutral"] = ctx["neutral"]
        d["flags"] = ctx["flags"]
        d["scenarios"] = ctx.get("scenarios")
        top = {}
        for s in ctx["selections"]:
            if s["market"] in ("1X2", "BTTS") or (s["market"] == "OU_GOALS" and s["line"] == 2.5):
                top[f'{s["market"]}:{s["selection"]}'] = round(cal.calibrate(s["market"], s["prob_raw"])[0], 3)
        d["probs"] = top
    return d


def _insert_pick(con, run_id, target, c, ctx, phase):
    con.execute("""INSERT OR IGNORE INTO picks(run_id, target_date, event_id, league, match_name, kickoff_utc,
                   market, selection, line, label, model, model_version, phase, prob, prob_low, prob_high, prob_initial,
                   market_prob, odds_dec, odds_initial, overround, edge, min_odds, est_book_odds, risk, quality_score,
                   quality_checks, reasons, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, target, c["event_id"], c["league"], f'{ctx["home_name"]} vs {ctx["away_name"]}',
                 ctx["date_utc"], c["market"], c["selection"], c["line"], c["label"], c["model"], MODEL_VERSION,
                 phase, c["prob"], c["prob_low"], c["prob_high"], c["prob"], c["market_prob"], c["odds_dec"],
                 c["odds_dec"], c["overround"], c["edge"], c["min_odds"], c["est_book_odds"], c["risk"], c["quality_score"],
                 json.dumps(c["checks"], ensure_ascii=False), json.dumps(reasons(ctx, c), ensure_ascii=False), _now()))


# ---------------------------------------------------------------------------
# FASE 1: pronóstico preliminar (la noche anterior)
# ---------------------------------------------------------------------------
def nightly(con, target: date) -> dict:
    run_id = "N" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    start = datetime.combine(target, time(0, 0), TZ)
    fixtures = [dict(r) for r in con.execute(
        "SELECT * FROM matches WHERE date_utc >= ? AND date_utc < ? AND status='scheduled' ORDER BY date_utc",
        (_iso(start), _iso(start + timedelta(days=1)))).fetchall()]
    log(f"Partidos programados para {target}: {len(fixtures)}")
    sync_news(con, sorted({f["league"] for f in fixtures}), log=log)
    an = Analyzer(con)
    candidates, combo_pool, analyzed, contexts = [], [], [], {}
    for fx in fixtures:
        ctx, cands, notes = an.analyze(fx, "preliminar")
        if ctx:
            an.store_predictions(run_id, "preliminar", ctx)
            contexts[fx["event_id"]] = ctx
            candidates += cands
            combo_pool += ctx["combo_cands"]
        status = "descartado" if (not ctx or danger.blocked(ctx["flags"])) else \
            ("con candidatos" if cands else "sin oportunidades")
        analyzed.append(_public_fx(fx, ctx, status, notes, len(cands), an.calibrator))
    picks = choose(candidates)
    t = target.isoformat()
    for c in picks:
        _insert_pick(con, run_id, t, c, c["ctx"], "preliminar")
    mega = combo.build(combo_pool)
    if mega:
        combo.save(con, t, mega)
        log(f"Súper combinada: {mega['n_legs']} selecciones · probabilidad {mega['prob']:.1%} · "
            f"cuota justa {mega['fair_odds']:.2f}")
    else:
        log(f"Súper combinada: no hay al menos {combo.MIN_LEGS} partidos con selecciones que superen el filtro.")
    con.execute("INSERT OR REPLACE INTO runs(run_id, kind, started_at, finished_at, notes) VALUES (?,?,?,?,?)",
                (run_id, "preliminar", an.now.isoformat(timespec="seconds"), _now(),
                 json.dumps({"target": t, "fixtures": len(fixtures), "candidates": len(candidates), "picks": len(picks),
                             "disabled_markets": an.disabled})))
    con.commit()
    q = review.write_queue(con, t, {k: {"elo": v["elo"], "flags": v["flags"]} for k, v in contexts.items()})
    log(f"Candidatos que superan el filtro de calidad: {len(candidates)} · Pronósticos publicados: {len(picks)}")
    if q:
        log(f"Cola de revisión para el analista: {q}")
    return {"run_id": run_id, "target": t, "analyzed": analyzed, "n_candidates": len(candidates),
            "disabled_markets": an.disabled}


# ---------------------------------------------------------------------------
# FASE 2: actualización con alineaciones y cambios de última hora
# ---------------------------------------------------------------------------
def alert(con, event_id, pick_id, kind, severity, message, action):
    con.execute("INSERT INTO alerts(created_at, event_id, pick_id, kind, severity, message, action) VALUES (?,?,?,?,?,?,?)",
                (_now(), event_id, pick_id, kind, severity, message, action))
    log(f"  [{severity}] {message} → {action}")


def update_upcoming(con, hours_ahead: float = 3.0):
    now = datetime.now(timezone.utc)
    pend = con.execute("""SELECT p.*, m.status mstatus FROM picks p JOIN matches m ON m.event_id=p.event_id
                          WHERE p.status='pending'""").fetchall()
    if not pend:
        log("Sin pronósticos pendientes.")
        return
    refresh_today_odds(con, sorted({p["league"] for p in pend}))
    an = None
    for p in pend:
        ko = datetime.fromisoformat(p["kickoff_utc"].replace("Z", "+00:00"))
        fx = dict(con.execute("SELECT * FROM matches WHERE event_id=?", (p["event_id"],)).fetchone())
        if fx["status"] == "postponed":
            con.execute("UPDATE picks SET status='void', result_detail='Partido aplazado', updated_at=? WHERE pick_id=?",
                        (_now(), p["pick_id"]))
            alert(con, p["event_id"], p["pick_id"], "estado", "alta", f"{p['match_name']}: partido aplazado.", "anulado")
            continue
        mv = danger.odds_movement(con, p["event_id"])
        if mv and mv["max_abs"] > 0.08:
            seen = con.execute("SELECT 1 FROM alerts WHERE pick_id=? AND kind='cuotas'", (p["pick_id"],)).fetchone()
            if not seen:
                alert(con, p["event_id"], p["pick_id"], "cuotas", "media",
                      f"{p['match_name']}: las cuotas se movieron {mv['max_abs']:.0%} desde el análisis.", "reevaluado")
        if not (now <= ko <= now + timedelta(hours=hours_ahead)):
            continue
        if p["review_status"] == "revisar" and ko - now <= timedelta(minutes=45):
            con.execute("""UPDATE picks SET status='withdrawn', result_detail=?, updated_at=? WHERE pick_id=?""",
                        ("Retirado: la revisión del analista quedó pendiente antes del inicio. " + (p["review_notes"] or ""),
                         _now(), p["pick_id"]))
            alert(con, p["event_id"], p["pick_id"], "revision", "media", f"{p['match_name']}: revisión sin resolver.", "retirado")
            continue
        info = event_context(con, p["league"], p["event_id"])
        if len(info["lineups"]) < 2:
            log(f"  {p['match_name']}: alineaciones aún no publicadas")
            continue
        if p["phase"] == "actualizado":
            continue
        an = an or Analyzer(con)
        ctx, cands, notes = an.analyze(fx, "actualizado", info["lineups"])
        if not ctx:
            continue
        an.store_predictions("U" + now.strftime("%Y%m%dT%H%M%S"), "actualizado", ctx)
        same = next((c for c in cands if c["market"] == p["market"] and c["selection"] == p["selection"]
                     and abs(c["line"] - p["line"]) < 1e-9), None)
        missing = "; ".join(f"{v.get('n_missing', 0)} bajas" for v in ctx["availability"].values() if v.get("confirmed"))
        if same:
            con.execute("""UPDATE picks SET phase='actualizado', lineup_status='confirmada', prob=?, prob_low=?, prob_high=?,
                           market_prob=?, odds_dec=COALESCE(?, odds_dec), edge=?, quality_score=?, quality_checks=?,
                           reasons=?, updated_at=? WHERE pick_id=?""",
                        (same["prob"], same["prob_low"], same["prob_high"], same["market_prob"], same["odds_dec"],
                         same["edge"], same["quality_score"],
                         json.dumps([{**ch, **({"status": "ok", "detail": "confirmadas"} if ch["name"].startswith("Alineaciones") else {})}
                                     for ch in same["checks"]], ensure_ascii=False),
                         json.dumps(reasons(ctx, same), ensure_ascii=False), _now(), p["pick_id"]))
            log(f"  {p['match_name']}: alineación confirmada ({missing}); prob. {p['prob']:.0%} → {same['prob']:.0%}")
        else:
            why = "; ".join(notes) or "la probabilidad actualizada ya no supera el filtro de calidad"
            con.execute("""UPDATE picks SET status='withdrawn', phase='actualizado', lineup_status='cambios',
                           result_detail=?, updated_at=? WHERE pick_id=?""",
                        ("Retirado antes del partido tras confirmar alineaciones: " + why, _now(), p["pick_id"]))
            alert(con, p["event_id"], p["pick_id"], "alineacion", "alta", f"{p['match_name']}: {why}", "retirado")
    con.commit()


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    con = connect()
    if "--update" in argv or "--lineups" in argv:
        update_upcoming(con)
        log(f"Liquidados: {settle_all(con)}")
        export.export_all(con, None)
        return
    if "--review" in argv:
        for d in (datetime.now(TZ).date(), datetime.now(TZ).date() + timedelta(days=1)):
            review.apply_verdicts(con, d.isoformat(), log=log)
        export.export_all(con, None)
        return
    refresh_data(con)
    log(f"Pronósticos liquidados: {settle_all(con)}")
    if "--settle" in argv:
        export.export_all(con, None)
        return
    args = [a for a in argv if not a.startswith("--")]
    target = date.fromisoformat(args[0]) if args else (datetime.now(TZ).date() + timedelta(days=1))
    result = nightly(con, target)
    export.export_all(con, result)
    log("Listo.")


if __name__ == "__main__":
    main(sys.argv[1:])

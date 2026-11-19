"""Módulo 4 (aplicación): exporta los JSON que lee el tablero web."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from . import advisor
from ..calidad.backtest import simulate_combos, simulate_selection
from ..calidad.calibration import holdout, metrics, reliability
from ..config import COMPETITIONS, MODEL_PARAMS_PATH, MODEL_VERSION, SELECTION, SITE_DATA_DIR

MARKET_NAMES = {"1X2": "Ganador", "DC": "Doble oportunidad", "OU_GOALS": "Más/menos goles",
                "BTTS": "Ambos marcan", "TEAM_GOALS": "Goles por equipo", "OU_CORNERS": "Córners",
                "OU_CARDS": "Tarjetas", "TEAM_SHOTS": "Tiros del equipo", "TEAM_SOT": "Tiros a puerta",
                "GK_SAVES": "Atajadas del portero"}
MODEL_NAMES = {"goals_dixon_coles": "Goles · Poisson Dixon-Coles", "corners_negbin": "Córners · binomial negativa",
               "cards_negbin": "Tarjetas · binomial negativa", "shots_negbin": "Tiros · binomial negativa",
               "sot_negbin": "Tiros a puerta · binomial negativa", "saves_negbin": "Atajadas · binomial negativa"}


def _write(name, obj):
    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DATA_DIR / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _pick_public(r) -> dict:
    d = dict(r)
    d["reasons"] = json.loads(d.get("reasons") or "[]")
    d["quality_checks"] = json.loads(d.get("quality_checks") or "[]")
    d["league_name"] = COMPETITIONS.get(d["league"], {}).get("name", d["league"])
    d["market_name"] = MARKET_NAMES.get(d["market"], d["market"])
    d["model_name"] = MODEL_NAMES.get(d.get("model"), d.get("model"))
    return d


def _summary(rows):
    settled = [r for r in rows if r["status"] in ("won", "lost")]
    n = len(settled)
    won = sum(r["status"] == "won" for r in settled)
    with_odds = [r for r in settled if r.get("odds_initial")]
    profit = sum((r["odds_initial"] - 1) if r["status"] == "won" else -1 for r in with_odds)
    return {"n": n, "won": won, "lost": n - won, "hit_rate": won / n if n else None,
            "expected_hit_rate": sum(r["prob"] for r in settled) / n if n else None,
            "brier": sum((r["prob"] - (r["status"] == "won")) ** 2 for r in settled) / n if n else None,
            "n_with_odds": len(with_odds), "profit_units": profit if with_odds else None,
            "roi_flat": profit / len(with_odds) if with_odds else None,
            "pending": sum(r["status"] == "pending" for r in rows),
            "withdrawn": sum(r["status"] == "withdrawn" for r in rows),
            "void": sum(r["status"] == "void" for r in rows)}


def _model_live(con) -> list[dict]:
    """Rendimiento real de cada modelo sobre TODAS sus predicciones liquidadas (no sólo las publicadas)."""
    acc = defaultdict(lambda: [0, 0.0, 0.0, 0, 0.0])
    for r in con.execute("""SELECT model, phase, prob, outcome FROM predictions p
                            WHERE outcome IS NOT NULL AND run_id = (SELECT MAX(run_id) FROM predictions q
                              WHERE q.event_id=p.event_id AND q.market=p.market AND q.selection=p.selection
                              AND q.line=p.line)"""):
        a = acc[r["model"]]
        p = min(max(r["prob"], 1e-6), 1 - 1e-6)
        a[0] += 1
        a[1] += (p - r["outcome"]) ** 2
        a[2] += -(r["outcome"] * math.log(p) + (1 - r["outcome"]) * math.log(1 - p))
        a[3] += r["outcome"]
        a[4] += p
    return [{"model": m, "name": MODEL_NAMES.get(m, m), "n": a[0], "brier": a[1] / a[0], "logloss": a[2] / a[0],
             "freq": a[3] / a[0], "mean_prob": a[4] / a[0]} for m, a in sorted(acc.items()) if a[0]]


def export_all(con, result: dict | None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    picks = [_pick_public(r) for r in con.execute("SELECT * FROM picks ORDER BY kickoff_utc DESC, pick_id DESC")]
    alerts = [dict(r) for r in con.execute("SELECT * FROM alerts ORDER BY alert_id DESC LIMIT 100")]

    # --- informe del día ------------------------------------------------------
    latest_path = SITE_DATA_DIR / "latest.json"
    latest = None
    if result:
        latest = {"generated_at": now, "target_date": result["target"], "run_id": result["run_id"],
                  "analyzed": result["analyzed"], "n_candidates": result["n_candidates"],
                  "disabled_markets": [MARKET_NAMES.get(m, m) for m in result.get("disabled_markets", [])],
                  "rules": SELECTION,
                  "model_version": MODEL_VERSION}
    elif latest_path.exists():
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        latest["updated_at"] = now
    if latest:
        day = [p for p in picks if p["target_date"] == latest["target_date"]]
        latest["picks"] = [p for p in day if p["status"] != "withdrawn" and p["review_status"] != "revisar"]
        latest["in_review"] = [p for p in day if p["status"] == "pending" and p["review_status"] == "revisar"]
        latest["withdrawn"] = [p for p in day if p["status"] == "withdrawn"]
        latest["alerts"] = [a for a in alerts if any(a["pick_id"] == p["pick_id"] for p in day)]
        top = sorted([p for p in latest["picks"] if p["status"] == "pending"], key=lambda p: -p["prob"])[:2]
        latest["combo"] = None
        if len(top) == 2 and top[0]["prob"] * top[1]["prob"] >= 0.60:
            latest["combo"] = {"legs": [p["pick_id"] for p in top], "prob": top[0]["prob"] * top[1]["prob"],
                               "note": "Combinada opcional de los dos pronósticos más sólidos (partidos distintos; se "
                                       "asume independencia). Su probabilidad es menor que la de cada uno por separado."}
        row = con.execute("SELECT * FROM combos WHERE target_date=?", (latest["target_date"],)).fetchone()
        latest["mega"] = ({**dict(row), "legs": json.loads(row["legs"]),
                           "expected_value": row["prob"] * row["est_odds"] - 1} if row else None)
        _write("latest.json", latest)

    # --- historial y rendimiento ---------------------------------------------
    published = [p for p in picks if p["status"] != "withdrawn"]
    groups = {"by_league": "league_name", "by_market": "market_name", "by_model": "model_name",
              "by_risk": "risk", "by_phase": "phase"}
    out = {"generated_at": now, "overall": _summary(picks)}
    for key, field in groups.items():
        g = defaultdict(list)
        for p in published:
            g[p.get(field) or "—"].append(p)
        out[key] = {k: _summary(v) for k, v in sorted(g.items())}
    q = defaultdict(list)
    for p in published:
        s = p.get("quality_score") or 0
        q["90–100" if s >= 90 else ("80–89" if s >= 80 else "70–79")].append(p)
    out["by_quality"] = {k: _summary(v) for k, v in sorted(q.items(), reverse=True)}
    # evolución acumulada (acierto real vs. esperado, beneficio simulado)
    series, hits, exp, profit, n = [], 0, 0.0, 0.0, 0
    for p in sorted([p for p in picks if p["status"] in ("won", "lost")], key=lambda p: p["kickoff_utc"]):
        n += 1
        hits += p["status"] == "won"
        exp += p["prob"]
        if p.get("odds_initial"):
            profit += (p["odds_initial"] - 1) if p["status"] == "won" else -1
        series.append({"date": p["kickoff_utc"][:10], "n": n, "hit": hits / n, "expected": exp / n, "profit": profit})
    out["series"] = series
    out["prob_evolution"] = [{"pick_id": p["pick_id"], "match": p["match_name"], "label": p["label"],
                              "initial": p["prob_initial"], "final": p["prob"], "status": p["status"]}
                             for p in picks if p.get("phase") == "actualizado"][:100]
    cal = defaultdict(lambda: [0, 0.0, 0])
    for p in picks:
        if p["status"] in ("won", "lost"):
            b = min(int(p["prob"] * 20) / 20, 0.95)
            cal[b][0] += 1
            cal[b][1] += p["prob"]
            cal[b][2] += p["status"] == "won"
    out["calibration"] = [{"lo": b, "hi": b + 0.05, "n": k, "mean_p": sp / k, "freq": w / k}
                          for b, (k, sp, w) in sorted(cal.items())]
    out["models_live"] = _model_live(con)
    adv = advisor.evaluate(con, record=False)
    out["advisor"] = {k: adv[k] for k in ("level", "reached", "progress", "message")}
    combos = [{**dict(r), "legs": json.loads(r["legs"])} for r in con.execute("SELECT * FROM combos ORDER BY target_date DESC")]
    done = [c for c in combos if c["status"] in ("won", "lost")]
    out["combos"] = {"n": len(done), "won": sum(c["status"] == "won" for c in done),
                     "hit_rate": (sum(c["status"] == "won" for c in done) / len(done)) if done else None,
                     "expected": (sum(c["prob"] for c in done) / len(done)) if done else None,
                     "profit_est": sum((c["est_odds"] - 1) if c["status"] == "won" else -1 for c in done) if done else None,
                     "list": combos[:60]}
    out["alerts"] = alerts
    out["picks"] = picks[:500]
    _write("history.json", out)

    # --- validación del modelo (backtest) -------------------------------------
    mk = metrics(con, "market")
    if mk:
        try:
            params = json.loads(MODEL_PARAMS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            params = {}
        _write("model.json", {
            "generated_at": now, "model_version": MODEL_VERSION, "params": params,
            "by_market": [{**m, "name": MARKET_NAMES.get(m["group"], m["group"])} for m in mk],
            "by_league": [{**m, "name": COMPETITIONS.get(m["group"], {}).get("name", m["group"])}
                          for m in metrics(con, "league")],
            "reliability": {m: reliability(con, m) for m in MARKET_NAMES},
            "reliability_high": reliability(con, None, 0.6),
            "holdout": holdout(con, "2026-03-01"),
            "simulation": simulate_selection(con, "2026-03-01") or None,
            "combo_simulation": simulate_combos(con, "2026-03-01") or None,
            "n_matches": con.execute("SELECT COUNT(DISTINCT event_id) FROM backtest").fetchone()[0]})

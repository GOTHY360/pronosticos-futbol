"""Exporta los JSON del tablero NBA (mismo formato que el de fútbol para reutilizar la interfaz)."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from pronosticos.calidad.calibration import Calibrator, metrics, reliability

from .config import MODEL_VERSION, SELECTION, SITE_DATA_DIR

MARKET_NAMES = {"ML": "Ganador", "SPREAD": "Hándicap", "TOTAL": "Total de puntos", "TEAM_TOTAL": "Puntos del equipo",
                "P_PTS": "Puntos del jugador", "P_3PM": "Triples del jugador", "P_REB": "Rebotes del jugador",
                "P_AST": "Asistencias del jugador", "P_PRA": "Puntos + rebotes + asistencias", "P_PR": "Puntos + rebotes",
                "P_PA": "Puntos + asistencias", "P_RA": "Rebotes + asistencias", "P_STOCKS": "Robos + tapones",
                "P_DD": "Doble-doble"}


def _write(name, obj):
    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (SITE_DATA_DIR / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _pub(r) -> dict:
    d = dict(r)
    d["reasons"] = json.loads(d.get("reasons") or "[]")
    d["quality_checks"] = json.loads(d.get("quality_checks") or "[]")
    d["neighbors"] = json.loads(d.get("neighbors") or "[]")
    d["league_name"] = "NBA"
    d["market_name"] = MARKET_NAMES.get(d["market"], d["market"])
    d["model_name"] = d.get("model")
    return d


def _summary(rows):
    s = [r for r in rows if r["status"] in ("won", "lost")]
    n = len(s)
    won = sum(r["status"] == "won" for r in s)
    wo = [r for r in s if r.get("odds_initial") or r.get("min_odds")]
    prof = sum(((r.get("odds_initial") or r["min_odds"]) - 1) if r["status"] == "won" else -1 for r in wo)
    return {"n": n, "won": won, "lost": n - won, "hit_rate": won / n if n else None,
            "expected_hit_rate": sum(r["prob"] for r in s) / n if n else None,
            "brier": sum((r["prob"] - (r["status"] == "won")) ** 2 for r in s) / n if n else None,
            "n_with_odds": len(wo), "profit_units": prof if wo else None, "roi_flat": prof / len(wo) if wo else None,
            "pending": sum(r["status"] == "pending" for r in rows), "withdrawn": sum(r["status"] == "withdrawn" for r in rows),
            "void": sum(r["status"] == "void" for r in rows)}


def simulate_selection(con, split="2026-03-01"):
    """Acierto fuera de muestra de lo que el selector habría publicado (rango de cuotas elegido)."""
    cal = Calibrator(Calibrator.from_db_range(con, None, split))
    S = SELECTION
    acc, tot = defaultdict(lambda: [0, 0, 0.0]), [0, 0, 0.0, 0.0]
    for r in con.execute("SELECT market, prob, outcome FROM backtest WHERE date_utc >= ?", (split,)):
        q, b = cal.calibrate(r["market"], r["prob"])
        mo = (1 / q) * (1 + S["min_edge"])
        if not (S["odds_min"] <= mo <= S["odds_max"]) or not b or b["n"] < 30:
            continue
        a = acc[r["market"]]
        a[0] += 1
        a[1] += r["outcome"]
        a[2] += q
        tot[0] += 1
        tot[1] += r["outcome"]
        tot[2] += q
        tot[3] += (mo - 1) if r["outcome"] else -1
    if not tot[0]:
        return None
    return {"split": split, "n": tot[0], "days": None, "hit": tot[1] / tot[0], "mean_prob": tot[2] / tot[0],
            "roi_at_min_odds": tot[3] / tot[0], "brier": None,
            "by_market": {MARKET_NAMES.get(m, m): {"n": a[0], "hit": a[1] / a[0], "mean_prob": a[2] / a[0]} for m, a in acc.items()}}


def export_all(con, result: dict | None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    picks = [_pub(r) for r in con.execute("SELECT * FROM picks ORDER BY kickoff_utc DESC, pick_id DESC")]
    alerts = [dict(r) for r in con.execute("SELECT * FROM alerts ORDER BY alert_id DESC LIMIT 100")]
    path = SITE_DATA_DIR / "latest.json"
    latest = None
    if result:
        latest = {"generated_at": now, "target_date": result["target"], "run_id": result["run_id"], "analyzed": result["analyzed"],
                  "n_candidates": result["n_candidates"], "disabled_markets": result.get("disabled_markets", []),
                  "rules": SELECTION, "model_version": MODEL_VERSION, "sport": "nba"}
    elif path.exists():
        latest = json.loads(path.read_text(encoding="utf-8"))
        latest["updated_at"] = now
    if latest:
        day = [p for p in picks if p["target_date"] == latest["target_date"]]
        latest["picks"] = [p for p in day if p["status"] != "withdrawn" and p["review_status"] != "revisar"]
        latest["in_review"] = [p for p in day if p["status"] == "pending" and p["review_status"] == "revisar"]
        latest["withdrawn"] = [p for p in day if p["status"] == "withdrawn"]
        latest["alerts"] = [a for a in alerts if any(a["pick_id"] == p["pick_id"] for p in day)]
        latest["combo"] = None
        row = con.execute("SELECT * FROM combos WHERE target_date=?", (latest["target_date"],)).fetchone()
        latest["mega"] = ({**dict(row), "legs": json.loads(row["legs"]), "expected_value": row["prob"] * row["est_odds"] - 1}
                          if row else None)
        _write("latest.json", latest)
    pub = [p for p in picks if p["status"] != "withdrawn"]
    out = {"generated_at": now, "overall": _summary(picks)}
    for key, f in {"by_market": "market_name", "by_model": "model_name", "by_risk": "risk", "by_phase": "phase"}.items():
        g = defaultdict(list)
        for p in pub:
            g[p.get(f) or "—"].append(p)
        out[key] = {k: _summary(v) for k, v in sorted(g.items())}
    out["by_league"] = {}
    out["by_quality"] = {}
    series, hits, exp, prof, n = [], 0, 0.0, 0.0, 0
    for p in sorted([p for p in picks if p["status"] in ("won", "lost")], key=lambda p: p["kickoff_utc"]):
        n += 1
        hits += p["status"] == "won"
        exp += p["prob"]
        odds = p.get("odds_initial") or p.get("min_odds")
        prof += (odds - 1) if p["status"] == "won" else -1
        series.append({"date": p["kickoff_utc"][:10], "n": n, "hit": hits / n, "expected": exp / n, "profit": prof})
    out["series"] = series
    out["prob_evolution"] = [{"pick_id": p["pick_id"], "match": p["match_name"], "label": p["label"], "initial": p["prob_initial"],
                              "final": p["prob"], "status": p["status"]} for p in picks if p.get("phase") == "actualizado"][:100]
    cal = defaultdict(lambda: [0, 0.0, 0])
    for p in picks:
        if p["status"] in ("won", "lost"):
            b = min(int(p["prob"] * 20) / 20, 0.95)
            cal[b][0] += 1
            cal[b][1] += p["prob"]
            cal[b][2] += p["status"] == "won"
    out["calibration"] = [{"lo": b, "hi": b + 0.05, "n": k, "mean_p": sp / k, "freq": w / k} for b, (k, sp, w) in sorted(cal.items())]
    out["models_live"] = []
    combos = [{**dict(r), "legs": json.loads(r["legs"])} for r in con.execute("SELECT * FROM combos ORDER BY target_date DESC")]
    done = [c for c in combos if c["status"] in ("won", "lost")]
    out["combos"] = {"n": len(done), "won": sum(c["status"] == "won" for c in done),
                     "hit_rate": sum(c["status"] == "won" for c in done) / len(done) if done else None,
                     "expected": sum(c["prob"] for c in done) / len(done) if done else None,
                     "profit_est": sum((c["est_odds"] - 1) if c["status"] == "won" else -1 for c in done) if done else None,
                     "list": combos[:60]}
    out["alerts"] = alerts
    out["picks"] = picks[:500]
    _write("history.json", out)
    mk = metrics(con, "market")
    if mk:
        _write("model.json", {
            "generated_at": now, "model_version": MODEL_VERSION, "params": {},
            "by_market": [{**m, "name": MARKET_NAMES.get(m["group"], m["group"])} for m in mk],
            "by_league": [],
            "reliability_high": reliability(con, None, 0.5),
            "holdout": None, "simulation": simulate_selection(con), "combo_simulation": None,
            "n_matches": con.execute("SELECT COUNT(DISTINCT event_id) FROM backtest").fetchone()[0]})

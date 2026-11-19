"""Backtest walk-forward fuera de muestra (regla 6).

Para cada semana del periodo de prueba, los modelos se ajustan SOLO con partidos anteriores a
esa semana y se predicen los partidos de la semana. Así ningún resultado evaluado se usó para
entrenar. Los resultados alimentan la calibración, el Brier score y la selección de mercados.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from ..config import COMPETITIONS
from ..db import connect
from ..motor.engine import LeagueEngine, load_rows, pool_of
from ..motor.markets import settle

TEST_START = "2025-07-01"


def run(pools=None, test_start: str = TEST_START, log=print):
    """Backtest por pool: cada liga con su propio modelo, copas con el pool de clubes y
    selecciones con el pool internacional. Sólo se evalúan las competiciones de ese pool."""
    con = connect()
    pools = pools or sorted({v["pool"] for v in COMPETITIONS.values()})
    for pool in pools:
        comps = {k for k, v in COMPETITIONS.items() if v["pool"] == pool}
        rows = load_rows(con, pool)
        test = [r for r in rows if r["date_utc"] >= test_start and r["league"] in comps]
        if not test:
            continue
        con.execute(f"DELETE FROM backtest WHERE league IN ({','.join('?' * len(comps))})", list(comps))
        start = datetime.fromisoformat(test_start).replace(tzinfo=timezone.utc)
        end = datetime.now(timezone.utc)
        week, n = start, 0
        while week < end:
            nxt = week + timedelta(days=7)
            a, b = week.isoformat().replace("+00:00", "Z"), nxt.isoformat().replace("+00:00", "Z")
            batch = [r for r in test if a <= r["date_utc"] < b]
            if batch:
                eng = LeagueEngine(pool, rows, week)
                for r in batch:
                    nh = eng.goals.r.n_matches if eng.goals.r else {}
                    if nh.get(r["home_id"], 0) < 5 or nh.get(r["away_id"], 0) < 5:
                        continue        # equipos sin historial suficiente: no se evalúan
                    pred = eng.predict(r["home_id"], r["away_id"], r["home_name"], r["away_name"],
                                       bool(r.get("neutral")), monte_carlo=False)
                    for s in pred["selections"]:
                        out = settle(s["market"], s["selection"], s["line"], r)
                        if out is None:
                            continue
                        con.execute("""INSERT OR REPLACE INTO backtest(event_id, league, date_utc, market, selection,
                                       line, prob, outcome) VALUES (?,?,?,?,?,?,?,?)""",
                                    (r["event_id"], r["league"], r["date_utc"], s["market"], s["selection"],
                                     s["line"], s["prob_raw"], out))
                    n += 1
                con.commit()
            week = nxt
        log(f"  {pool}: {n} partidos evaluados fuera de muestra")


def simulate_selection(con, split: str = "2026-03-01") -> dict:
    """Simula el selector diario sobre el periodo posterior a `split` usando una calibración
    aprendida SOLO con datos anteriores. Mide el acierto real de lo que se habría publicado."""
    from collections import defaultdict

    from .calibration import Calibrator
    from ..config import SELECTION as S, TZ
    from .quality import MARKET_REF, DIRECT_PRICE

    cal = Calibrator(Calibrator.from_db_range(con, None, split))
    max_p = 1 / S["min_fair_odds"]
    rows = con.execute("SELECT * FROM backtest WHERE date_utc >= ?", (split,)).fetchall()
    by_day = defaultdict(list)
    for r in rows:
        if COMPETITIONS.get(r["league"], {}).get("kind") == "friendly":
            continue            # los amistosos no se publican
        q, b = cal.calibrate(r["market"], r["prob"])
        # rango de cuotas de los pronósticos individuales: la cuota mínima para apostar
        # (cuota justa + valor mínimo) debe estar entre odds_min y odds_max
        min_odds = (1 / q) * (1 + S["min_edge"])
        if not (S["odds_min"] <= min_odds <= S["odds_max"]):
            continue
        if not b or b["n"] < 30 or (b["freq"] < b["mean_p"] - S["max_calibration_gap"]):
            continue
        d = datetime.fromisoformat(r["date_utc"].replace("Z", "+00:00")).astimezone(TZ).date()
        by_day[d].append((q, r))
    picks = []
    for d, cands in by_day.items():
        pm, pl, pk, n = defaultdict(int), defaultdict(int), defaultdict(int), 0
        for q, r in sorted(cands, key=lambda x: abs((1 / x[0]) * (1 + S["min_edge"]) - S["target_odds"])):
            if pm[r["event_id"]] >= S["max_picks_per_match"] or pl[r["league"]] >= S["max_picks_per_league"] \
                    or pk[r["market"]] >= S["max_picks_per_market"]:
                continue
            picks.append((q, r))
            pm[r["event_id"]] += 1
            pl[r["league"]] += 1
            pk[r["market"]] += 1
            n += 1
            if n >= S["max_picks"]:
                break
    if not picks:
        return {}
    n = len(picks)
    hit = sum(r["outcome"] for _, r in picks) / n
    by_market = defaultdict(lambda: [0, 0, 0.0])
    for q, r in picks:
        a = by_market[r["market"]]
        a[0] += 1
        a[1] += r["outcome"]
        a[2] += q
    # rentabilidad si sólo se apuesta cuando la casa paga exactamente la cuota mínima (peor caso aceptado)
    profit = sum(((1 / q) * (1 + S["min_edge"]) - 1) if r["outcome"] else -1 for q, r in picks)
    return {"split": split, "n": n, "days": len({d for d in by_day}), "hit": hit,
            "odds_range": [S["odds_min"], S["odds_max"]],
            "mean_min_odds": sum((1 / q) * (1 + S["min_edge"]) for q, _ in picks) / n,
            "roi_at_min_odds": profit / n,
            "mean_prob": sum(q for q, _ in picks) / n,
            "brier": sum((q - r["outcome"]) ** 2 for q, r in picks) / n,
            "by_market": {m: {"n": a[0], "hit": a[1] / a[0], "mean_prob": a[2] / a[0]} for m, a in by_market.items()}}


def simulate_combos(con, split: str = "2026-03-01", max_legs: int = 10, min_legs: int = 4) -> dict:
    """Simula la súper combinada de cada día del periodo fuera de muestra: cuántas veces se habría
    acertado frente a la probabilidad anunciada, y cuántas selecciones fallan normalmente."""
    from collections import defaultdict

    from .calibration import Calibrator
    from ..config import SELECTION as S, TZ

    cal = Calibrator(Calibrator.from_db_range(con, None, split))
    max_p = 1 / S["min_fair_odds"]
    skip = {"OU_CORNERS"}
    from .combo import select_legs

    by_day = defaultdict(list)
    for r in con.execute("SELECT * FROM backtest WHERE date_utc >= ?", (split,)):
        if r["market"] in skip or COMPETITIONS.get(r["league"], {}).get("kind") == "friendly":
            continue
        q, b = cal.calibrate(r["market"], r["prob"])
        if q < S["combo_min_prob_no_odds"] or q > max_p or not b or b["n"] < 30:
            continue
        d = datetime.fromisoformat(r["date_utc"].replace("Z", "+00:00")).astimezone(TZ).date()
        by_day[d].append((r["event_id"], r["market"], q, (q, r["outcome"])))
    res = []
    for d, cands in sorted(by_day.items()):
        legs = select_legs(cands, max_legs)
        if len(legs) < min_legs:
            continue
        n_matches = len({c[0] for c in cands})
        p = 1.0
        for q, _ in legs:
            p *= q
        res.append({"date": str(d), "n_legs": len(legs), "matches": n_matches, "prob": p,
                    "won": all(o for _, o in legs), "misses": sum(1 - o for _, o in legs)})
    if not res:
        return {}

    def summ(xs):
        return {"days": len(xs), "won": sum(x["won"] for x in xs),
                "hit_rate": (sum(x["won"] for x in xs) / len(xs)) if xs else None,
                "mean_prob": (sum(x["prob"] for x in xs) / len(xs)) if xs else None,
                "mean_legs": (sum(x["n_legs"] for x in xs) / len(xs)) if xs else None}
    ten = [x for x in res if x["n_legs"] == max_legs]
    few = [x for x in res if x["matches"] < max_legs]     # días con pocos partidos: varias selecciones por partido
    dist = defaultdict(int)
    for x in res:
        dist[x["misses"]] += 1
    return {"split": split, "days": len(res), "won": sum(x["won"] for x in res),
            "hit_rate": sum(x["won"] for x in res) / len(res),
            "mean_prob": sum(x["prob"] for x in res) / len(res),
            "days_10": len(ten), "won_10": sum(x["won"] for x in ten),
            "mean_prob_10": (sum(x["prob"] for x in ten) / len(ten)) if ten else None,
            "many_matches": summ([x for x in res if x["matches"] >= max_legs]),
            "few_matches": summ(few),
            "misses_dist": dict(sorted(dist.items())), "recent": res[-30:]}


def market_check(con, split: str = "2026-03-01", zone: str = "picks") -> dict:
    """Rendimiento por mercado con TODAS sus selecciones dentro del rango de cuotas de los pronósticos
    (fuera de muestra). Muestra grande y estable para decidir qué mercados se desactivan."""
    from collections import defaultdict

    from .calibration import Calibrator
    from ..config import SELECTION as S

    cal = Calibrator(Calibrator.from_db_range(con, None, split))
    acc = defaultdict(lambda: [0, 0, 0.0])
    for r in con.execute("SELECT league, market, prob, outcome FROM backtest WHERE date_utc >= ?", (split,)):
        if COMPETITIONS.get(r["league"], {}).get("kind") == "friendly":
            continue
        q, _ = cal.calibrate(r["market"], r["prob"])
        ok = (S["odds_min"] <= (1 / q) * (1 + S["min_edge"]) <= S["odds_max"]) if zone == "picks"             else (S["combo_min_prob"] <= q <= 1 / S["min_fair_odds"])
        if ok:
            a = acc[r["market"]]
            a[0] += 1
            a[1] += r["outcome"]
            a[2] += q
    return {m: {"n": n, "hit": w / n, "mean_prob": p / n, "disabled": n >= 300 and w / n < p / n - 0.03}
            for m, (n, w, p) in acc.items()}


def competition_check(con, split: str = "2026-03-01", lo: float = 0.53, hi: float = 0.847) -> dict:
    """Rendimiento por competición en la zona de probabilidades que se publica (fuera de muestra).
    Devuelve {competición: {"n", "hit", "mean_prob", "disabled"}}."""
    from collections import defaultdict

    from .calibration import Calibrator
    cal = Calibrator(Calibrator.from_db_range(con, None, split))
    acc = defaultdict(lambda: [0, 0, 0.0])
    for r in con.execute("SELECT league, market, prob, outcome FROM backtest WHERE date_utc >= ?", (split,)):
        q, _ = cal.calibrate(r["market"], r["prob"])
        if lo <= q <= hi:
            a = acc[r["league"]]
            a[0] += 1
            a[1] += r["outcome"]
            a[2] += q
    return {lg: {"n": n, "hit": w / n, "mean_prob": p / n, "disabled": n >= 300 and w / n < p / n - 0.04}
            for lg, (n, w, p) in acc.items()}


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run(sys.argv[1:] or None)
    import json
    print(json.dumps(simulate_selection(connect()), indent=1, default=str))

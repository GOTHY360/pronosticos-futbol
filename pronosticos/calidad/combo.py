"""Súper combinada: reúne hasta 10 selecciones (de partidos distintos) con mayor probabilidad
entre las que superan el filtro de calidad.

Matemática honesta: la probabilidad de acertar la combinada es el PRODUCTO de las probabilidades.
Con 10 selecciones del 84 % la probabilidad conjunta ronda el 17 %: se gana mucho pocas veces y
se pierde la mayoría. Además el margen de la casa se multiplica en cada selección añadida.
Por eso se muestra separada, con su riesgo, y se registra y liquida como todo lo demás.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

MAX_LEGS = 10
MIN_LEGS = 4
TYPICAL_MARGIN = 0.06          # margen típico por selección si no hay cuota real


GROUP = {"1X2": "goals", "DC": "goals", "OU_GOALS": "goals", "BTTS": "goals", "TEAM_GOALS": "goals",
         "OU_CARDS": "cards", "TEAM_SHOTS": "shots", "TEAM_SOT": "shots", "GK_SAVES": "saves",
         "OU_CORNERS": "corners"}
MAX_PER_MATCH = 3
INCOMPATIBLE = {frozenset({"shots", "saves"})}   # tiros y atajadas del mismo partido están correlacionados


def compatible(groups: set, g: str) -> bool:
    return g not in groups and not any(frozenset({g, x}) in INCOMPATIBLE for x in groups)


def select_legs(cands: list[tuple], max_legs: int = MAX_LEGS) -> list:
    """cands: [(event_id, market, score, item)]. 1ª pasada: la mejor de cada partido. Si no se llega a
    max_legs (días con pocos partidos), 2ª pasada: añade selecciones de OTRO tipo del mismo partido,
    hasta 3 por partido y sin combinar tipos correlacionados."""
    by_ev = {}
    for ev, mk, sc, it in sorted(cands, key=lambda x: -x[2]):
        by_ev.setdefault(ev, []).append((mk, sc, it))
    legs, used = [], {}
    for ev, lst in sorted(by_ev.items(), key=lambda x: -x[1][0][1]):
        mk, sc, it = lst[0]
        legs.append((sc, it))
        used[ev] = {GROUP.get(mk, mk)}
    legs = sorted(legs, key=lambda x: -x[0])[:max_legs]
    keep = {id(it) for _, it in legs}
    used = {ev: g for ev, g in used.items() if any(id(it) in keep for mk, sc, it in by_ev[ev][:1])}
    while len(legs) < max_legs:
        best = None
        for ev, groups in used.items():
            if len(groups) >= MAX_PER_MATCH:
                continue
            for mk, sc, it in by_ev[ev]:
                g = GROUP.get(mk, mk)
                if id(it) not in keep and compatible(groups, g):
                    if not best or sc > best[1]:
                        best = (ev, sc, it, g)
                    break
        if not best:
            break
        ev, sc, it, g = best
        legs.append((sc, it))
        keep.add(id(it))
        used[ev].add(g)
    return [it for _, it in legs]


def build(candidates: list[dict], blocked_events: set | None = None) -> dict | None:
    blocked_events = blocked_events or set()
    pool = [(c["event_id"], c["market"], 0.6 * c["prob"] + 0.4 * c["prob_low"], c)
            for c in candidates if c["event_id"] not in blocked_events and c["market"] in GROUP]
    legs = select_legs(pool)
    if len(legs) < MIN_LEGS:
        return None
    prob = math.prod(c["prob"] for c in legs)
    prob_low = math.prod(c["prob_low"] for c in legs)
    prob_high = math.prod(c["prob_high"] for c in legs)
    fair = 1 / prob
    est = math.prod(c["odds_dec"] if c.get("odds_dec") else (1 / c["prob"]) / (1 + TYPICAL_MARGIN) for c in legs)
    return {
        "legs": [{"event_id": c["event_id"], "league": c["league"], "match": f'{c["ctx"]["home_name"]} vs {c["ctx"]["away_name"]}',
                  "kickoff_utc": c["ctx"]["date_utc"], "market": c["market"], "selection": c["selection"],
                  "line": c["line"], "label": c["label"], "prob": c["prob"], "prob_low": c["prob_low"],
                  "prob_high": c["prob_high"], "odds_dec": c.get("odds_dec"), "status": "pending"}
                 for c in sorted(legs, key=lambda c: c["ctx"]["date_utc"])],
        "n_legs": len(legs), "n_matches": len({c["event_id"] for c in legs}), "prob": prob, "prob_low": prob_low, "prob_high": prob_high,
        "fair_odds": fair, "est_odds": est,
        # valor esperado con la cuota típica estimada: casi siempre negativo por el margen acumulado
        "expected_value": prob * est - 1,
    }


def prune(con, target: str):
    """Quita de la combinada las selecciones que el analista rechazó o dejó en revisión."""
    row = con.execute("SELECT * FROM combos WHERE target_date=? AND status='pending'", (target,)).fetchone()
    if not row:
        return
    bad = {(r["event_id"], r["market"], r["selection"], float(r["line"])) for r in con.execute(
        """SELECT event_id, market, selection, line FROM picks WHERE target_date=?
           AND (review_status IN ('rechazado','revisar') OR status='withdrawn')""", (target,))}
    legs = [l for l in json.loads(row["legs"]) if (l["event_id"], l["market"], l["selection"], float(l["line"])) not in bad]
    if len(legs) == row["n_legs"]:
        return
    if len(legs) < MIN_LEGS:
        con.execute("DELETE FROM combos WHERE combo_id=?", (row["combo_id"],))
    else:
        prob = math.prod(l["prob"] for l in legs)
        est = math.prod(l["odds_dec"] if l.get("odds_dec") else (1 / l["prob"]) / (1 + TYPICAL_MARGIN) for l in legs)
        con.execute("""UPDATE combos SET legs=?, n_legs=?, prob=?, prob_low=?, prob_high=?, fair_odds=?, est_odds=?
                       WHERE combo_id=?""",
                    (json.dumps(legs, ensure_ascii=False), len(legs), prob, math.prod(l["prob_low"] for l in legs),
                     math.prod(l["prob_high"] for l in legs), 1 / prob, est, row["combo_id"]))
    con.commit()


def save(con, target: str, combo: dict):
    con.execute("""INSERT OR REPLACE INTO combos(target_date, created_at, legs, n_legs, prob, prob_low, prob_high,
                   fair_odds, est_odds) VALUES (?,?,?,?,?,?,?,?,?)""",
                (target, datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(combo["legs"], ensure_ascii=False),
                 combo["n_legs"], combo["prob"], combo["prob_low"], combo["prob_high"], combo["fair_odds"], combo["est_odds"]))
    con.commit()


def settle(con, settle_fn, result_rows: dict):
    """Liquida las combinadas pendientes. Una selección anulada (partido aplazado) se elimina."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in con.execute("SELECT * FROM combos WHERE status='pending'").fetchall():
        legs = json.loads(row["legs"])
        for lg in legs:
            if lg["status"] != "pending":
                continue
            st = con.execute("SELECT status FROM matches WHERE event_id=?", (lg["event_id"],)).fetchone()
            if st and st["status"] == "postponed":
                lg["status"] = "void"
                continue
            res = result_rows(lg["event_id"], lg["league"])
            if res:
                out = settle_fn(lg["market"], lg["selection"], lg["line"], res)
                if out is not None:
                    lg["status"] = "won" if out else "lost"
                    lg["result"] = f"{res['home_goals']}-{res['away_goals']}"
        active = [l for l in legs if l["status"] != "void"]
        won = sum(l["status"] == "won" for l in active)
        status, detail = "pending", f"{won}/{len(active)} acertadas"
        if any(l["status"] == "lost" for l in active):
            status = "lost"
        elif active and all(l["status"] == "won" for l in active):
            status = "won"
        elif not active:
            status = "void"
        con.execute("UPDATE combos SET legs=?, status=?, result_detail=?, settled_at=? WHERE combo_id=?",
                    (json.dumps(legs, ensure_ascii=False), status, detail, now if status != "pending" else None,
                     row["combo_id"]))
    con.commit()

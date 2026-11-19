"""Modelos especializados por mercado: cada estadística (goles, córners, tarjetas, tiros, tiros a
puerta, atajadas) elige SUS propios hiperparámetros (vida media temporal y fuerza de la previa)
según su verosimilitud fuera de muestra. Cada modelo se entrena y evalúa por separado."""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

from ..config import COMPETITIONS, MODEL_PARAMS_PATH
from ..db import connect
from ..motor.engine import load_rows
from ..motor.models import fit_ratings, time_weights

GRID_HALF_LIFE = [90, 180, 365]
GRID_PRIOR = [2.0, 4.0, 8.0]
STATS = {"goals": ("home_goals", "away_goals"), "corners": ("h_corners", "a_corners"),
         "cards": ("h_cards", "a_cards"), "shots": ("h_shots", "a_shots"), "sot": ("h_sot", "a_sot"),
         "saves": ("h_saves", "a_saves")}


def _nll(y, mu, k):
    if k > 1e4:
        return mu - y * math.log(max(mu, 1e-9)) + math.lgamma(y + 1)
    p = k / (k + mu)
    return -(math.lgamma(y + k) - math.lgamma(k) - math.lgamma(y + 1) + k * math.log(p)
             + y * math.log(max(1 - p, 1e-12)))


def evaluate(rows, kh, ka, half_life, prior, start: datetime, end: datetime) -> tuple[float, int]:
    rows = [r for r in rows if r.get(kh) is not None and r.get(ka) is not None]
    month, tot, n = start, 0.0, 0
    while month < end:
        nxt = month + timedelta(days=30)
        a, b = month.isoformat().replace("+00:00", "Z"), nxt.isoformat().replace("+00:00", "Z")
        train = [r for r in rows if r["date_utc"] < a]
        test = [r for r in rows if a <= r["date_utc"] < b]
        if len(train) > 150 and test:
            w = time_weights([r["date_utc"] for r in train], month, half_life)
            rt = fit_ratings([r["home_id"] for r in train], [r["away_id"] for r in train],
                             np.array([r[kh] for r in train], float), np.array([r[ka] for r in train], float),
                             w, prior, neutral=np.array([bool(r.get("neutral")) for r in train]))
            k = rt.dispersion if kh != "home_goals" else 1e6
            for r in test:
                if r["home_id"] not in rt.att or r["away_id"] not in rt.att:
                    continue
                mh, ma = rt.expected(r["home_id"], r["away_id"], bool(r.get("neutral")))
                tot += _nll(r[kh], mh, k) + _nll(r[ka], ma, k)
                n += 2
        month = nxt
    return tot, n


def run(log=print, start="2025-08-01"):
    con = connect()
    pools = sorted({v["pool"] for v in COMPETITIONS.values()})
    data = {p: load_rows(con, p) for p in pools}
    s = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    e = datetime.now(timezone.utc)
    best = {}
    for stat, (kh, ka) in STATS.items():
        scores = {}
        for hl in GRID_HALF_LIFE:
            for pr in GRID_PRIOR:
                tot = n = 0
                for p, rows in data.items():
                    t, k = evaluate(rows, kh, ka, hl, pr, s, e)
                    tot, n = tot + t, n + k
                if n:
                    scores[(hl, pr)] = tot / n
        if not scores:
            continue
        (hl, pr), sc = min(scores.items(), key=lambda x: x[1])
        default = scores.get((180, 4.0))
        best[stat] = {"half_life": hl, "prior": pr, "nll": round(sc, 5),
                      "nll_default": round(default, 5) if default else None}
        log(f"  {stat}: vida media {hl} días, previa {pr} (NLL {sc:.4f} vs. {default:.4f} por defecto)")
    best["_updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    MODEL_PARAMS_PATH.write_text(json.dumps(best, indent=1), encoding="utf-8")
    return best


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run()

"""Calibración de probabilidades y métricas (Brier score, log-loss, curva de fiabilidad)."""
from __future__ import annotations

import json
import math
from collections import defaultdict

BINS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0001]
SHRINK = 60     # pseudo-observaciones hacia la probabilidad bruta en tramos con pocos datos


def _bin(p):
    for i in range(len(BINS) - 1):
        if BINS[i] <= p < BINS[i + 1]:
            return i
    return len(BINS) - 2


class Calibrator:
    """Mapa por familia de mercado: prob bruta -> frecuencia observada (suavizada)."""

    def __init__(self, table: dict | None = None):
        self.table = table or {}

    @classmethod
    def from_db(cls, con, min_date: str | None = None):
        return cls(cls.from_db_range(con, min_date, None))

    @staticmethod
    def from_db_range(con, min_date: str | None, max_date: str | None) -> dict:
        q = "SELECT market, prob, outcome FROM backtest WHERE 1=1"
        args = []
        if min_date:
            q += " AND date_utc >= ?"
            args.append(min_date)
        if max_date:
            q += " AND date_utc < ?"
            args.append(max_date)
        acc = defaultdict(lambda: [[0, 0.0, 0.0] for _ in range(len(BINS) - 1)])
        for m, p, o in con.execute(q, args):
            b = acc[m][_bin(p)]
            b[0] += 1
            b[1] += p
            b[2] += o
        table = {}
        for m, bins in acc.items():
            table[m] = [{"lo": BINS[i], "hi": min(BINS[i + 1], 1.0), "n": n,
                         "mean_p": (sp / n) if n else None, "freq": (so / n) if n else None}
                        for i, (n, sp, so) in enumerate(bins)]
        return table

    def calibrate(self, market: str, p: float) -> tuple[float, dict | None]:
        bins = self.table.get(market)
        if not bins:
            return p, None
        b = bins[_bin(p)]
        if not b["n"]:
            return p, b
        # corrige el sesgo observado en el tramo, suavizado según tamaño de muestra
        bias = b["freq"] - b["mean_p"]
        w = b["n"] / (b["n"] + SHRINK)
        return float(min(max(p + w * bias, 0.001), 0.999)), b

    def to_json(self):
        return json.dumps(self.table)


def metrics(con, group_by: str = "market", min_date: str | None = None) -> list[dict]:
    """Brier score, log-loss y frecuencia por mercado (o liga)."""
    col = {"market": "market", "league": "league", "league_market": "league || '|' || market"}[group_by]
    q = f"SELECT {col} g, prob, outcome FROM backtest"
    args = []
    if min_date:
        q += " WHERE date_utc >= ?"
        args.append(min_date)
    acc = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])
    for g, p, o in con.execute(q, args):
        a = acc[g]
        a[0] += 1
        a[1] += (p - o) ** 2
        pc = min(max(p, 1e-6), 1 - 1e-6)
        a[2] += -(o * math.log(pc) + (1 - o) * math.log(1 - pc))
        a[3] += o
        a[4] += p
    out = []
    for g, (n, b, ll, so, sp) in sorted(acc.items()):
        base = so / n
        brier_ref = base * (1 - base)                 # Brier de predecir siempre la frecuencia base
        out.append({"group": g, "n": n, "brier": b / n, "brier_ref": brier_ref,
                    "skill": 1 - (b / n) / brier_ref if brier_ref else 0.0,
                    "logloss": ll / n, "freq": base, "mean_prob": sp / n})
    return out


def holdout(con, split: str) -> dict:
    """La calibración también se valida fuera de muestra: se aprende con el backtest anterior a
    `split` y se mide sobre el posterior. Devuelve Brier bruto vs. calibrado y la precisión real de
    las selecciones que el selector habría considerado (prob. calibrada 0.72–0.85)."""
    cal = Calibrator(Calibrator.from_db_range(con, None, split))
    raw = calb = 0.0
    n = 0
    zone = [0, 0, 0.0]
    for m, p, o in con.execute("SELECT market, prob, outcome FROM backtest WHERE date_utc >= ?", (split,)):
        q, _ = cal.calibrate(m, p)
        raw += (p - o) ** 2
        calb += (q - o) ** 2
        n += 1
        if 0.72 <= q <= 0.85:
            zone[0] += 1
            zone[1] += o
            zone[2] += q
    return {"split": split, "n": n, "brier_raw": raw / n if n else None, "brier_cal": calb / n if n else None,
            "zone_n": zone[0], "zone_hit": zone[1] / zone[0] if zone[0] else None,
            "zone_mean_prob": zone[2] / zone[0] if zone[0] else None}


def reliability(con, market: str | None = None, lo: float = 0.0) -> list[dict]:
    """Curva de fiabilidad: en cada tramo, prob media anunciada vs. frecuencia real."""
    q = "SELECT prob, outcome FROM backtest WHERE prob >= ?"
    args = [lo]
    if market:
        q += " AND market = ?"
        args.append(market)
    acc = [[0, 0.0, 0.0] for _ in range(len(BINS) - 1)]
    for p, o in con.execute(q, args):
        b = acc[_bin(p)]
        b[0] += 1
        b[1] += p
        b[2] += o
    return [{"lo": BINS[i], "hi": min(BINS[i + 1], 1.0), "n": n, "mean_p": sp / n, "freq": so / n}
            for i, (n, sp, so) in enumerate(acc) if n]

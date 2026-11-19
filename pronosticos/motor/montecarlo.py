"""Simulador Monte Carlo.

Dos niveles de simulación:
1. Incertidumbre del modelo: se simulan cientos de valores plausibles de las medias esperadas
   (goles, córners, tarjetas…) según la precisión con que se estimó cada equipo. Para cada
   simulación se recalculan todas las probabilidades. Resultado: intervalo p5–p95 por selección.
2. Escenarios del partido: se simulan miles de marcadores para mostrar los resultados más
   probables y la distribución de goles.
"""
from __future__ import annotations

import numpy as np

from ..config import MODEL
from .markets import count_markets, goal_markets


def _key(s):
    return (s["market"], s["selection"], float(s["line"]))


def goal_uncertainty(gm, h, a, neutral, adj, home_name, away_name, rng, draws=None) -> dict:
    draws = draws or MODEL["mc_draws"]
    lh, la = gm.r.expected(h, a, neutral, adj)
    sh, sa = gm.r.sd(h, a)
    zs = rng.standard_normal((draws, 2))
    acc = {}
    for zh, za in zs:
        m = gm.matrix_from(lh * np.exp(sh * zh - sh * sh / 2), la * np.exp(sa * za - sa * sa / 2))
        for s in goal_markets(m, home_name, away_name):
            acc.setdefault(_key(s), []).append(s["prob_raw"])
    return {k: (float(np.mean(v)), float(np.percentile(v, 5)), float(np.percentile(v, 95))) for k, v in acc.items()}


def count_uncertainty(cm, stat, h, a, neutral, adj, home_name, away_name, rng, draws=150) -> dict:
    mh, ma = cm.r.expected(h, a, neutral, adj)
    sh, sa = cm.r.sd(h, a)
    acc = {}
    for zh, za in rng.standard_normal((draws, 2)):
        dh, da = cm.dists_from(mh * np.exp(sh * zh - sh * sh / 2), ma * np.exp(sa * za - sa * sa / 2))
        for s in count_markets(stat, dh, da, home_name, away_name):
            acc.setdefault(_key(s), []).append(s["prob_raw"])
    return {k: (float(np.mean(v)), float(np.percentile(v, 5)), float(np.percentile(v, 95))) for k, v in acc.items()}


def scenarios(matrix: np.ndarray, rng, n: int = 10000) -> dict:
    """Simula n partidos con la matriz de marcadores y resume los escenarios."""
    flat = matrix.ravel()
    idx = rng.choice(flat.size, size=n, p=flat / flat.sum())
    hg, ag = np.divmod(idx, matrix.shape[1])
    vals, counts = np.unique(np.stack([hg, ag], 1), axis=0, return_counts=True)
    order = np.argsort(-counts)[:6]
    tot = hg + ag
    return {
        "n": n,
        "top_scores": [{"score": f"{int(vals[i][0])}-{int(vals[i][1])}", "p": float(counts[i] / n)} for i in order],
        "home_win": float(np.mean(hg > ag)), "draw": float(np.mean(hg == ag)), "away_win": float(np.mean(hg < ag)),
        "goals_dist": [float(np.mean(tot == k)) for k in range(7)] + [float(np.mean(tot >= 7))],
        "mean_goals": float(tot.mean()),
    }

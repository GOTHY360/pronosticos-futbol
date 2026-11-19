"""Motor estadístico independiente.

- GoalsModel: Poisson con fuerzas de ataque/defensa ponderadas en el tiempo + corrección
  Dixon-Coles para marcadores bajos. Genera la matriz de marcadores.
- CountModel: mismo esquema multiplicativo para córners, tarjetas, tiros, tiros a puerta y
  atajadas, con distribución binomial negativa (sobredispersión estimada por liga).
- Elo: rating independiente usado como control de consistencia del 1X2.
Sólo depende de numpy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from ..config import MODEL


def _parse(d: str) -> datetime:
    return datetime.fromisoformat(d.replace("Z", "+00:00"))


def time_weights(dates: list[str], ref: datetime, half_life: float) -> np.ndarray:
    age = np.array([(ref - _parse(d)).total_seconds() / 86400 for d in dates])
    return np.power(0.5, np.clip(age, 0, None) / half_life)


# ---------------------------------------------------------------------------
# Modelo multiplicativo genérico: y_home = base*home*att[h]*def[a] ; y_away = base*att[a]*def[h]
# ---------------------------------------------------------------------------
@dataclass
class Ratings:
    base: float = 1.0
    home: float = 1.0
    att: dict = field(default_factory=dict)
    dfn: dict = field(default_factory=dict)
    n_matches: dict = field(default_factory=dict)
    dispersion: float = 1e6       # parámetro k de la binomial negativa (grande = Poisson)
    sd_att: dict = field(default_factory=dict)   # incertidumbre (desv. típica en escala log)
    sd_dfn: dict = field(default_factory=dict)

    def expected(self, h: str, a: str, neutral: bool = False, adj: dict | None = None) -> tuple[float, float]:
        """adj: {team_id: {"att": mult, "def": mult}} — ajustes por bajas/alineación."""
        adj = adj or {}
        ah = self.att.get(h, 1.0) * adj.get(h, {}).get("att", 1.0)
        aa = self.att.get(a, 1.0) * adj.get(a, {}).get("att", 1.0)
        dh = self.dfn.get(h, 1.0) * adj.get(h, {}).get("def", 1.0)
        da = self.dfn.get(a, 1.0) * adj.get(a, {}).get("def", 1.0)
        hf = 1.0 if neutral else self.home
        return self.base * hf * ah * da, self.base * aa * dh

    def sd(self, h: str, a: str) -> tuple[float, float]:
        """Desviación típica (log) de cada media esperada: base de la simulación Monte Carlo."""
        return (math.hypot(self.sd_att.get(h, 0.3), self.sd_dfn.get(a, 0.3)),
                math.hypot(self.sd_att.get(a, 0.3), self.sd_dfn.get(h, 0.3)))


def fit_ratings(home: list[str], away: list[str], yh: np.ndarray, ya: np.ndarray,
                w: np.ndarray, prior_strength: float = 4.0, iters: int = 60,
                neutral: np.ndarray | None = None) -> Ratings:
    teams = sorted(set(home) | set(away))
    idx = {t: i for i, t in enumerate(teams)}
    hi = np.array([idx[t] for t in home])
    ai = np.array([idx[t] for t in away])
    n = len(teams)
    neu = np.zeros(len(home), bool) if neutral is None else np.asarray(neutral, bool)
    att, dfn = np.ones(n), np.ones(n)
    tot_w = w.sum()
    base = (np.sum(w * (yh + ya)) / (2 * tot_w)) if tot_w > 0 else 1.0
    nn = ~neu
    home_f = (np.sum(w[nn] * yh[nn]) / max(np.sum(w[nn] * ya[nn]), 1e-9)) ** 0.5 if nn.any() else 1.0
    for _ in range(iters):
        hm = np.where(neu, 1.0, home_f)
        # ataque
        num = np.bincount(hi, w * yh, n) + np.bincount(ai, w * ya, n)
        den = (np.bincount(hi, w * base * hm * dfn[ai], n)
               + np.bincount(ai, w * base * dfn[hi], n))
        att = (num + prior_strength * base) / (den + prior_strength * base)
        # defensa (concedido)
        num = np.bincount(ai, w * yh, n) + np.bincount(hi, w * ya, n)
        den = (np.bincount(ai, w * base * hm * att[hi], n)
               + np.bincount(hi, w * base * att[ai], n))
        dfn = (num + prior_strength * base) / (den + prior_strength * base)
        # normaliza y reajusta base y localía
        att /= np.exp(np.mean(np.log(att)))
        dfn /= np.exp(np.mean(np.log(dfn)))
        eh = base * hm * att[hi] * dfn[ai]
        ea = base * att[ai] * dfn[hi]
        base *= np.sum(w * (yh + ya)) / max(np.sum(w * (eh + ea)), 1e-9)
        if nn.any():
            eh0 = base * att[hi] * dfn[ai]
            home_f = np.sum(w[nn] * yh[nn]) / max(np.sum(w[nn] * eh0[nn]), 1e-9)
    hm = np.where(neu, 1.0, home_f)
    eh = base * hm * att[hi] * dfn[ai]
    ea = base * att[ai] * dfn[hi]
    # información de Fisher aproximada (Poisson): var(log λ) ≈ 1 / Σ w·λ
    info_att = np.bincount(hi, w * eh, n) + np.bincount(ai, w * ea, n) + prior_strength * base
    info_dfn = np.bincount(ai, w * eh, n) + np.bincount(hi, w * ea, n) + prior_strength * base
    r = Ratings(base=float(base), home=float(home_f),
                att={t: float(att[idx[t]]) for t in teams},
                dfn={t: float(dfn[idx[t]]) for t in teams},
                n_matches={t: int(np.sum(hi == idx[t]) + np.sum(ai == idx[t])) for t in teams},
                sd_att={t: float(1 / math.sqrt(info_att[idx[t]])) for t in teams},
                sd_dfn={t: float(1 / math.sqrt(info_dfn[idx[t]])) for t in teams})
    # sobredispersión (método de momentos): var = mu + mu^2/k
    mu = np.concatenate([eh, ea])
    y = np.concatenate([yh, ya])
    ww = np.concatenate([w, w])
    excess = np.sum(ww * ((y - mu) ** 2 - mu)) / max(np.sum(ww), 1e-9)
    mu2 = np.sum(ww * mu ** 2) / max(np.sum(ww), 1e-9)
    r.dispersion = float(mu2 / excess) if excess > 1e-6 else 1e6
    return r


# ---------------------------------------------------------------------------
# Distribuciones
# ---------------------------------------------------------------------------
def poisson_pmf(lam: float, kmax: int) -> np.ndarray:
    k = np.arange(kmax + 1)
    logp = k * math.log(max(lam, 1e-9)) - lam - np.array([math.lgamma(i + 1) for i in k])
    p = np.exp(logp)
    return p / p.sum()


def negbin_pmf(mu: float, k_disp: float, kmax: int) -> np.ndarray:
    if k_disp > 1e4:
        return poisson_pmf(mu, kmax)
    r = k_disp
    p = r / (r + mu)
    ks = np.arange(kmax + 1)
    logp = np.array([math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1) for k in ks]) \
        + r * math.log(p) + ks * math.log(max(1 - p, 1e-12))
    out = np.exp(logp)
    return out / out.sum()


def dc_tau(x, y, lh, la, rho):
    if x == 0 and y == 0:
        return 1 - lh * la * rho
    if x == 0 and y == 1:
        return 1 + lh * rho
    if x == 1 and y == 0:
        return 1 + la * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


# ---------------------------------------------------------------------------
# Goles: Poisson + Dixon-Coles
# ---------------------------------------------------------------------------
class GoalsModel:
    """Poisson + Dixon-Coles. Parámetros (vida media, previa) específicos del mercado de goles."""
    name = "goals_dixon_coles"

    def __init__(self, half_life: float | None = None, prior: float | None = None):
        self.r: Ratings | None = None
        self.rho = 0.0
        self.half_life = half_life or MODEL["half_life_days"]
        self.prior = prior or MODEL["prior_strength"]

    def fit(self, rows: list[dict], ref: datetime):
        w = time_weights([r["date_utc"] for r in rows], ref, self.half_life)
        yh = np.array([r["home_goals"] for r in rows], float)
        ya = np.array([r["away_goals"] for r in rows], float)
        neu = np.array([bool(r.get("neutral")) for r in rows])
        self.r = fit_ratings([r["home_id"] for r in rows], [r["away_id"] for r in rows], yh, ya, w,
                             self.prior, neutral=neu)
        # rho por máxima verosimilitud (búsqueda en rejilla, sólo marcadores bajos)
        low = [(self.r.expected(r["home_id"], r["away_id"], bool(r.get("neutral"))), int(x), int(y), wi)
               for r, x, y, wi in zip(rows, yh, ya, w) if x <= 1 and y <= 1]
        best, best_ll = 0.0, -1e18
        for rho in np.linspace(-0.25, 0.15, 41):
            ll = 0.0
            for (lh, la), x, y, wi in low:
                t = dc_tau(x, y, lh, la, rho)
                if t <= 0:
                    ll = -1e18
                    break
                ll += wi * math.log(t)
            if ll > best_ll:
                best, best_ll = rho, ll
        self.rho = float(best)
        return self

    def matrix_from(self, lh: float, la: float) -> np.ndarray:
        n = MODEL["max_goals"]
        m = np.outer(poisson_pmf(lh, n), poisson_pmf(la, n))
        for x in range(2):
            for y in range(2):
                m[x, y] *= max(dc_tau(x, y, lh, la, self.rho), 0.0)
        return m / m.sum()

    def score_matrix(self, h: str, a: str, neutral: bool = False, adj: dict | None = None):
        lh, la = self.r.expected(h, a, neutral, adj)
        return self.matrix_from(lh, la), lh, la


class CountModel:
    """Córners, tarjetas, tiros, tiros a puerta, atajadas: un modelo independiente por estadística."""
    def __init__(self, stat: str, half_life: float | None = None, prior: float | None = None):
        self.stat = stat
        self.name = f"{stat}_negbin"
        self.r: Ratings | None = None
        self.half_life = half_life or MODEL["half_life_days"]
        self.prior = prior or MODEL["prior_strength"]

    def fit(self, rows: list[dict], ref: datetime):
        rows = [r for r in rows if r.get(f"h_{self.stat}") is not None and r.get(f"a_{self.stat}") is not None]
        if len(rows) < 60:
            self.r = None
            return self
        w = time_weights([r["date_utc"] for r in rows], ref, self.half_life)
        self.r = fit_ratings([r["home_id"] for r in rows], [r["away_id"] for r in rows],
                             np.array([r[f"h_{self.stat}"] for r in rows], float),
                             np.array([r[f"a_{self.stat}"] for r in rows], float), w, self.prior,
                             neutral=np.array([bool(r.get("neutral")) for r in rows]))
        return self

    def dists_from(self, mh: float, ma: float, kmax: int = 40):
        k = self.r.dispersion
        return negbin_pmf(mh, k, kmax), negbin_pmf(ma, k, kmax)

    def dists(self, h: str, a: str, neutral: bool = False, adj: dict | None = None, kmax: int = 40):
        mh, ma = self.r.expected(h, a, neutral, adj)
        dh, da = self.dists_from(mh, ma, kmax)
        return dh, da, mh, ma


# ---------------------------------------------------------------------------
# Elo (control de consistencia, independiente del modelo Poisson)
# ---------------------------------------------------------------------------
class Elo:
    name = "elo"

    def __init__(self):
        self.rating: dict[str, float] = {}
        self.draw_rate = 0.26

    def fit(self, rows: list[dict]):
        k, hadv = MODEL["elo_k"], MODEL["elo_home_adv"]
        draws = 0
        for r in sorted(rows, key=lambda x: x["date_utc"]):
            rh = self.rating.get(r["home_id"], 1500.0)
            ra = self.rating.get(r["away_id"], 1500.0)
            adv = 0 if r.get("neutral") else hadv
            exp_h = 1 / (1 + 10 ** ((ra - rh - adv) / 400))
            gd = r["home_goals"] - r["away_goals"]
            s = 1.0 if gd > 0 else (0.5 if gd == 0 else 0.0)
            draws += gd == 0
            mult = 1 if abs(gd) <= 1 else (1.5 if abs(gd) == 2 else (11 + abs(gd)) / 8)
            delta = k * mult * (s - exp_h)
            self.rating[r["home_id"]] = rh + delta
            self.rating[r["away_id"]] = ra - delta
        if rows:
            self.draw_rate = draws / len(rows)
        return self

    def probs(self, h: str, a: str, neutral: bool = False) -> tuple[float, float, float]:
        rh = self.rating.get(h, 1500.0)
        ra = self.rating.get(a, 1500.0)
        e = 1 / (1 + 10 ** ((ra - rh - (0 if neutral else MODEL["elo_home_adv"])) / 400))
        pd = self.draw_rate * (1 - abs(e - 0.5) * 1.2)
        pd = max(min(pd, 0.34), 0.12)
        return e * (1 - pd), pd, (1 - e) * (1 - pd)

"""Motor estadístico NBA (independiente de Claude).

EQUIPOS — puntos esperados = base · localía · ataque · defensa del rival (ponderado en el tiempo),
más ajustes de calendario/fatiga APRENDIDOS de los datos (regresión sobre residuos):
noches consecutivas, 3 partidos en 4 noches, descanso largo, gira fuera de casa, altitud.
Margen y total ~ Normal bivariada (desviaciones estimadas de los residuos). Los resultados
finales incluyen la prórroga, así que todo se calcula "prórroga incluida".

JUGADORES — minutos proyectados (ponderados por partidos recientes, ajustados por descanso,
riesgo de paliza, minutos que dejan libres los compañeros ausentes y riesgo de faltas) × tasas
por minuto (puntos, triples, rebotes, asistencias, robos, tapones) ajustadas por rival y ritmo,
con aumento de uso cuando faltan compañeros. Distribución por Monte Carlo (minutos inciertos ×
binomial negativa), que también da puntos+rebotes+asistencias, dobles-dobles, etc.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from pronosticos.motor.models import fit_ratings, time_weights

from .config import MODEL

ALTITUDE_TEAMS = {"Denver Nuggets", "Utah Jazz"}
STATS = ["pts", "fg3m", "reb", "ast", "stl", "blk", "tov"]


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ---------------------------------------------------------------------------
# Calendario y fatiga
# ---------------------------------------------------------------------------
class Schedule:
    """Historial de fechas/sedes por equipo para calcular descanso y carga."""

    def __init__(self, games: list[dict]):
        self.by_team = defaultdict(list)          # team_id -> [(datetime, is_home, home_team_name)]
        for g in sorted(games, key=lambda x: x["date_utc"]):
            d = _dt(g["date_utc"])
            self.by_team[g["home_id"]].append((d, 1, g["home"]))
            self.by_team[g["away_id"]].append((d, 0, g["home"]))

    def context(self, team: str, when: datetime, is_home: bool, venue_team: str) -> dict:
        prev = [x for x in self.by_team.get(team, []) if x[0] < when - timedelta(hours=3)]
        last = prev[-1][0] if prev else None
        # descanso en horas desde el partido anterior: < 36 h = noches consecutivas (back-to-back)
        hrs = (when - last).total_seconds() / 3600 if last else 999
        b2b = hrs < 36
        n4 = sum(1 for x in prev if x[0] >= when - timedelta(days=4))
        road = 0
        for x in reversed(prev):
            if x[1] == 0:
                road += 1
            else:
                break
        load7 = sum(1 for x in prev if x[0] >= when - timedelta(days=7))
        return {"rest": 0 if b2b else max(1, min(int(hrs // 24), 7)), "b2b": int(b2b), "three_in_four": int(n4 >= 2),
                "rest3plus": int(hrs >= 72 + 12), "road_trip": 0 if is_home else road + 1,
                "altitude": int(not is_home and venue_team in ALTITUDE_TEAMS), "games_last7": load7}


FATIGUE_FEATURES = ["b2b", "three_in_four", "rest3plus", "long_road", "altitude", "opp_b2b", "opp_rest3plus"]


def fatigue_vector(own: dict, opp: dict) -> list[float]:
    return [own["b2b"], own["three_in_four"], own["rest3plus"], int(own["road_trip"] >= 4), own["altitude"],
            opp["b2b"], opp["rest3plus"]]


# ---------------------------------------------------------------------------
# Modelo de equipos
# ---------------------------------------------------------------------------
@dataclass
class TeamModel:
    r: object = None
    coef: np.ndarray = field(default_factory=lambda: np.zeros(len(FATIGUE_FEATURES)))
    sd_margin: float = 12.5
    sd_total: float = 18.5
    rho: float = 0.0
    league_total: float = 225.0
    elo: dict = field(default_factory=dict)

    def fit(self, games: list[dict], cutoff: datetime, sched: Schedule):
        rows = [g for g in games if g["home_pts"] is not None and _dt(g["date_utc"]) < cutoff]
        if len(rows) < 100:
            return self
        w = time_weights([g["date_utc"] for g in rows], cutoff, MODEL["half_life_days"])
        yh = np.array([g["home_pts"] for g in rows], float)
        ya = np.array([g["away_pts"] for g in rows], float)
        self.r = fit_ratings([g["home_id"] for g in rows], [g["away_id"] for g in rows], yh, ya, w,
                             MODEL["prior_strength"], neutral=np.array([bool(g["neutral"]) for g in rows]))
        # residuos -> efectos de fatiga (regresión ridge ponderada)
        X, y, ww = [], [], []
        eh, ea = [], []
        for g, wi in zip(rows, w):
            mh, ma = self.r.expected(g["home_id"], g["away_id"], bool(g["neutral"]))
            d = _dt(g["date_utc"])
            ch = sched.context(g["home_id"], d, True, g["home"])
            ca = sched.context(g["away_id"], d, False, g["home"])
            X.append(fatigue_vector(ch, ca)); y.append(g["home_pts"] - mh); ww.append(wi)
            X.append(fatigue_vector(ca, ch)); y.append(g["away_pts"] - ma); ww.append(wi)
            eh.append(mh); ea.append(ma)
        X, y, ww = np.array(X, float), np.array(y), np.array(ww)
        lam = 50.0
        A = X.T @ (X * ww[:, None]) + lam * np.eye(X.shape[1])
        self.coef = np.linalg.solve(A, X.T @ (y * ww))
        # desviaciones de margen y total sobre los últimos ~2 meses
        recent = np.array([_dt(g["date_utc"]) >= cutoff - timedelta(days=120) for g in rows])
        mh_, ma_ = np.array(eh), np.array(ea)
        rm = (yh - ya) - (mh_ - ma_)
        rt = (yh + ya) - (mh_ + ma_)
        sel = recent if recent.sum() > 200 else np.ones(len(rows), bool)
        self.sd_margin = float(np.std(rm[sel]))
        self.sd_total = float(np.std(rt[sel]))
        rh_, ra_ = yh - mh_, ya - ma_
        self.rho = float(np.corrcoef(rh_[sel], ra_[sel])[0, 1])
        self.league_total = float(np.average(yh + ya, weights=w))
        self._fit_elo(rows)
        return self

    def _fit_elo(self, rows):
        k, hadv = MODEL["elo_k"], MODEL["elo_home_adv"]
        for g in sorted(rows, key=lambda x: x["date_utc"]):
            rh, ra = self.elo.get(g["home_id"], 1500.0), self.elo.get(g["away_id"], 1500.0)
            e = 1 / (1 + 10 ** ((ra - rh - (0 if g["neutral"] else hadv)) / 400))
            mov = abs(g["home_pts"] - g["away_pts"])
            mult = ((mov + 3) ** 0.8) / (7.5 + 0.006 * abs(rh - ra))
            s = 1.0 if g["home_pts"] > g["away_pts"] else 0.0
            self.elo[g["home_id"]] = rh + k * mult * (s - e)
            self.elo[g["away_id"]] = ra - k * mult * (s - e)

    def elo_prob(self, h, a, neutral=False) -> float:
        rh, ra = self.elo.get(h, 1500.0), self.elo.get(a, 1500.0)
        return 1 / (1 + 10 ** ((ra - rh - (0 if neutral else MODEL["elo_home_adv"])) / 400))

    def knows(self, h, a) -> bool:
        return self.r is not None and h in self.r.att and a in self.r.att

    def predict(self, h, a, neutral, ch, ca, injury_pts=(0.0, 0.0)) -> dict:
        mh, ma = self.r.expected(h, a, neutral)
        fh = float(np.dot(self.coef, fatigue_vector(ch, ca)))
        fa = float(np.dot(self.coef, fatigue_vector(ca, ch)))
        mh, ma = mh + fh - injury_pts[0], ma + fa - injury_pts[1]
        sh, sa = self.r.sd(h, a)
        return {"mu_home": mh, "mu_away": ma, "margin": mh - ma, "total": mh + ma,
                "sd_margin": self.sd_margin, "sd_total": self.sd_total, "rho": self.rho,
                "fatigue_home": fh, "fatigue_away": fa,
                "sd_param_margin": math.hypot(sh * mh, sa * ma), "sd_param_total": math.hypot(sh * mh, sa * ma)}


def team_markets(p: dict, home: str, away: str, lines: dict | None = None) -> list[dict]:
    """Ganador, hándicap, total y totales por equipo (prórroga incluida)."""
    out = []

    m, sm, t, st = p["margin"], p["sd_margin"], p["total"], p["sd_total"]
    um, ut = 1.645 * p["sd_param_margin"], 1.645 * p["sd_param_total"]

    def sel(market, selection, line, prob, label, lo=None, hi=None):
        c = lambda x: min(max(x, 1e-4), 1 - 1e-4)
        lo, hi = (prob, prob) if lo is None else (min(lo, hi), max(lo, hi))
        out.append({"market": market, "selection": selection, "line": float(line), "prob_raw": c(prob),
                    "prob_low": c(lo), "prob_high": c(hi), "label": label, "player_id": "", "model": "team_normal"})
    ph = phi(m / sm)
    lo_h, hi_h = phi((m - um) / sm), phi((m + um) / sm)
    sel("ML", "H", 0, ph, f"Gana {home} (prórroga incluida)", lo_h, hi_h)
    sel("ML", "A", 0, 1 - ph, f"Gana {away} (prórroga incluida)", 1 - hi_h, 1 - lo_h)
    spread = (lines or {}).get("spread_home")
    for L in sorted({round(-m * 2) / 2 + d for d in (-6, -3, 0, 3, 6)} | ({spread} if spread is not None else set())):
        if L is None or float(L).is_integer():
            continue
        pc = phi((m + L) / sm)             # local cubre L (L negativo = local favorito)
        lo, hi = phi((m - um + L) / sm), phi((m + um + L) / sm)
        sel("SPREAD", "H", L, pc, f"{home} {L:+.1f} (hándicap, prórroga incluida)", lo, hi)
        sel("SPREAD", "A", -L, 1 - pc, f"{away} {-L:+.1f} (hándicap, prórroga incluida)", 1 - hi, 1 - lo)
    tl = (lines or {}).get("total_line")
    for L in sorted({round(t) + d + 0.5 for d in (-15, -10, -6, -3, 0, 3, 6, 10, 15)} | ({tl} if tl else set())):
        po = 1 - phi((L - t) / st)
        lo, hi = 1 - phi((L - t + ut) / st), 1 - phi((L - t - ut) / st)
        sel("TOTAL", "over", L, po, f"Más de {L:.1f} puntos en el partido (prórroga incluida)", lo, hi)
        sel("TOTAL", "under", L, 1 - po, f"Menos de {L:.1f} puntos en el partido (prórroga incluida)", 1 - hi, 1 - lo)
    sdt = st / math.sqrt(2 * (1 + p["rho"]))
    for side, mu, team in (("home", p["mu_home"], home), ("away", p["mu_away"], away)):
        for d in (-8, -4, 0, 4, 8):
            L = round(mu) + d + 0.5
            po = 1 - phi((L - mu) / sdt)
            lo, hi = 1 - phi((L - mu + ut / 2) / sdt), 1 - phi((L - mu - ut / 2) / sdt)
            sel("TEAM_TOTAL", f"{side}_over", L, po, f"{team}: más de {L:.1f} puntos (prórroga incluida)", lo, hi)
            sel("TEAM_TOTAL", f"{side}_under", L, 1 - po, f"{team}: menos de {L:.1f} puntos (prórroga incluida)", 1 - hi, 1 - lo)
    return out


# ---------------------------------------------------------------------------
# Modelo de jugadores
# ---------------------------------------------------------------------------
class PlayerBook:
    """Historial por jugador (sólo partidos ANTERIORES a la fecha de corte se usan al proyectar)."""

    def __init__(self, player_rows: list[dict], games: dict):
        self.games = games                      # event_id -> game dict
        self.hist = defaultdict(list)           # player_id -> [row ordenadas por fecha]
        self.team_games = defaultdict(list)     # team_id -> [(date, event_id)]
        for r in player_rows:
            g = games.get(r["event_id"])
            if not g:
                continue
            r = dict(r)
            r["date_utc"] = g["date_utc"]
            r["b2b"] = 0
            self.hist[r["player_id"]].append(r)
        for pid in self.hist:
            self.hist[pid].sort(key=lambda x: x["date_utc"])
        self.league = self._league_rates()

    def _league_rates(self) -> dict:
        tot = defaultdict(float)
        mins = 0.0
        for rows in self.hist.values():
            for r in rows[-200:]:
                if r["minutes"] and r["minutes"] > 0:
                    mins += r["minutes"]
                    for s in STATS:
                        tot[s] += r[s] or 0
        return {s: tot[s] / mins for s in STATS} if mins else {s: 0.0 for s in STATS}

    def before(self, pid: str, when: str) -> list[dict]:
        rows = self.hist.get(pid, [])
        lo, hi = 0, len(rows)
        while lo < hi:
            mid = (lo + hi) // 2
            if rows[mid]["date_utc"] < when:
                lo = mid + 1
            else:
                hi = mid
        return rows[:lo]

    def profile(self, pid: str, when: str, team_id: str | None = None) -> dict | None:
        rows = self.before(pid, when)
        if team_id:
            rows = [r for r in rows if r["team_id"] == team_id] or rows
        played = [r for r in rows if r["minutes"] and r["minutes"] > 0]
        if len(played) < 3:
            return None
        recent = played[-30:]
        hl = MODEL["player_half_life_games"]
        w = np.array([0.5 ** ((len(recent) - 1 - i) / hl) for i in range(len(recent))])
        mins = np.array([r["minutes"] for r in recent], float)
        m_mean = float(np.average(mins, weights=w))
        m_sd = float(math.sqrt(np.average((mins - m_mean) ** 2, weights=w)))
        tot_min = float(np.sum(w * mins))
        prior_min = MODEL["player_prior_games"] * max(m_mean, 1)
        rates = {}
        for s in STATS:
            v = np.array([r[s] or 0 for r in recent], float)
            rates[s] = (float(np.sum(w * v)) + prior_min * self.league[s]) / (tot_min + prior_min)
        last10 = rows[-10:]
        rest_dnps = sum(1 for r in rows[-82:] if r["dnp"] and r["dnp_reason"] and "rest" in r["dnp_reason"].lower())
        pf36 = 36 * sum(r["pf"] or 0 for r in recent) / max(sum(mins), 1)
        return {"n": len(played), "minutes": m_mean, "minutes_sd": m_sd, "starter_rate": float(np.mean([r["starter"] for r in recent[-10:]])),
                "rates": rates, "pf36": pf36, "ejections": sum(r["ejected"] or 0 for r in rows[-82:]),
                "rest_dnps": rest_dnps, "dnp_last10": sum(r["dnp"] for r in last10),
                "avg": {s: float(np.mean([r[s] or 0 for r in recent[-10:]])) for s in STATS},
                "last5_pts": [int(r["pts"] or 0) for r in played[-5:]], "team_id": rows[-1]["team_id"],
                "name": rows[-1]["name"], "pos": rows[-1]["pos"], "last": self._last_game(rows[-1], m_mean)}

    def _last_game(self, r: dict, avg_min: float) -> dict:
        """Novedades del último partido del jugador: minutos, expulsión, faltas, prórroga, no jugó."""
        g = self.games.get(r["event_id"], {})
        margin = abs((g.get("home_pts") or 0) - (g.get("away_pts") or 0))
        return {"date_utc": r["date_utc"], "event_id": r["event_id"], "minutes": r["minutes"] or 0.0,
                "avg_minutes": avg_min, "dnp": bool(r["dnp"]), "dnp_reason": r["dnp_reason"], "ejected": bool(r["ejected"]),
                "pf": r["pf"] or 0, "overtime": (g.get("periods") or 4) > 4, "blowout": margin >= 20,
                "pts": r["pts"], "opponent_game": f"{g.get('away', '')} @ {g.get('home', '')}"}


def nb_sample(rng, mean, k, size):
    mean = np.maximum(mean, 1e-6)
    if k > 1e4:
        return rng.poisson(mean)
    p = k / (k + mean)
    return rng.negative_binomial(k, p)


DISPERSION = {"pts": 8.0, "fg3m": 6.0, "reb": 10.0, "ast": 8.0, "stl": 20.0, "blk": 6.0, "tov": 20.0}


def simulate_player(prof: dict, ctx: dict, rng, n: int) -> dict:
    """ctx: minutes_adj (min), usage_boost (fracción), pace (factor), opp_def (factor puntos),
    blowout_sd (min), p_ot (prob. de prórroga). Devuelve muestras por estadística."""
    m = max(prof["minutes"] + ctx.get("minutes_adj", 0.0), 0)
    sd = math.hypot(prof["minutes_sd"] * 0.8, ctx.get("blowout_sd", 0.0)) + 1.0
    sd += 1.5 * max(prof["pf36"] - 4.0, 0)                  # propenso a problemas de faltas
    m *= ctx.get("min_mult", 1.0)
    mins = np.clip(rng.normal(m, sd, n), 0, 48)
    ot = rng.random(n) < ctx.get("p_ot", 0.06)
    mins = mins + ot * 5 * (m / 48)                          # prórroga incluida
    out = {"minutes": mins}
    for s in STATS:
        rate = prof["rates"][s] * ctx.get("pace", 1.0) * ctx.get("rate_mult", 1.0)
        if s in ("pts", "fg3m"):
            rate *= ctx.get("opp_def", 1.0) * (1 + ctx.get("usage_boost", 0.0))
        elif s == "ast":
            rate *= 1 + 0.5 * ctx.get("usage_boost", 0.0)
        out[s] = nb_sample(rng, rate * mins, DISPERSION[s], n)
    out["pra"] = out["pts"] + out["reb"] + out["ast"]
    out["pr"] = out["pts"] + out["reb"]
    out["pa"] = out["pts"] + out["ast"]
    out["ra"] = out["reb"] + out["ast"]
    out["stocks"] = out["stl"] + out["blk"]
    cats = np.stack([out["pts"] >= 10, out["reb"] >= 10, out["ast"] >= 10, out["stl"] >= 10, out["blk"] >= 10]).sum(0)
    out["dd"] = cats >= 2
    return out


PLAYER_MARKETS = {"pts": ("P_PTS", "puntos"), "fg3m": ("P_3PM", "triples"), "reb": ("P_REB", "rebotes"),
                  "ast": ("P_AST", "asistencias"), "pra": ("P_PRA", "puntos + rebotes + asistencias"),
                  "pr": ("P_PR", "puntos + rebotes"), "pa": ("P_PA", "puntos + asistencias"),
                  "ra": ("P_RA", "rebotes + asistencias"), "stocks": ("P_STOCKS", "robos + tapones")}


def player_markets(sim: dict, pid: str, name: str, lines_per_stat: int = 7, sim_lo: dict | None = None,
                   sim_hi: dict | None = None) -> list[dict]:
    """sim_lo / sim_hi: simulaciones con tasas y minutos pesimistas/optimistas (incertidumbre del modelo)."""
    out = []
    c = lambda v: min(max(v, 1e-4), 1 - 1e-4)
    for stat, (mk, label) in PLAYER_MARKETS.items():
        x = sim[stat]
        mu = float(np.mean(x))
        if mu < 0.6:
            continue
        cand = sorted({math.floor(q) + 0.5 for q in np.quantile(x, np.linspace(0.15, 0.85, lines_per_stat))})
        for L in cand:
            if L < 0.5:
                continue
            po = float(np.mean(x > L))
            lo = float(np.mean(sim_lo[stat] > L)) if sim_lo else po
            hi = float(np.mean(sim_hi[stat] > L)) if sim_hi else po
            lo, hi = min(lo, hi, po), max(lo, hi, po)
            out.append({"market": mk, "selection": "over", "line": L, "prob_raw": c(po), "prob_low": c(lo), "prob_high": c(hi),
                        "label": f"{name}: más de {L:g} {label} (prórroga incluida)", "player_id": pid, "model": f"player_{stat}",
                        "player": name, "mean": mu})
            out.append({"market": mk, "selection": "under", "line": L, "prob_raw": c(1 - po), "prob_low": c(1 - hi),
                        "prob_high": c(1 - lo), "label": f"{name}: menos de {L:g} {label} (prórroga incluida)", "player_id": pid,
                        "model": f"player_{stat}", "player": name, "mean": mu})
    pdd = float(np.mean(sim["dd"]))
    if 0.05 < pdd < 0.95:
        lo = float(np.mean(sim_lo["dd"])) if sim_lo else pdd
        hi = float(np.mean(sim_hi["dd"])) if sim_hi else pdd
        out.append({"market": "P_DD", "selection": "yes", "line": 0.0, "prob_raw": pdd, "prob_low": c(min(lo, pdd)),
                    "prob_high": c(max(hi, pdd)), "label": f"{name}: doble-doble (prórroga incluida)",
                    "player_id": pid, "model": "player_dd", "player": name, "mean": pdd})
    return out


def settle_player(market: str, selection: str, line: float, row: dict) -> int | None:
    """row: estadísticas finales del jugador (prórroga incluida). None si no jugó (apuesta nula)."""
    if not row or row.get("dnp") or not row.get("minutes"):
        return None
    v = {"P_PTS": row["pts"], "P_3PM": row["fg3m"], "P_REB": row["reb"], "P_AST": row["ast"],
         "P_PRA": (row["pts"] or 0) + (row["reb"] or 0) + (row["ast"] or 0), "P_PR": (row["pts"] or 0) + (row["reb"] or 0),
         "P_PA": (row["pts"] or 0) + (row["ast"] or 0), "P_RA": (row["reb"] or 0) + (row["ast"] or 0),
         "P_STOCKS": (row["stl"] or 0) + (row["blk"] or 0)}
    if market == "P_DD":
        c = sum(1 for k in ("pts", "reb", "ast", "stl", "blk") if (row[k] or 0) >= 10)
        return int(c >= 2)
    val = v.get(market)
    if val is None:
        return None
    return int((val > line) == (selection == "over"))


def settle_team(market: str, selection: str, line: float, g: dict) -> int | None:
    hp, ap = g.get("home_pts"), g.get("away_pts")
    if hp is None or ap is None:
        return None
    if market == "ML":
        return int((hp > ap) == (selection == "H"))
    if market == "SPREAD":
        return int((hp + line > ap) if selection == "H" else (ap + line > hp))
    if market == "TOTAL":
        return int((hp + ap > line) == (selection == "over"))
    if market == "TEAM_TOTAL":
        v = hp if selection.startswith("home") else ap
        return int((v > line) == selection.endswith("over"))
    return None

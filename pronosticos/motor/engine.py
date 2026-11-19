"""Motor predictivo: ajusta los modelos especializados de un "pool" de competiciones y produce las
probabilidades (centrales + intervalo Monte Carlo) de cada partido."""
from __future__ import annotations

import json
from datetime import datetime

import numpy as np

from ..config import COMPETITIONS, MODEL_PARAMS_PATH
from . import montecarlo
from .markets import COUNT_STATS, count_markets, goal_markets
from .models import CountModel, Elo, GoalsModel

ROWS_SQL = """
SELECT m.event_id, m.league, m.date_utc, m.home_id, m.home_name, m.away_id, m.away_name,
       m.home_goals, m.away_goals, m.has_details, m.neutral,
       th.shots h_shots, th.sot h_sot, th.corners h_corners, th.saves h_saves,
       CASE WHEN th.yellow IS NULL THEN NULL ELSE th.yellow + COALESCE(th.red,0) END h_cards,
       ta.shots a_shots, ta.sot a_sot, ta.corners a_corners, ta.saves a_saves,
       CASE WHEN ta.yellow IS NULL THEN NULL ELSE ta.yellow + COALESCE(ta.red,0) END a_cards
FROM matches m
LEFT JOIN team_stats th ON th.event_id = m.event_id AND th.team_id = m.home_id
LEFT JOIN team_stats ta ON ta.event_id = m.event_id AND ta.team_id = m.away_id
WHERE m.league IN ({q}) AND m.status = 'final' AND m.home_goals IS NOT NULL
ORDER BY m.date_utc
"""

MODEL_OF = {"1X2": "goals_dixon_coles", "DC": "goals_dixon_coles", "OU_GOALS": "goals_dixon_coles",
            "BTTS": "goals_dixon_coles", "TEAM_GOALS": "goals_dixon_coles",
            "OU_CORNERS": "corners_negbin", "OU_CARDS": "cards_negbin", "TEAM_SHOTS": "shots_negbin",
            "TEAM_SOT": "sot_negbin", "GK_SAVES": "saves_negbin"}


def pool_of(competition: str) -> str:
    return COMPETITIONS.get(competition, {}).get("pool", competition)


def pool_leagues(pool: str) -> list[str]:
    if pool == "clubs":
        return [k for k, v in COMPETITIONS.items() if v["kind"] in ("league", "cup")]
    if pool == "national":
        return [k for k, v in COMPETITIONS.items() if v["pool"] == "national"]
    return [pool]


def load_rows(con, pool_or_league: str) -> list[dict]:
    lgs = pool_leagues(pool_or_league)
    return [dict(r) for r in con.execute(ROWS_SQL.format(q=",".join("?" * len(lgs))), lgs).fetchall()]


def model_params() -> dict:
    try:
        return json.loads(MODEL_PARAMS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class LeagueEngine:
    def __init__(self, pool: str, rows: list[dict], cutoff: datetime, params: dict | None = None):
        self.pool = pool
        cut = cutoff.isoformat().replace("+00:00", "Z")
        self.rows = [r for r in rows if r["date_utc"] < cut]
        self.cutoff = cutoff
        p = params if params is not None else model_params()
        g = p.get("goals", {})
        self.goals = GoalsModel(g.get("half_life"), g.get("prior")).fit(self.rows, cutoff)
        self.counts = {}
        for s in COUNT_STATS:
            ps = p.get(s, {})
            self.counts[s] = CountModel(s, ps.get("half_life"), ps.get("prior")).fit(self.rows, cutoff)
        self.elo = Elo().fit(self.rows)

    # -- contexto para las reglas ---------------------------------------------
    def team_history(self, team: str, n: int = 10, venue: str | None = None) -> list[dict]:
        out = []
        for r in reversed(self.rows):
            if venue == "home" and r["home_id"] != team:
                continue
            if venue == "away" and r["away_id"] != team:
                continue
            if team in (r["home_id"], r["away_id"]):
                out.append(r)
                if len(out) >= n:
                    break
        return out

    def form(self, team: str, n: int = 10, venue: str | None = None) -> dict:
        hist = self.team_history(team, n, venue)
        gf = ga = pts = det = 0
        for r in hist:
            home = r["home_id"] == team
            f, a = (r["home_goals"], r["away_goals"]) if home else (r["away_goals"], r["home_goals"])
            gf, ga = gf + f, ga + a
            pts += 3 if f > a else (1 if f == a else 0)
            det += 1 if r["has_details"] else 0
        k = len(hist) or 1
        return {"n": len(hist), "gf": gf / k, "ga": ga / k, "ppg": pts / k, "detail_ratio": det / k}

    def residuals(self, team: str, n: int = 10) -> list[float]:
        """Diferencia de goles real menos la esperada por el modelo en los últimos n partidos."""
        out = []
        for r in self.team_history(team, n):
            lh, la = self.goals.r.expected(r["home_id"], r["away_id"], bool(r.get("neutral")))
            gd = (r["home_goals"] - r["away_goals"]) - (lh - la)
            out.append(gd if r["home_id"] == team else -gd)
        return out

    def h2h(self, h: str, a: str, n: int = 5) -> list[str]:
        out = []
        for r in reversed(self.rows):
            if {r["home_id"], r["away_id"]} == {h, a}:
                out.append(f'{r["date_utc"][:10]} {r["home_name"]} {r["home_goals"]}-{r["away_goals"]} {r["away_name"]}')
                if len(out) >= n:
                    break
        return out

    def knows(self, h: str, a: str) -> bool:
        return self.goals.r is not None and h in self.goals.r.att and a in self.goals.r.att

    # -- predicción -------------------------------------------------------------
    def predict(self, h: str, a: str, home_name: str, away_name: str, neutral: bool = False,
                adj: dict | None = None, monte_carlo: bool = True, seed: int = 7) -> dict:
        m, lh, la = self.goals.score_matrix(h, a, neutral, adj)
        sels = goal_markets(m, home_name, away_name)
        expected = {"goals": (lh, la)}
        rng = np.random.default_rng(seed)
        intervals = montecarlo.goal_uncertainty(self.goals, h, a, neutral, adj, home_name, away_name, rng) \
            if monte_carlo else {}
        for stat, cm in self.counts.items():
            if cm.r is None or h not in cm.r.att or a not in cm.r.att:
                continue
            dh, da, mh, ma = cm.dists(h, a, neutral, adj)
            expected[stat] = (mh, ma)
            sels += count_markets(stat, dh, da, home_name, away_name)
            if monte_carlo:
                intervals.update(montecarlo.count_uncertainty(cm, stat, h, a, neutral, adj,
                                                              home_name, away_name, rng))
        for s in sels:
            s["model"] = MODEL_OF.get(s["market"], "?")
            iv = intervals.get((s["market"], s["selection"], float(s["line"])))
            if iv:
                s["prob_mc"], s["prob_low"], s["prob_high"] = iv
        return {
            "selections": sels,
            "expected": expected,
            "elo": self.elo.probs(h, a, neutral),
            "n_hist": (self.goals.r.n_matches.get(h, 0), self.goals.r.n_matches.get(a, 0)),
            "scenarios": montecarlo.scenarios(m, rng) if monte_carlo else None,
        }

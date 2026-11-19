"""Análisis de un partido NBA: contexto (fatiga, lesiones, rotación), equipos y jugadores."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np

from .config import MODEL, SELECTION as S
from .models import (PlayerBook, Schedule, TeamModel, _dt, phi, player_markets, simulate_player,
                     team_markets)

OUT_STATUSES = {"out", "suspension", "suspended"}
DOUBT_STATUSES = {"doubtful"}
QUESTION_STATUSES = {"questionable", "day-to-day", "probable"}


def load_games(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM games ORDER BY date_utc")]


def load_player_rows(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM player_games")]


class NBAEngine:
    def __init__(self, con, cutoff: datetime, games=None, prows=None):
        self.con = con
        self.cutoff = cutoff
        self.games = games if games is not None else load_games(con)
        self.by_id = {g["event_id"]: g for g in self.games}
        self.sched = Schedule([g for g in self.games if g["status"] in ("final", "scheduled", "in")])
        self.team = TeamModel().fit([g for g in self.games if g["status"] == "final"], cutoff, self.sched)
        self.book = PlayerBook(prows if prows is not None else load_player_rows(con), self.by_id)
        self.team_def = {}
        if self.team.r:
            self.team_def = dict(self.team.r.dfn)

    # -- rotación de un equipo -------------------------------------------------
    def rotation(self, team_id: str, when: str, n_games: int = 10) -> list[str]:
        evs = [g["event_id"] for g in self.games if g["status"] == "final" and g["date_utc"] < when
               and team_id in (g["home_id"], g["away_id"])][-n_games:]
        seen = {}
        q = ",".join("?" * len(evs))
        if not evs:
            return []
        for r in self.con.execute(f"""SELECT player_id, AVG(minutes) m, COUNT(*) n FROM player_games
                                      WHERE team_id=? AND event_id IN ({q}) AND minutes > 0 GROUP BY player_id""",
                                  (team_id, *evs)):
            if r["m"] >= 8 and r["n"] >= 2:
                seen[r["player_id"]] = r["m"]
        return sorted(seen, key=lambda p: -seen[p])

    def analyze(self, g: dict, injuries: dict, known_out: set | None = None, lineups: dict | None = None,
                rng=None, n_sims: int | None = None, players: bool = True) -> dict | None:
        """injuries: parte oficial {player_id: {...}}. known_out: jugadores que se sabe no juegan (backtest)."""
        h, a = g["home_id"], g["away_id"]
        if not self.team.knows(h, a):
            return None
        when = _dt(g["date_utc"])
        ch = self.sched.context(h, when, True, g["home"])
        ca = self.sched.context(a, when, False, g["home"])
        rng = rng or np.random.default_rng(int(g["event_id"]) % 100000)
        n = n_sims or MODEL["mc_draws"]
        teams = {}
        injury_pts = []
        for tid, ctx in ((h, ch), (a, ca)):
            rot = self.rotation(tid, g["date_utc"])
            info = {"out": [], "doubt": [], "question": [], "available": [], "profiles": {}, "vacated": 0.0,
                    "vacated_pts": 0.0}
            for pid in rot:
                prof = self.book.profile(pid, g["date_utc"], tid)
                if not prof:
                    continue
                inj = injuries.get(str(pid)) or injuries.get("name:" + (prof["name"] or "").lower()) or {}
                st = (inj.get("status") or "").lower()
                if (known_out and pid in known_out) or st in OUT_STATUSES:
                    info["out"].append(pid)
                    info["vacated"] += prof["minutes"]
                    info["vacated_pts"] += prof["avg"]["pts"]
                elif st in DOUBT_STATUSES:
                    info["doubt"].append(pid)
                    info["vacated"] += 0.75 * prof["minutes"]
                    info["vacated_pts"] += 0.75 * prof["avg"]["pts"]
                else:
                    if st in QUESTION_STATUSES:
                        info["question"].append(pid)
                    info["available"].append(pid)
                info["profiles"][pid] = {**prof, "injury": inj}
            if lineups and tid in lineups:          # alineación confirmada: quien no es titular habitual y no figura, fuera
                info["confirmed_starters"] = lineups[tid]
            teams[tid] = info
            injury_pts.append(min(0.25 * info["vacated_pts"], 12.0))
        tp = self.team.predict(h, a, bool(g["neutral"]), ch, ca, tuple(injury_pts))
        res = {"game": g, "ctx_home": ch, "ctx_away": ca, "team": tp, "teams": teams,
               "elo_home": self.team.elo_prob(h, a, bool(g["neutral"])),
               "selections": team_markets(tp, g["home"], g["away"], None)}
        if not players:
            return res
        sm = tp["sd_margin"]
        exp_abs_margin = sm * math.sqrt(2 / math.pi) * math.exp(-tp["margin"] ** 2 / (2 * sm * sm)) + \
            abs(tp["margin"]) * (1 - 2 * phi(-abs(tp["margin"]) / sm))
        p_ot = 0.9 * (phi(2.5 / sm - tp["margin"] / sm) - phi(-2.5 / sm - tp["margin"] / sm)) * 0.45
        pace = tp["total"] / max(self.team.league_total, 1)
        res["players"] = {}
        for tid, opp, ctx in ((h, a, ch), (a, h, ca)):
            info = teams[tid]
            avail = [p for p in info["available"] if p in info["profiles"]]
            tot_min = sum(info["profiles"][p]["minutes"] for p in avail) or 1
            for pid in avail:
                prof = info["profiles"][pid]
                share = prof["minutes"] / tot_min
                extra = min(info["vacated"] * share, 8.0)
                usage = 0.35 * info["vacated_pts"] / max(sum(info["profiles"][p]["avg"]["pts"] for p in avail), 1) \
                    if info["vacated_pts"] else 0.0
                starter = prof["starter_rate"] >= 0.5
                adj = extra - (1.0 if ctx["b2b"] and prof["minutes"] >= 30 else 0.0)
                if starter:
                    adj -= 0.15 * max(exp_abs_margin - 10, 0)
                pctx = {"minutes_adj": adj, "usage_boost": min(usage, 0.25), "pace": pace,
                        "opp_def": self.team_def.get(opp, 1.0), "blowout_sd": 0.25 * exp_abs_margin if starter else 0.0,
                        "p_ot": max(min(p_ot, 0.12), 0.02)}
                sim = simulate_player(prof, pctx, rng, n)
                # incertidumbre del modelo: tasas ±1.645·error estándar y minutos ±1.645·error de proyección
                cv = 0.5 / math.sqrt(max(min(prof["n"], 40), 3)) + 0.04
                cvm = min(0.6 * prof["minutes_sd"] / max(prof["minutes"], 1) / math.sqrt(max(min(prof["n"], 10), 3)) + 0.03, 0.2)
                sims = n // 2
                sim_lo = simulate_player(prof, {**pctx, "rate_mult": 1 - 1.645 * cv, "min_mult": 1 - 1.645 * cvm}, rng, sims)
                sim_hi = simulate_player(prof, {**pctx, "rate_mult": 1 + 1.645 * cv, "min_mult": 1 + 1.645 * cvm}, rng, sims)
                proj_min = float(np.mean(sim["minutes"]))
                res["players"][pid] = {"profile": prof, "ctx": pctx, "team_id": tid, "proj_minutes": proj_min,
                                       "sim_means": {k: float(np.mean(v)) for k, v in sim.items() if k != "dd"},
                                       "p_dd": float(np.mean(sim["dd"])), "markets": player_markets(sim, pid, prof["name"], sim_lo=sim_lo, sim_hi=sim_hi)}
        return res

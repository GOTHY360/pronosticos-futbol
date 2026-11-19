"""Backtest walk-forward NBA (fuera de muestra): equipos y props de jugadores.

Cada semana se reajusta el modelo de equipos sólo con partidos anteriores; los perfiles de
jugadores usan sólo partidos anteriores a cada encuentro. Los jugadores que no jugaron se tratan
como "fuera" (equivale a conocer el parte de lesiones). Las apuestas de jugadores que no juegan
son nulas, así que sólo se evalúan jugadores que jugaron.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from .db import connect
from .engine import NBAEngine, load_games, load_player_rows
from .models import settle_player, settle_team

TEST_START = "2025-11-15"


def run(test_start: str = TEST_START, log=print, n_sims: int = 1500):
    con = connect()
    games = load_games(con)
    prows = load_player_rows(con)
    by_game = defaultdict(dict)
    for r in prows:
        by_game[r["event_id"]][r["player_id"]] = r
    test = [g for g in games if g["status"] == "final" and g["has_details"] and g["date_utc"] >= test_start]
    con.execute("DELETE FROM backtest")
    start = datetime.fromisoformat(test_start).replace(tzinfo=timezone.utc)
    end = datetime.now(timezone.utc)
    week, n_games, n_rows = start, 0, 0
    rng = np.random.default_rng(11)
    eng = None
    while week < end:
        nxt = week + timedelta(days=7)
        a, b = week.isoformat().replace("+00:00", "Z"), nxt.isoformat().replace("+00:00", "Z")
        batch = [g for g in test if a <= g["date_utc"] < b]
        if batch:
            eng = NBAEngine(con, week, games, prows) if eng is None else _refit(eng, week)
            for g in batch:
                box = by_game.get(g["event_id"], {})
                known_out = {pid for pid, r in box.items() if r["dnp"] or not r["minutes"]}
                res = eng.analyze(g, {}, known_out=known_out, rng=rng, n_sims=n_sims)
                if not res:
                    continue
                rows = []
                for s in res["selections"]:
                    o = settle_team(s["market"], s["selection"], s["line"], g)
                    if o is not None:
                        rows.append((g["event_id"], "nba", g["date_utc"], s["market"], s["selection"], s["line"], "",
                                     s["prob_raw"], o))
                for pid, p in res["players"].items():
                    actual = box.get(pid)
                    if not actual or actual["dnp"] or not actual["minutes"] or p["proj_minutes"] < 18:
                        continue
                    for s in p["markets"]:
                        if not 0.25 <= s["prob_raw"] <= 0.88:
                            continue
                        o = settle_player(s["market"], s["selection"], s["line"], actual)
                        if o is not None:
                            rows.append((g["event_id"], "nba", g["date_utc"], s["market"], s["selection"], s["line"], pid,
                                         s["prob_raw"], o))
                con.executemany("""INSERT OR REPLACE INTO backtest(event_id, league, date_utc, market, selection, line,
                                   player_id, prob, outcome) VALUES (?,?,?,?,?,?,?,?,?)""", rows)
                n_games += 1
                n_rows += len(rows)
            con.commit()
            log(f"  semana {week.date()}: {len(batch)} partidos (acumulado {n_games} partidos, {n_rows} selecciones)")
        week = nxt
    return n_games, n_rows


def _refit(eng: NBAEngine, cutoff: datetime) -> NBAEngine:
    from .models import TeamModel
    eng.cutoff = cutoff
    eng.team = TeamModel().fit([g for g in eng.games if g["status"] == "final"], cutoff, eng.sched)
    eng.team_def = dict(eng.team.r.dfn) if eng.team.r else {}
    return eng


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run(log=lambda s: print(s, flush=True))

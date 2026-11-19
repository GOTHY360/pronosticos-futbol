"""Módulo de datos NBA: calendario, resultados, box scores (jugadores y equipos), cuotas, lesiones y noticias."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from pronosticos.datos.espn import _get
from pronosticos.datos.ingest import american_to_decimal, months_between

from .config import CACHE_DIR, ESPN_BASE, HISTORY_START


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _close(block, side):
    v = ((block or {}).get(side) or {}).get("close") or {}
    return v if isinstance(v, dict) else {}


def parse_odds(o: dict) -> dict | None:
    ml, ps, tot = o.get("moneyline") or {}, o.get("pointSpread") or {}, o.get("total") or {}
    if not (ml or ps or tot):
        if o.get("overUnder") is None:
            return None
        return {"provider": (o.get("provider") or {}).get("name"), "home_dec": american_to_decimal((o.get("homeTeamOdds") or {}).get("moneyLine")),
                "away_dec": american_to_decimal((o.get("awayTeamOdds") or {}).get("moneyLine")),
                "spread_home": -_num(o.get("spread")) if o.get("spread") is not None else None,
                "spread_home_dec": None, "spread_away_dec": None, "total_line": _num(o.get("overUnder")),
                "over_dec": american_to_decimal(o.get("overOdds")), "under_dec": american_to_decimal(o.get("underOdds"))}
    tl = (_close(tot, "over").get("line") or "").lstrip("ou")
    return {"provider": (o.get("provider") or {}).get("name"),
            "home_dec": american_to_decimal(_close(ml, "home").get("odds")),
            "away_dec": american_to_decimal(_close(ml, "away").get("odds")),
            "spread_home": _num(_close(ps, "home").get("line")),
            "spread_home_dec": american_to_decimal(_close(ps, "home").get("odds")),
            "spread_away_dec": american_to_decimal(_close(ps, "away").get("odds")),
            "total_line": _num(tl) if tl else _num(o.get("overUnder")),
            "over_dec": american_to_decimal(_close(tot, "over").get("odds")),
            "under_dec": american_to_decimal(_close(tot, "under").get("odds"))}


def upsert_events(con, events: list[dict]) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for e in events:
        c = e["competitions"][0]
        t = {x["homeAway"]: x for x in c["competitors"]}
        if "home" not in t or "away" not in t:
            continue
        st = c["status"]["type"]
        status = "final" if st.get("completed") else ("scheduled" if st.get("state") == "pre" else st.get("state"))
        if st.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED"):
            status = "postponed"
        fin = status == "final"
        periods = len(t["home"].get("linescores") or []) or None
        season = e.get("season") or {}
        con.execute("""INSERT INTO games(event_id, season, season_type, date_utc, status, home_id, home, away_id, away,
                       home_pts, away_pts, periods, neutral, venue) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(event_id) DO UPDATE SET date_utc=excluded.date_utc, status=excluded.status,
                       home_pts=excluded.home_pts, away_pts=excluded.away_pts, periods=excluded.periods""",
                    (e["id"], season.get("year"), season.get("type"), e["date"], status,
                     t["home"]["team"]["id"], t["home"]["team"]["displayName"], t["away"]["team"]["id"],
                     t["away"]["team"]["displayName"], int(t["home"]["score"]) if fin else None,
                     int(t["away"]["score"]) if fin else None, periods if fin else None,
                     1 if c.get("neutralSite") else 0, (c.get("venue") or {}).get("fullName")))
        odds = [o for o in (c.get("odds") or []) if isinstance(o, dict)]
        if odds and status == "scheduled":
            od = parse_odds(odds[0])
            if od and (od["home_dec"] or od["total_line"]):
                con.execute("""INSERT OR REPLACE INTO odds(event_id, taken_at, provider, home_dec, away_dec, spread_home,
                               spread_home_dec, spread_away_dec, total_line, over_dec, under_dec) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (e["id"], now, od["provider"], od["home_dec"], od["away_dec"], od["spread_home"],
                             od["spread_home_dec"], od["spread_away_dec"], od["total_line"], od["over_dec"], od["under_dec"]))
        n += 1
    con.commit()
    return n


def summary(event_id: str, use_cache: bool = True) -> dict:
    path = CACHE_DIR / f"{event_id}.json"
    if use_cache and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    d = _get(f"{ESPN_BASE}/summary?event={event_id}")
    st = ((d.get("header") or {}).get("competitions") or [{}])[0].get("status", {}).get("type", {})
    if st.get("completed"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({k: d.get(k) for k in ("boxscore", "header")}), encoding="utf-8")
    return d


def _split(v):
    try:
        a, b = str(v).split("-")
        return int(a), int(b)
    except (ValueError, TypeError):
        return None, None


def store_details(con, event_id: str, s: dict) -> bool:
    box = s.get("boxscore") or {}
    teams, players = box.get("teams") or [], box.get("players") or []
    if len(teams) != 2 or len(players) != 2:
        return False
    comp = s["header"]["competitions"][0]
    side = {c["team"]["id"]: (1 if c["homeAway"] == "home" else 0, c.get("score")) for c in comp["competitors"]}
    for t in teams:
        tid = t["team"]["id"]
        m = {x["name"]: x.get("displayValue") for x in t.get("statistics", [])}
        fgm, fga = _split(m.get("fieldGoalsMade-fieldGoalsAttempted"))
        f3m, f3a = _split(m.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
        ftm, fta = _split(m.get("freeThrowsMade-freeThrowsAttempted"))
        i = lambda k: int(_num(m.get(k)) or 0)
        oreb, tov = i("offensiveRebounds"), i("totalTurnovers") or i("turnovers")
        poss = (fga or 0) - oreb + tov + 0.44 * (fta or 0)
        h, score = side.get(tid, (None, None))
        con.execute("""INSERT OR REPLACE INTO team_games(event_id, team_id, is_home, pts, fgm, fga, fg3m, fg3a, ftm, fta,
                       oreb, dreb, reb, ast, stl, blk, tov, fouls, tech, flagrant, poss) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event_id, tid, h, int(score) if score not in (None, "") else None, fgm, fga, f3m, f3a, ftm, fta,
                     oreb, i("defensiveRebounds"), i("totalRebounds"), i("assists"), i("steals"), i("blocks"), tov,
                     i("fouls"), i("totalTechnicalFouls") or i("technicalFouls"), i("flagrantFouls"), poss))
    for p in players:
        tid = p["team"]["id"]
        st = (p.get("statistics") or [{}])[0]
        keys = st.get("keys") or []
        for a in st.get("athletes") or []:
            ath = a.get("athlete") or {}
            vals = dict(zip(keys, a.get("stats") or []))
            dnp = 1 if a.get("didNotPlay") or not vals else 0
            g = lambda k: _num(vals.get(k))
            f3m, f3a = _split(vals.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
            fgm, fga = _split(vals.get("fieldGoalsMade-fieldGoalsAttempted"))
            ftm, fta = _split(vals.get("freeThrowsMade-freeThrowsAttempted"))
            pm = vals.get("plusMinus")
            con.execute("""INSERT OR REPLACE INTO player_games(event_id, team_id, player_id, name, pos, starter, minutes, pts,
                           fg3m, fg3a, fga, fta, reb, ast, stl, blk, tov, pf, plus_minus, dnp, dnp_reason, ejected)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (event_id, tid, ath.get("id"), ath.get("displayName"), (ath.get("position") or {}).get("abbreviation"),
                         1 if a.get("starter") else 0, 0.0 if dnp else (g("minutes") or 0.0), g("points"), f3m, f3a, fga, fta,
                         g("rebounds"), g("assists"), g("steals"), g("blocks"), g("turnovers"), g("fouls"),
                         int(_num(str(pm).replace("+", "")) or 0) if pm not in (None, "--") else None,
                         dnp, a.get("reason") if dnp else None, 1 if a.get("ejected") else 0))
    con.execute("UPDATE games SET has_details=1 WHERE event_id=?", (event_id,))
    con.commit()
    return True


def sync_calendar(con, months=None, log=print):
    total = 0
    for mo in months or list(months_between(HISTORY_START, datetime.now(timezone.utc).date())):
        try:
            total += upsert_events(con, _get(f"{ESPN_BASE}/scoreboard?dates={mo}&limit=1000").get("events", []))
        except RuntimeError as e:
            log(f"  ! {mo}: {e}")
    log(f"  NBA: {total} partidos sincronizados")


def sync_day(con, day: str):
    """Partidos y cuotas de un día concreto (AAAAMMDD)."""
    return upsert_events(con, _get(f"{ESPN_BASE}/scoreboard?dates={day}").get("events", []))


def sync_details(con, limit=None, log=print):
    q = "SELECT event_id FROM games WHERE status='final' AND has_details=0 ORDER BY date_utc DESC"
    rows = con.execute(q + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    log(f"  box scores pendientes: {len(rows)}")
    ok = 0
    for k, r in enumerate(rows, 1):
        try:
            ok += store_details(con, r["event_id"], summary(r["event_id"]))
        except RuntimeError as e:
            log(f"  ! {r['event_id']}: {e}")
        if k % 200 == 0:
            log(f"    {k}/{len(rows)}")
    log(f"  box scores guardados: {ok}")


def sync_injuries(con, log=print) -> int:
    """Instantánea del parte oficial de lesiones de toda la liga."""
    d = _get(f"{ESPN_BASE}/injuries")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for team in d.get("injuries", []):
        for i in team.get("injuries", []):
            a = i.get("athlete") or {}
            det = i.get("details") or {}
            pid = a.get("id") or ((a.get("links") or [{}])[0].get("href", "").split("/id/")[-1].split("/")[0])
            con.execute("""INSERT OR REPLACE INTO injuries(taken_at, team, player_id, name, status, detail, return_date,
                           comment, report_date) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (now, team.get("displayName"), str(pid), a.get("displayName"), i.get("status"),
                         " ".join(x for x in (det.get("type"), det.get("detail")) if x), det.get("returnDate"),
                         i.get("shortComment"), i.get("date")))
            n += 1
    con.commit()
    log(f"  parte de lesiones: {n} jugadores")
    return n


def latest_injuries(con) -> dict:
    """{player_id: {...}} de la última instantánea; también indexado por nombre en minúsculas."""
    last = con.execute("SELECT MAX(taken_at) FROM injuries").fetchone()[0]
    out = {}
    if not last:
        return out
    for r in con.execute("SELECT * FROM injuries WHERE taken_at=?", (last,)):
        d = dict(r)
        out[str(r["player_id"])] = d
        out["name:" + (r["name"] or "").lower()] = d
    return out


def sync_news(con, log=print):
    try:
        for a in _get(f"{ESPN_BASE}/news?limit=50").get("articles", []):
            con.execute("INSERT OR IGNORE INTO news(news_id, published, headline, description) VALUES (?,?,?,?)",
                        (str(a.get("id") or a.get("headline")), a.get("published"), a.get("headline"), a.get("description")))
        con.commit()
    except RuntimeError as e:
        log(f"  ! noticias: {e}")


def lineups(event_id: str) -> dict:
    """Titulares confirmados (si ya se publicaron) {team_id: [player_id, ...]}."""
    s = _get(f"{ESPN_BASE}/summary?event={event_id}")
    out = {}
    for p in (s.get("boxscore") or {}).get("players") or []:
        st = (p.get("statistics") or [{}])[0]
        starters = [str((a.get("athlete") or {}).get("id")) for a in st.get("athletes") or [] if a.get("starter")]
        if len(starters) == 5:
            out[p["team"]["id"]] = starters
    return out

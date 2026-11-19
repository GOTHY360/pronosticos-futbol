"""Motor de datos: descarga calendario, resultados, estadísticas, alineaciones y cuotas."""
from datetime import date, datetime, timezone

from . import espn
from ..config import COMPETITIONS, HISTORY_START, LEAGUES


def american_to_decimal(ml):
    if ml is None:
        return None
    try:
        ml = float(ml)
    except (TypeError, ValueError):
        return None
    if ml == 0:
        return None
    return round(1 + ml / 100, 4) if ml > 0 else round(1 + 100 / abs(ml), 4)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _close(block, side):
    """Lee la cuota 'close' (actual) del formato nuevo de ESPN: {'home': {'close': {'odds': '-105'}}}."""
    v = ((block or {}).get(side) or {}).get("close") or {}
    return v.get("odds") if isinstance(v, dict) else None


def _parse_odds(o: dict) -> dict:
    if o.get("moneyline") or o.get("total"):          # formato de partidos programados
        ml, tot = o.get("moneyline") or {}, o.get("total") or {}
        line = ((tot.get("over") or {}).get("close") or {}).get("line") or ""
        return {
            "provider": (o.get("provider") or {}).get("name"),
            "home_dec": american_to_decimal(_close(ml, "home")),
            "away_dec": american_to_decimal(_close(ml, "away")),
            "draw_dec": american_to_decimal(_close(ml, "draw") or (o.get("drawOdds") or {}).get("moneyLine")),
            "total_line": _num(line.lstrip("ou")) if line else _num(o.get("overUnder")),
            "over_dec": american_to_decimal(_close(tot, "over")),
            "under_dec": american_to_decimal(_close(tot, "under")),
        }
    return {
        "provider": (o.get("provider") or {}).get("name"),
        "home_dec": american_to_decimal((o.get("homeTeamOdds") or {}).get("moneyLine")),
        "away_dec": american_to_decimal((o.get("awayTeamOdds") or {}).get("moneyLine")),
        "draw_dec": american_to_decimal((o.get("drawOdds") or {}).get("moneyLine")),
        "total_line": _num(o.get("overUnder")),
        "over_dec": american_to_decimal(o.get("overOdds")),
        "under_dec": american_to_decimal(o.get("underOdds")),
    }


def upsert_events(con, league: str, events: list[dict]) -> int:
    n = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for e in events:
        comp = e["competitions"][0]
        teams = {c["homeAway"]: c for c in comp["competitors"]}
        if "home" not in teams or "away" not in teams:
            continue
        st = comp["status"]["type"]
        status = "final" if st.get("completed") else ("scheduled" if st.get("state") == "pre" else st.get("state", "?"))
        if st.get("name") in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_ABANDONED"):
            status = "postponed"
        hg = int(teams["home"]["score"]) if status == "final" and teams["home"].get("score") not in (None, "") else None
        ag = int(teams["away"]["score"]) if status == "final" and teams["away"].get("score") not in (None, "") else None
        neutral = 1 if comp.get("neutralSite") else 0
        if COMPETITIONS.get(league, {}).get("kind") == "tournament":
            # torneo en sede fija: sólo el anfitrión juega "en casa"
            country = (((comp.get("venue") or {}).get("address") or {}).get("country") or "").lower()
            host = teams["home"]["team"]["displayName"].lower()
            neutral = 0 if country and (country in host or host in country) else 1
        con.execute("""
            INSERT INTO matches(event_id, league, date_utc, status, home_id, home_name, away_id, away_name,
                                home_goals, away_goals, venue, neutral)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET date_utc=excluded.date_utc, status=excluded.status,
                home_goals=excluded.home_goals, away_goals=excluded.away_goals, league=excluded.league,
                neutral=excluded.neutral""",
            (e["id"], league, e["date"], status,
             teams["home"]["team"]["id"], teams["home"]["team"]["displayName"],
             teams["away"]["team"]["id"], teams["away"]["team"]["displayName"],
             hg, ag, (comp.get("venue") or {}).get("fullName"), neutral))
        # Cuotas: sólo guardamos como 'pre' las capturadas antes del inicio.
        odds_list = [o for o in (comp.get("odds") or []) if isinstance(o, dict)]
        if odds_list and status == "scheduled":
            od = _parse_odds(odds_list[0])
            if od["home_dec"] or od["total_line"]:
                con.execute("""INSERT OR REPLACE INTO odds(event_id, taken_at, kind, provider, home_dec, draw_dec,
                               away_dec, total_line, over_dec, under_dec) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (e["id"], now, "pre", od["provider"], od["home_dec"], od["draw_dec"], od["away_dec"],
                             od["total_line"], od["over_dec"], od["under_dec"]))
        n += 1
    con.commit()
    return n


def _stat_map(stats):
    return {s["name"]: s.get("displayValue", s.get("value")) for s in stats}


def store_details(con, event_id: str, summ: dict) -> bool:
    """Guarda estadísticas de equipo y jugador de un partido terminado."""
    box = (summ.get("boxscore") or {}).get("teams") or []
    if len(box) != 2:
        return False
    comp = summ["header"]["competitions"][0]
    side = {c["team"]["id"]: (1 if c["homeAway"] == "home" else 0, c.get("score")) for c in comp["competitors"]}
    for t in box:
        tid = t["team"]["id"]
        m = _stat_map(t.get("statistics", []))
        is_home, score = side.get(tid, (None, None))
        def i(k):
            v = _num(m.get(k))
            return int(v) if v is not None else None
        con.execute("""INSERT OR REPLACE INTO team_stats(event_id, team_id, is_home, goals, shots, sot, corners,
                       yellow, red, saves, fouls, possession) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event_id, tid, is_home, int(score) if score not in (None, "") else None,
                     i("totalShots"), i("shotsOnTarget"), i("wonCorners"), i("yellowCards"), i("redCards"),
                     i("saves"), i("foulsCommitted"), _num(m.get("possessionPct"))))
    for r in summ.get("rosters") or []:
        tid = (r.get("team") or {}).get("id")
        for p in r.get("roster") or []:
            ath = p.get("athlete") or {}
            st = {s["name"]: s.get("value") for s in p.get("stats") or []}
            if not ath.get("id"):
                continue
            con.execute("""INSERT OR REPLACE INTO player_stats(event_id, team_id, player_id, name, position, starter,
                           sub_in, shots, sot, goals, saves, yellow, red) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (event_id, tid, ath["id"], ath.get("displayName"),
                         (p.get("position") or {}).get("abbreviation"), 1 if p.get("starter") else 0,
                         1 if p.get("subbedIn") else 0, st.get("totalShots"), st.get("shotsOnTarget"),
                         st.get("totalGoals"), st.get("saves"), st.get("yellowCards"), st.get("redCards")))
    con.execute("UPDATE matches SET has_details=1 WHERE event_id=?", (event_id,))
    con.commit()
    return True


def months_between(start: str, end: date):
    y, m = map(int, start.split("-"))
    while (y, m) <= (end.year, end.month):
        yield f"{y:04d}{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def sync_calendar(con, months: list[str] | None = None, leagues=None, log=print):
    today = datetime.now(timezone.utc).date()
    for lg in leagues or LEAGUES:
        total = 0
        start = COMPETITIONS.get(lg, {}).get("start", HISTORY_START)
        for mo in months or list(months_between(start, today)):
            try:
                total += upsert_events(con, lg, espn.scoreboard(lg, mo))
            except RuntimeError as e:
                log(f"  ! {lg} {mo}: {e}")
        log(f"  {lg}: {total} partidos sincronizados")


def sync_details(con, leagues=None, limit=None, log=print):
    q = "SELECT event_id, league FROM matches WHERE status='final' AND has_details=0"
    args = []
    if leagues:
        q += f" AND league IN ({','.join('?' * len(leagues))})"
        args += list(leagues)
    q += " ORDER BY date_utc DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = con.execute(q, args).fetchall()
    log(f"  detalles pendientes: {len(rows)}")
    ok = 0
    for k, r in enumerate(rows, 1):
        try:
            if store_details(con, r["event_id"], espn.summary(r["league"], r["event_id"])):
                ok += 1
        except RuntimeError as e:
            log(f"  ! {r['event_id']}: {e}")
        if k % 100 == 0:
            log(f"    {k}/{len(rows)}")
    log(f"  detalles guardados: {ok}")


def _store_news(con, league, event_id, articles):
    for a in articles or []:
        nid = str(a.get("id") or a.get("nowId") or a.get("headline"))
        con.execute("""INSERT OR IGNORE INTO news(news_id, league, event_id, published, headline, description, url)
                       VALUES (?,?,?,?,?,?,?)""",
                    (nid, league, event_id, a.get("published") or a.get("lastModified"), a.get("headline"),
                     a.get("description"), ((a.get("links") or {}).get("web") or {}).get("href")))


def sync_news(con, leagues, log=print):
    """Titulares recientes de cada competición (bajas, entrenadores, sanciones…)."""
    for lg in leagues:
        try:
            _store_news(con, lg, None, espn.news(lg))
        except RuntimeError as e:
            log(f"  ! noticias {lg}: {e}")
    con.commit()


def event_context(con, league: str, event_id: str) -> dict:
    """Estado actual de un partido próximo: alineación confirmada (regla 2), noticias y estado."""
    s = espn.summary(league, event_id, use_cache=False)
    lineups = {}
    for r in s.get("rosters") or []:
        tid = (r.get("team") or {}).get("id")
        starters = [((p.get("athlete") or {}).get("id"), (p.get("athlete") or {}).get("displayName"),
                     (p.get("position") or {}).get("abbreviation"))
                    for p in r.get("roster") or [] if p.get("starter")]
        if len(starters) >= 11:
            lineups[tid] = starters
    _store_news(con, league, event_id, (s.get("news") or {}).get("articles"))
    st = ((s.get("header") or {}).get("competitions") or [{}])[0].get("status", {}).get("type", {})
    con.commit()
    return {"lineups": lineups, "status": st.get("name"), "state": st.get("state")}


def lineup(league: str, event_id: str) -> dict:
    s = espn.summary(league, event_id, use_cache=False)
    out = {}
    for r in s.get("rosters") or []:
        tid = (r.get("team") or {}).get("id")
        starters = [((p.get("athlete") or {}).get("id"), (p.get("athlete") or {}).get("displayName"),
                     (p.get("position") or {}).get("abbreviation"))
                    for p in r.get("roster") or [] if p.get("starter")]
        if len(starters) >= 11:
            out[tid] = starters
    return out

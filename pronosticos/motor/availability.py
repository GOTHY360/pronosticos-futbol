"""Disponibilidad de jugadores: titulares habituales, importancia, suspensiones y ajuste por bajas.

Se usa en dos momentos:
- Pronóstico preliminar (la noche anterior): sólo suspensiones probables (roja en el último partido).
- Pronóstico actualizado (alineación confirmada): todas las ausencias de titulares habituales.
"""
from __future__ import annotations

DEFENSIVE = {"CD-L", "CD-R", "CD", "LB", "RB", "SW", "D", "LWB", "RWB"}


def _recent_events(con, team_id: str, before: str, n: int) -> list[str]:
    return [r[0] for r in con.execute(
        """SELECT event_id FROM matches WHERE (home_id=? OR away_id=?) AND status='final' AND has_details=1
           AND date_utc < ? ORDER BY date_utc DESC LIMIT ?""", (team_id, team_id, before, n))]


def team_profile(con, team_id: str, before: str, n: int = 10) -> dict:
    """Titulares habituales y peso ofensivo de cada jugador en los últimos n partidos."""
    evs = _recent_events(con, team_id, before, n)
    if not evs:
        return {"n": 0, "players": {}, "regulars": [], "last_event": None}
    q = ",".join("?" * len(evs))
    rows = con.execute(f"""SELECT player_id, name, position, starter, COALESCE(shots,0) shots,
                              COALESCE(goals,0) goals, COALESCE(red,0) red, event_id
                           FROM player_stats WHERE team_id=? AND event_id IN ({q})""", (team_id, *evs)).fetchall()
    players, tot_contrib = {}, 0.0
    for r in rows:
        p = players.setdefault(r["player_id"], {"name": r["name"], "pos": r["position"], "starts": 0,
                                                "contrib": 0.0, "gk": False, "def": False})
        p["starts"] += r["starter"]
        if r["starter"]:
            p["pos"] = r["position"]
        c = r["shots"] + 3 * r["goals"]
        p["contrib"] += c
        tot_contrib += c
        p["gk"] |= (r["position"] == "G" and bool(r["starter"]))
        p["def"] |= (r["position"] in DEFENSIVE and bool(r["starter"]))
    for p in players.values():
        p["share"] = p["contrib"] / tot_contrib if tot_contrib else 0.0
    regulars = [pid for pid, p in sorted(players.items(), key=lambda x: -x[1]["starts"])
                if p["starts"] >= max(0.6 * len(evs), 1)][:11]
    last = evs[0]
    suspended = [r["player_id"] for r in rows if r["event_id"] == last and r["red"]]
    return {"n": len(evs), "players": players, "regulars": regulars, "last_event": last,
            "likely_suspended": [pid for pid in suspended if pid in regulars]}


def adjustment(profile: dict, missing: list[str]) -> dict:
    """Multiplicadores de ataque/defensa por ausencias de titulares habituales."""
    pl = profile["players"]
    share = sum(pl[m]["share"] for m in missing if m in pl)
    att = max(0.75, 1 - 0.55 * share)
    n_def = sum(1 for m in missing if m in pl and pl[m]["def"])
    gk = any(pl[m]["gk"] for m in missing if m in pl)
    dfn = min(1.18, 1 + 0.03 * n_def + (0.06 if gk else 0.0))
    return {"att": att, "def": dfn, "missing": [pl[m]["name"] for m in missing if m in pl],
            "attack_share_missing": share, "n_def": n_def, "gk": gk}


def preliminary_adjustments(con, h: str, a: str, before: str, injuries: dict | None = None) -> tuple[dict, dict]:
    """Ajustes de la noche anterior: suspensiones probables (roja en el último partido) y, si hay
    API-Football, lesionados y sancionados confirmados. injuries: {team_id: [{"player","kind","reason"}]}.
    Devuelve (adj, info)."""
    from ..datos.apifootball import player_match
    adj, info = {}, {}
    for t in (h, a):
        prof = team_profile(con, t, before)
        sus = list(prof.get("likely_suspended", []))
        out_ids, doubt, unmatched = [], [], []
        cands = {pid: p["name"] for pid, p in prof["players"].items()}
        for inj in (injuries or {}).get(t, []):
            pid = player_match(inj["player"], cands)
            if not pid:
                unmatched.append(inj["player"])
                continue
            if (inj.get("kind") or "").lower().startswith("missing"):
                out_ids.append(pid)
            else:
                doubt.append(pid)
        missing = sorted(set(sus) | {p for p in out_ids if p in prof["regulars"]})
        info[t] = {"profile_n": prof["n"], "suspended": [prof["players"][s]["name"] for s in sus],
                   "injured_regulars": [prof["players"][p]["name"] for p in out_ids if p in prof["regulars"]],
                   "injured_others": [prof["players"][p]["name"] for p in out_ids if p not in prof["regulars"]],
                   "doubtful_regulars": [prof["players"][p]["name"] for p in doubt if p in prof["regulars"]],
                   "injuries_source": bool(injuries), "unmatched": unmatched}
        if missing:
            a_ = adjustment(prof, missing)
            adj[t] = {"att": a_["att"], "def": a_["def"]}
            info[t]["n_out_regulars"] = len(missing)
            info[t]["attack_share_missing"] = round(a_["attack_share_missing"], 3)
    return adj, info


def lineup_adjustments(con, h: str, a: str, before: str, lineups: dict) -> tuple[dict, dict]:
    """Ajustes con la alineación oficial: compara los titulares confirmados con los habituales."""
    adj, info = {}, {}
    for t in (h, a):
        prof = team_profile(con, t, before)
        starters = {p[0] for p in lineups.get(t, [])}
        if not starters:
            info[t] = {"confirmed": False}
            continue
        missing = [r for r in prof["regulars"] if r not in starters]
        a_ = adjustment(prof, missing)
        adj[t] = {"att": a_["att"], "def": a_["def"]}
        info[t] = {"confirmed": True, "missing": a_["missing"], "n_missing": len(missing),
                   "attack_share_missing": round(a_["attack_share_missing"], 3), "gk_missing": a_["gk"]}
    return adj, info

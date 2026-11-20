"""Filtro de calidad y detector de riesgos NBA.

Riesgos específicos del baloncesto que bloquean o advierten:
- jugador en duda (questionable / day-to-day / doubtful): sin props de ese jugador
- minutos volátiles o proyección < 20 min
- gestión de carga: noche consecutiva y el jugador ya descansó en noches consecutivas
- riesgo de paliza (hándicap grande): sin "más de" para titulares (minutos basura)
- propenso a problemas de faltas o expulsiones recientes: advertencia en los "más de"
- muchas bajas en el equipo: rol incierto
- contradicción entre el modelo, Elo y el mercado; movimientos bruscos de cuotas
"""
from __future__ import annotations

from .config import SELECTION as S

TEAM_MARKETS = {"ML", "SPREAD", "TOTAL", "TEAM_TOTAL"}


def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def devig2(a, b):
    if not a or not b:
        return None
    ia, ib = 1 / a, 1 / b
    s = ia + ib
    return (ia / s, ib / s, s) if 1.0 <= s <= 1.15 else None


def market_ref(sel: dict, od: dict | None):
    """(prob. mercado sin margen, cuota real, margen) para ganador / hándicap / total a la línea del mercado."""
    if not od:
        return None, None, None
    m, s, L = sel["market"], sel["selection"], sel["line"]
    if m == "ML":
        x = devig2(od.get("home_dec"), od.get("away_dec"))
        if x:
            return (x[0] if s == "H" else x[1]), od["home_dec" if s == "H" else "away_dec"], x[2] - 1
    if m == "SPREAD" and od.get("spread_home") is not None:
        sh = od["spread_home"]
        if (s == "H" and abs(L - sh) < 1e-9) or (s == "A" and abs(L + sh) < 1e-9):
            x = devig2(od.get("spread_home_dec"), od.get("spread_away_dec"))
            if x:
                return (x[0] if s == "H" else x[1]), od["spread_home_dec" if s == "H" else "spread_away_dec"], x[2] - 1
    if m == "TOTAL" and od.get("total_line") is not None and abs(L - od["total_line"]) < 1e-9:
        x = devig2(od.get("over_dec"), od.get("under_dec"))
        if x:
            return (x[0] if s == "over" else x[1]), od["over_dec" if s == "over" else "under_dec"], x[2] - 1
    return None, None, None


def game_flags(res: dict, odds_move: float | None) -> list[dict]:
    flags = []
    add = lambda c, sev, msg: flags.append({"code": c, "severity": sev, "message": msg})
    g = res["game"]
    for tid, name, ctx in ((g["home_id"], g["home"], res["ctx_home"]), (g["away_id"], g["away"], res["ctx_away"])):
        info = res["teams"][tid]
        if ctx["b2b"]:
            add("b2b", "info", f"{name} juega en noches consecutivas" + (" (3 partidos en 4 noches)" if ctx["three_in_four"] else "") + ".")
        if ctx["road_trip"] >= 4:
            add("gira", "info", f"{name} lleva {ctx['road_trip']} partidos seguidos fuera de casa.")
        if ctx["altitude"]:
            add("altitud", "info", f"{name} juega en altitud (Denver/Utah).")
        outs = [info["profiles"][p]["name"] for p in info["out"] if p in info["profiles"]]
        if len(outs) >= 3:
            add("bajas", "advertencia", f"{name}: {len(outs)} jugadores de rotación fuera ({', '.join(outs[:4])}): rol de los demás incierto.")
        q = [info["profiles"][p]["name"] for p in info["question"] + info["doubt"] if p in info["profiles"]]
        if len(q) >= 2:
            add("dudas", "advertencia", f"{name}: varios jugadores en duda ({', '.join(q[:4])}).")
    if odds_move and odds_move > 0.08:
        add("cuotas", "advertencia" if odds_move <= 0.16 else "bloqueo", f"Movimiento brusco de cuotas ({odds_move:.0%}).")
    if g["status"] == "postponed":
        add("estado", "bloqueo", "Partido aplazado.")
    return flags


def evaluate(res: dict, calibrator, market_ok: dict, odds: dict | None, flags: list[dict], mode: str = "picks") -> list[dict]:
    g = res["game"]
    if any(f["severity"] == "bloqueo" for f in flags):
        return []
    n_warn = sum(f["severity"] == "advertencia" for f in flags)
    tp = res["team"]
    ph = next(s["prob_raw"] for s in res["selections"] if s["market"] == "ML" and s["selection"] == "H")
    elo_gap = abs(ph - res["elo_home"])
    spread_abs = abs((odds or {}).get("spread_home") or tp["margin"])
    e, omin, omax = S["min_edge"], S["odds_min"], S["odds_max"]
    sels = [(s, None) for s in res["selections"]]
    for pid, p in res["players"].items():
        sels += [(s, p) for s in p["markets"]]
    out = []
    for sel, pl in sels:
        m = sel["market"]
        if m in S["excluded_markets"]:
            continue
        if pl is not None and sel.get("mean", 99) < S["min_player_mean"].get(m, 0):
            continue                       # línea trivial: el jugador casi no produce esa estadística
        p, b = calibrator.calibrate(m, sel["prob_raw"])
        fair = 1 / p
        min_odds = fair * (1 + e)
        mp, price, margin = market_ref(sel, odds) if m in TEAM_MARKETS else (None, None, None)
        if mode == "picks":
            if not ((omin <= price <= omax) if price else (omin <= min_odds <= omax)):
                continue
        elif p < S["combo_min_prob"] or p > 1 / S["min_fair_odds"]:
            continue
        shift = p - sel["prob_raw"]
        p_low = min(max(sel.get("prob_low", sel["prob_raw"]) + shift, 0), 1)
        p_high = min(max(sel.get("prob_high", sel["prob_raw"]) + shift, 0), 1)
        width = p_high - p_low
        checks, block, warn = [], False, n_warn
        cal_ok = bool(market_ok.get(m)) and b is not None and b["n"] >= 30 and b["freq"] >= b["mean_p"] - S["max_calibration_gap"]
        checks.append(_check("Modelo calibrado", "ok" if cal_ok else "fallo",
                             f"tramo {b['lo']:.0%}–{b['hi']:.0%}: real {b['freq']:.0%} (n={b['n']})" if b and b["n"] else "sin backtest"))
        if mode == "picks":
            checks.append(_check("Cuota en el rango elegido", "ok",
                                 (f"cuota real {price:.2f}" if price else f"cuota mínima {min_odds:.2f}") + f" (rango {omin:.2f}–{omax:.2f})"))
            prob_ok = p_low >= S["min_prob_low"]
        else:
            prob_ok = p_low >= S["combo_min_prob_low"]
        checks.append(_check("Probabilidad respaldada", "ok" if prob_ok else "fallo", f"{p:.0%} (intervalo {p_low:.0%}–{p_high:.0%})"))
        unc_ok = width <= S["max_interval_width"]
        checks.append(_check("Incertidumbre aceptable", "ok" if unc_ok else "fallo", f"ancho {width:.0%}"))
        if m in TEAM_MARKETS:
            coh = elo_gap <= 0.15 and (mp is None or abs(p - mp) <= S["max_market_gap"])
            checks.append(_check("Sin contradicciones", "ok" if coh else "fallo",
                                 f"Elo {elo_gap:.0%}" + (f"; mercado {abs(p - mp):.0%}" if mp is not None else "; sin cuota")))
            data_ok = True
        else:
            prof = pl["profile"]
            inj = (prof.get("injury") or {}).get("status")
            reasons_bad = []
            if inj:
                reasons_bad.append(f"en el parte de lesiones: {inj}")
            if prof["n"] < S["min_player_games"]:
                reasons_bad.append(f"sólo {prof['n']} partidos")
            if pl["proj_minutes"] < S["min_player_minutes"]:
                reasons_bad.append(f"proyección de {pl['proj_minutes']:.0f} min")
            cvm = prof["minutes_sd"] / max(prof["minutes"], 1)
            if cvm > S["max_minutes_cv"]:
                reasons_bad.append(f"minutos volátiles ({cvm:.0%})")
            ctx = res["ctx_home"] if pl["team_id"] == g["home_id"] else res["ctx_away"]
            # Novedades del último partido (clave cuando juega noches consecutivas)
            last = prof.get("last") or {}
            recent_game = last and ctx["b2b"]
            if last.get("minutes") and not last.get("blowout") and last["minutes"] < 0.5 * last.get("avg_minutes", 0) \
                    and not last.get("ejected") and last.get("pf", 0) < 5:
                reasons_bad.append(f"salió temprano en su último partido ({last['minutes']:.0f} min vs. {last['avg_minutes']:.0f} "
                                   "de media): posible molestia no confirmada")
            if last.get("ejected"):
                warn += 1
                checks.append(_check("Último partido", "aviso", "fue expulsado en su último partido (posible sanción)"))
            if recent_game and (last.get("minutes", 0) >= 40 or last.get("overtime")) and \
                    (sel["selection"] == "over" or m == "P_DD"):
                reasons_bad.append(f"noche consecutiva tras jugar {last.get('minutes', 0):.0f} min"
                                   + (" con prórroga" if last.get("overtime") else "") + ": fatiga")
            if sel["selection"] == "over" or m == "P_DD":
                if ctx["b2b"] and prof["rest_dnps"] >= 1:
                    reasons_bad.append("gestión de carga: noche consecutiva y ya descansó en otras")
                if spread_abs >= S["blowout_spread"] and prof["starter_rate"] >= 0.5:
                    reasons_bad.append(f"riesgo de paliza (hándicap {spread_abs:.1f}): minutos basura")
                if prof["pf36"] >= 5.0:
                    warn += 1
                    checks.append(_check("Faltas", "aviso", f"{prof['pf36']:.1f} faltas por 36 min: riesgo de problemas de faltas"))
                if prof["ejections"]:
                    warn += 1
                    checks.append(_check("Expulsiones", "aviso", f"{prof['ejections']} expulsión(es) esta temporada"))
            data_ok = not reasons_bad
            checks.append(_check("Disponibilidad y rol del jugador", "ok" if data_ok else "fallo",
                                 "; ".join(reasons_bad) or f"{pl['proj_minutes']:.0f} min proyectados, sano"))
            coh = True
        if price:
            edge, edge_low = p * price - 1, p_low * price - 1
            val_ok = edge >= e and edge_low >= -0.10
            checks.append(_check("Valor en la cuota", "ok" if val_ok else "fallo",
                                 f"cuota {price:.2f}, margen {margin:.1%}, VE {edge:+.1%}"))
        else:
            edge = None
            val_ok = True
            est = fair / (1 + S["typical_margin"])
            checks.append(_check("Valor en la cuota", "n/d", f"apostar sólo si la casa paga ≥ {min_odds:.2f} (cuota típica ~{est:.2f})"))
        checks.append(_check("Sin alertas de riesgo", "ok" if warn == 0 else "aviso", "ninguna" if warn == 0 else f"{warn} advertencia(s)"))
        checks.append(_check("Alineaciones verificadas", "pendiente", "se revalidan antes del partido"))
        checks.append(_check("Revisión del analista (Claude)", "pendiente", "pendiente"))
        if not (cal_ok and prob_ok and unc_ok and coh and val_ok and data_ok) or warn >= 2:
            continue
        score = 40 + (20 if b and b["n"] >= 300 and abs(b["freq"] - b["mean_p"]) <= 0.03 else 10)
        score += 20 if width <= 0.15 else (12 if width <= 0.22 else 6)
        score += 10 if warn == 0 else 4
        score += 10 if (m in TEAM_MARKETS and elo_gap <= 0.08) or (m not in TEAM_MARKETS and pl["profile"]["n"] >= 20) else 5
        if score < S["min_quality_score"]:
            continue
        ref = price or min_odds
        bonus = S["priority_bonus"] if m in S["priority_markets"] else 0.0
        rank = (-abs(ref - S["target_odds"]) + 0.5 * max(edge or 0, 0) + score / 2000 + bonus) if mode == "picks" else \
            (0.6 * p + 0.4 * p_low + score / 1000 + bonus)
        risk = "moderado-bajo" if p >= 0.70 else ("moderado" if p >= 0.60 else "medio-alto")
        out.append({**sel, "prob": p, "prob_low": p_low, "prob_high": p_high, "min_odds": min_odds,
                    "est_book_odds": price or fair / (1 + S["typical_margin"]), "odds_dec": price, "market_prob": mp,
                    "edge": edge, "risk": risk, "quality_score": score, "checks": checks, "rank": rank, "bin": b,
                    "event_id": g["event_id"], "player_ctx": pl})
    return out


def choose(cands: list[dict]) -> list[dict]:
    picks, pg, pm, pp = [], {}, {}, set()
    for c in sorted(cands, key=lambda c: -c["rank"]):
        if pg.get(c["event_id"], 0) >= S["max_picks_per_game"] or pm.get(c["market"], 0) >= S["max_picks_per_market"]:
            continue
        if c["player_id"] and c["player_id"] in pp:
            continue
        picks.append(c)
        pg[c["event_id"]] = pg.get(c["event_id"], 0) + 1
        pm[c["market"]] = pm.get(c["market"], 0) + 1
        if c["player_id"]:
            pp.add(c["player_id"])
        if len(picks) >= S["max_picks"]:
            break
    return picks


def reasons(res: dict, c: dict) -> list[str]:
    g, tp = res["game"], res["team"]
    r = [f"Puntos esperados: {g['home']} {tp['mu_home']:.1f} – {tp['mu_away']:.1f} {g['away']} "
         f"(total {tp['total']:.1f}, margen {tp['margin']:+.1f}; desviación del margen {tp['sd_margin']:.1f})."]
    for name, ctx, fat in ((g["home"], res["ctx_home"], tp["fatigue_home"]), (g["away"], res["ctx_away"], tp["fatigue_away"])):
        bits = [f"{'noches consecutivas' if ctx['b2b'] else str(ctx['rest']) + ' día(s) de descanso'}",
                f"{ctx['games_last7']} partidos en 7 días"]
        if ctx["three_in_four"]:
            bits.append("3 en 4 noches")
        if ctx["road_trip"] >= 2:
            bits.append(f"{ctx['road_trip']}º partido seguido fuera")
        r.append(f"Calendario {name}: " + ", ".join(bits) + f" → efecto estimado {fat:+.1f} pts.")
    for tid, name in ((g["home_id"], g["home"]), (g["away_id"], g["away"])):
        info = res["teams"][tid]
        outs = [info["profiles"][p]["name"] for p in info["out"] if p in info["profiles"]]
        dq = [info["profiles"][p]["name"] for p in info["doubt"] + info["question"] if p in info["profiles"]]
        r.append(f"Parte de lesiones {name}: fuera {', '.join(outs) or 'nadie de la rotación'}"
                 + (f"; en duda {', '.join(dq)}" if dq else "") + ".")
    r.append(f"Control Elo: {g['home']} gana {res['elo_home']:.0%}.")
    pl = c.get("player_ctx")
    if pl:
        pr, cx = pl["profile"], pl["ctx"]
        sm = pl["sim_means"]
        r.append(f"{pr['name']}: {pl['proj_minutes']:.1f} min proyectados (media reciente {pr['minutes']:.1f} ± {pr['minutes_sd']:.1f}); "
                 f"proyección {sm['pts']:.1f} pts, {sm['fg3m']:.1f} triples, {sm['reb']:.1f} reb, {sm['ast']:.1f} ast "
                 f"(PRA {sm['pra']:.1f}); doble-doble {pl['p_dd']:.0%}.")
        r.append(f"Últimos 10: {pr['avg']['pts']:.1f} pts, {pr['avg']['fg3m']:.1f} triples, {pr['avg']['reb']:.1f} reb, "
                 f"{pr['avg']['ast']:.1f} ast · últimos 5 en puntos: {', '.join(map(str, pr['last5_pts']))}.")
        adj = []
        if cx["minutes_adj"]:
            adj.append(f"minutos {cx['minutes_adj']:+.1f}")
        if cx["usage_boost"]:
            adj.append(f"más uso por bajas {cx['usage_boost']:+.0%}")
        adj.append(f"ritmo ×{cx['pace']:.2f}")
        adj.append(f"defensa rival ×{cx['opp_def']:.2f}")
        adj.append(f"prob. de prórroga {cx['p_ot']:.0%}")
        r.append("Ajustes: " + ", ".join(adj) + ".")
        r.append(f"Faltas: {pr['pf36']:.1f} por 36 min · expulsiones esta temporada: {pr['ejections']} · "
                 f"descansos por gestión de carga: {pr['rest_dnps']}.")
        lg = pr.get("last") or {}
        if lg:
            d = lg["date_utc"][:10]
            txt = ("no jugó" + (f" ({lg['dnp_reason']})" if lg.get("dnp_reason") else "")) if lg.get("dnp") else \
                f"{lg['minutes']:.0f} min, {lg.get('pts') or 0} pts, {lg['pf']} faltas"
            extra = [x for x, on in (("expulsado", lg.get("ejected")), ("hubo prórroga", lg.get("overtime")),
                                     ("partido resuelto por 20+", lg.get("blowout"))) if on]
            r.append(f"Último partido ({d}, {lg.get('opponent_game', '')}): {txt}" + (f" · {', '.join(extra)}" if extra else "") + ".")
    if c.get("market_prob") is not None:
        r.append(f"Mercado (sin margen): {c['market_prob']:.0%} vs. sistema {c['prob']:.0%}"
                 + (f"; cuota {c['odds_dec']:.2f}, VE {c['edge']:+.1%}." if c.get("odds_dec") else "."))
    b = c.get("bin")
    if b and b.get("n"):
        r.append(f"Backtest: selecciones de este mercado anunciadas en {b['lo']:.0%}–{b['hi']:.0%} se cumplieron {b['freq']:.0%} (n={b['n']}).")
    for f in res.get("flags", []):
        if f["severity"] != "info":
            r.append(f"⚠ {f['message']}")
    return r

"""Filtro inteligente de calidad: decide qué pronósticos pueden aparecer en el informe final.

Cada selección pasa una lista de comprobaciones (datos, calibración, probabilidad, incertidumbre,
coherencia entre modelos y mercado, valor de la cuota, riesgos, alineación y revisión del
analista) y recibe una puntuación de calidad de 0 a 100. La aplicación NO tiene obligación de
producir pronósticos: si nada supera el filtro, el informe sale vacío.
"""
from __future__ import annotations

from ..config import SELECTION as S

DIRECT_PRICE = {"1X2", "OU_GOALS"}            # mercados con cuota real en la fuente gratuita
MARKET_REF = {"DC"}                            # prob. de mercado derivable de la cuota 1X2
STAT_MARKETS = {"OU_CORNERS", "OU_CARDS", "TEAM_SHOTS", "TEAM_SOT", "GK_SAVES"}
FAMILY = {"1X2": "result", "DC": "result", "OU_GOALS": "goals", "BTTS": "goals", "TEAM_GOALS": "goals",
          "OU_CORNERS": "corners", "OU_CARDS": "cards", "TEAM_SHOTS": "shots", "TEAM_SOT": "shots",
          "GK_SAVES": "saves"}
MAX_PROB = 1 / S["min_fair_odds"]


# ---------------------------------------------------------------------------
# Detector de valor en las cuotas
# ---------------------------------------------------------------------------
def devig_1x2(od: dict | None):
    if not od or not all(od.get(k) for k in ("home_dec", "draw_dec", "away_dec")):
        return None
    inv = [1 / od["home_dec"], 1 / od["draw_dec"], 1 / od["away_dec"]]
    s = sum(inv)
    if not 1.0 <= s <= 1.25:
        return None
    return {"H": inv[0] / s, "D": inv[1] / s, "A": inv[2] / s, "overround": s}


def devig_total(od: dict | None):
    if not od or not (od.get("over_dec") and od.get("under_dec") and od.get("total_line")):
        return None
    io, iu = 1 / od["over_dec"], 1 / od["under_dec"]
    s = io + iu
    if not 1.0 <= s <= 1.2:
        return None
    return {"line": od["total_line"], "over": io / s, "under": iu / s, "overround": s}


def value(sel: dict, p: float, p_low: float, od: dict | None) -> dict:
    """Probabilidad del sistema vs. probabilidad implícita, cuota, margen y valor esperado."""
    m, s = sel["market"], sel["selection"]
    out = {"market_prob": None, "odds_dec": None, "implied": None, "overround": None,
           "edge": None, "edge_low": None}
    x = devig_1x2(od)
    if m == "1X2" and x:
        price = od[{"H": "home_dec", "D": "draw_dec", "A": "away_dec"}[s]]
        out.update(market_prob=x[s], odds_dec=price, implied=1 / price, overround=x["overround"])
    elif m == "DC" and x:
        out.update(market_prob={"1X": x["H"] + x["D"], "X2": x["A"] + x["D"], "12": x["H"] + x["A"]}[s],
                   overround=x["overround"])
    t = devig_total(od)
    if m == "OU_GOALS" and t and abs(t["line"] - sel["line"]) < 1e-9:
        price = od["over_dec" if s == "over" else "under_dec"]
        out.update(market_prob=t[s], odds_dec=price, implied=1 / price, overround=t["overround"])
    if out["odds_dec"]:
        out["edge"] = p * out["odds_dec"] - 1
        out["edge_low"] = p_low * out["odds_dec"] - 1
    return out


# ---------------------------------------------------------------------------
# Evaluación de cada selección
# ---------------------------------------------------------------------------
def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def evaluate_match(ctx: dict, calibrator, market_ok: dict, mode: str = "picks") -> tuple[list[dict], list[str]]:
    """Devuelve (candidatos con checklist y puntuación, notas del partido).

    mode="picks": pronósticos individuales con cuota entre odds_min y odds_max (1.35–1.90).
    mode="combo": selecciones de alta probabilidad para la súper combinada.
    """
    notes = [f["message"] for f in ctx["flags"]]
    if any(f["severity"] == "bloqueo" for f in ctx["flags"]):
        return [], notes
    n_warn = sum(f["severity"] == "advertencia" for f in ctx["flags"])
    fh, fa = ctx["form_home"], ctx["form_away"]
    stats_ok = min(fh["detail_ratio"], fa["detail_ratio"]) >= 0.8
    if not stats_ok:
        notes.append("Estadísticas de córners/tarjetas/tiros incompletas: se omiten esos mercados.")
    pr = {s["selection"]: s["prob_raw"] for s in ctx["selections"] if s["market"] == "1X2"}
    elo = dict(zip("HDA", ctx["elo"]))
    elo_gap = max(abs(pr[k] - elo[k]) for k in "HDA")
    x = devig_1x2(ctx["odds"])
    mkt_gap = max(abs(pr[k] - x[k]) for k in "HDA") if x else None
    result_ok = elo_gap <= S["max_disagreement_elo"] and (mkt_gap is None or mkt_gap <= 0.15)
    if elo_gap > S["max_disagreement_elo"]:
        notes.append(f"Contradicción: Elo y Poisson discrepan {elo_gap:.0%} en el 1X2 (mercados de resultado descartados).")
    if mkt_gap is not None and mkt_gap > 0.15:
        notes.append(f"Contradicción: el modelo difiere {mkt_gap:.0%} del mercado en el 1X2 (posible información faltante).")
    lineup_ok = ctx.get("phase") == "actualizado"
    e, omin, omax = S["min_edge"], S["odds_min"], S["odds_max"]
    out = []
    for sel in ctx["selections"]:
        m = sel["market"]
        if m in STAT_MARKETS and not stats_ok:
            continue
        if FAMILY[m] == "result" and not result_ok:
            continue
        p, b = calibrator.calibrate(m, sel["prob_raw"])
        fair = 1 / p
        min_odds = fair * (1 + e)          # cuota mínima a la que vale la pena apostar
        if mode == "picks":
            price = value(sel, p, p, ctx["odds"])["odds_dec"]
            if not ((omin <= price <= omax) if price else (omin <= min_odds <= omax)):
                continue
        elif p < S["combo_min_prob"] or p > MAX_PROB:
            continue
        # intervalo Monte Carlo trasladado a la escala calibrada
        shift = p - sel["prob_raw"]
        p_low = min(max(sel.get("prob_low", sel["prob_raw"]) + shift, 0.0), 1.0)
        p_high = min(max(sel.get("prob_high", sel["prob_raw"]) + shift, 0.0), 1.0)
        width = p_high - p_low
        v = value(sel, p, p_low, ctx["odds"])
        est_book = v["odds_dec"] or fair / (1 + S["typical_margin"])
        checks = []
        # 1. datos completos
        checks.append(_check("Datos completos", "ok",
                             f"{min(ctx['n_hist'])}+ partidos por equipo; estadísticas {min(fh['detail_ratio'], fa['detail_ratio']):.0%}"))
        # 2. modelo calibrado y validado fuera de muestra
        cal_ok = bool(market_ok.get(m)) and b is not None and b["n"] >= 30 and \
            b["freq"] >= b["mean_p"] - S["max_calibration_gap"]
        checks.append(_check("Modelo calibrado", "ok" if cal_ok else "fallo",
                             f"tramo {b['lo']:.0%}–{b['hi']:.0%}: real {b['freq']:.0%} (n={b['n']})" if b and b["n"]
                             else "sin backtest suficiente"))
        # 3. rango de cuotas y probabilidad respaldada (también en el peor caso del intervalo)
        if mode == "picks":
            prob_ok = p_low >= S["min_prob_low"]
            checks.append(_check("Cuota en el rango elegido", "ok",
                                 (f"cuota real {v['odds_dec']:.2f}" if v["odds_dec"] else f"cuota mínima {min_odds:.2f}")
                                 + f" (rango {omin:.2f}–{omax:.2f})"))
            checks.append(_check("Probabilidad respaldada", "ok" if prob_ok else "fallo",
                                 f"{p:.0%} (intervalo {p_low:.0%}–{p_high:.0%}; peor caso mínimo {S['min_prob_low']:.0%})"))
        else:
            need = S["combo_min_prob"] if (v["market_prob"] is not None) else S["combo_min_prob_no_odds"]
            prob_ok = p >= need and p_low >= S["combo_min_prob_low"]
            checks.append(_check("Probabilidad respaldada", "ok" if prob_ok else "fallo",
                                 f"{p:.0%} (intervalo {p_low:.0%}–{p_high:.0%}; mínimo {need:.0%})"))
        # 4. incertidumbre
        unc_ok = width <= S["max_interval_width"]
        checks.append(_check("Incertidumbre aceptable", "ok" if unc_ok else "fallo", f"ancho {width:.0%}"))
        # 5. información contradictoria
        gap = abs(p - v["market_prob"]) if v["market_prob"] is not None else None
        coh_ok = gap is None or gap <= S["max_market_gap"]
        checks.append(_check("Sin contradicciones", "ok" if coh_ok else "fallo",
                             f"Elo {elo_gap:.0%}" + (f"; mercado {gap:.0%}" if gap is not None else "; sin mercado")))
        # 6. valor de la cuota
        if v["odds_dec"]:
            val_ok = v["edge"] >= e and v["edge_low"] >= (-0.10 if mode == "picks" else -0.02)
            checks.append(_check("Valor en la cuota", "ok" if val_ok else "fallo",
                                 f"cuota {v['odds_dec']:.2f}, margen {v['overround'] - 1:.1%}, VE {v['edge']:+.1%} "
                                 f"(peor caso {v['edge_low']:+.1%})"))
        elif v["market_prob"] is not None:
            val_ok = p >= v["market_prob"] - 0.02
            checks.append(_check("Coherente con el mercado", "ok" if val_ok else "fallo",
                                 f"mercado {v['market_prob']:.0%} vs. modelo {p:.0%}; apostar sólo a cuota ≥ {min_odds:.2f}"))
        else:
            val_ok = True
            checks.append(_check("Valor en la cuota", "n/d",
                                 f"sin cuota gratuita: apostar sólo si la casa paga ≥ {min_odds:.2f} "
                                 f"(cuota típica estimada {est_book:.2f})"))
        # 7. riesgos
        checks.append(_check("Sin alertas de riesgo", "ok" if n_warn == 0 else "aviso",
                             "ninguna" if n_warn == 0 else f"{n_warn} advertencia(s)"))
        # 8. alineación y 9. revisión del analista (pueden estar pendientes en la fase preliminar)
        checks.append(_check("Alineaciones verificadas", "ok" if lineup_ok else "pendiente",
                             "confirmadas" if lineup_ok else "se revalidan antes del partido"))
        checks.append(_check("Revisión del analista (Claude)", "pendiente", "pendiente"))
        critical = cal_ok and prob_ok and unc_ok and coh_ok and val_ok
        if not critical:
            continue
        score = _quality_score(min(ctx["n_hist"]), stats_ok or m not in STAT_MARKETS, b, width,
                               elo_gap, gap, lineup_ok, n_warn)
        if score < S["min_quality_score"]:
            continue
        if mode == "picks":
            risk = "moderado-bajo" if p >= 0.70 else ("moderado" if p >= 0.60 else "medio-alto")
            # preferencia: cuota cercana al objetivo, luego valor y calidad de datos
            ref = v["odds_dec"] or min_odds
            rank = -abs(ref - S["target_odds"]) + 0.5 * max(v["edge"] or 0.0, 0.0) + score / 2000
        else:
            risk = "bajo" if (p >= 0.80 and p_low >= 0.68 and n_warn == 0) else \
                ("moderado-bajo" if p >= 0.75 else "moderado")
            rank = 0.6 * p + 0.4 * p_low + 0.5 * max(v["edge"] or 0.0, 0.0) + score / 1000
        out.append({**sel, **v, "prob": p, "prob_low": p_low, "prob_high": p_high, "risk": risk,
                    "min_odds": min_odds, "est_book_odds": est_book,
                    "quality_score": score, "checks": checks, "rank": rank, "bin": b})
    return out, notes


def _quality_score(n_hist, stats_ok, b, width, elo_gap, mkt_gap, lineup_ok, n_warn) -> int:
    s = 10 if n_hist >= 20 else 6
    s += 10 if stats_ok else 0
    if b and b["n"] >= 100 and abs(b["freq"] - b["mean_p"]) <= 0.03:
        s += 20
    elif b and b["n"] >= 30:
        s += 12
    s += 20 if width <= 0.15 else (14 if width <= 0.22 else (8 if width <= 0.30 else 0))
    s += 10 if elo_gap <= 0.10 else 5
    s += 5 if mkt_gap is None else (10 if mkt_gap <= 0.05 else 5)
    s += 10 if lineup_ok else 4
    s += 10 if n_warn == 0 else 4
    return int(min(s, 100))


def choose(candidates: list[dict]) -> list[dict]:
    """Máximo 1 por partido, 2 por liga, 2 por mercado y 5 en total. Nunca se fuerza un mínimo."""
    picks, per_match, per_league, per_market = [], {}, {}, {}
    for c in sorted(candidates, key=lambda c: -c["rank"]):
        if per_match.get(c["event_id"], 0) >= S["max_picks_per_match"]:
            continue
        if per_league.get(c["league"], 0) >= S["max_picks_per_league"]:
            continue
        if per_market.get(c["market"], 0) >= S["max_picks_per_market"]:
            continue
        picks.append(c)
        for d, k in ((per_match, c["event_id"]), (per_league, c["league"]), (per_market, c["market"])):
            d[k] = d.get(k, 0) + 1
        if len(picks) >= S["max_picks"]:
            break
    return picks


def reasons(ctx: dict, c: dict) -> list[str]:
    fh, fa = ctx["form_home"], ctx["form_away"]
    hh, aa = ctx["home_at_home"], ctx["away_away"]
    lh, la = ctx["expected"]["goals"]
    r = [f"Goles esperados: {ctx['home_name']} {lh:.2f} – {la:.2f} {ctx['away_name']}"
         + (" (sede neutral)." if ctx.get("neutral") else "."),
         f"Últimos 10: {ctx['home_name']} {fh['gf']:.1f} GF / {fh['ga']:.1f} GC ({fh['ppg']:.2f} pts); "
         f"{ctx['away_name']} {fa['gf']:.1f} GF / {fa['ga']:.1f} GC ({fa['ppg']:.2f} pts).",
         f"Como local {ctx['home_name']}: {hh['gf']:.1f} GF / {hh['ga']:.1f} GC (últ. {hh['n']}); "
         f"como visitante {ctx['away_name']}: {aa['gf']:.1f} GF / {aa['ga']:.1f} GC (últ. {aa['n']})."]
    stat = {"OU_CORNERS": "corners", "OU_CARDS": "cards", "TEAM_SHOTS": "shots", "TEAM_SOT": "sot",
            "GK_SAVES": "saves"}.get(c["market"])
    names = {"corners": "córners", "cards": "tarjetas", "shots": "tiros", "sot": "tiros a puerta", "saves": "atajadas"}
    if stat and stat in ctx["expected"]:
        eh, ea = ctx["expected"][stat]
        r.append(f"Media esperada de {names[stat]}: {ctx['home_name']} {eh:.1f} – {ea:.1f} {ctx['away_name']}.")
    eh, ed, ea = ctx["elo"]
    r.append(f"Verificación 1 (modelo independiente Elo): 1X2 {eh:.0%} / {ed:.0%} / {ea:.0%}.")
    if c.get("market_prob") is not None:
        txt = f"Verificación 2 (mercado): probabilidad sin margen {c['market_prob']:.0%} vs. sistema {c['prob']:.0%}"
        if c.get("odds_dec"):
            txt += (f"; cuota {c['odds_dec']:.2f} (implícita {c['implied']:.0%}, margen {c['overround'] - 1:.1%}), "
                    f"valor esperado {c['edge']:+.1%}.")
        r.append(txt + ("" if c.get("odds_dec") else "."))
    else:
        r.append("Verificación 2 (mercado): no hay cuota gratuita para este mercado; se exige mayor probabilidad.")
    r.append(f"Monte Carlo ({ctx.get('mc_draws', 400)} simulaciones de parámetros): probabilidad entre "
             f"{c['prob_low']:.0%} y {c['prob_high']:.0%}.")
    sc = ctx.get("scenarios")
    if sc:
        r.append("Escenarios más probables (10.000 partidos simulados): "
                 + ", ".join(f"{t['score']} ({t['p']:.0%})" for t in sc["top_scores"][:4]) + ".")
    b = c.get("bin")
    if b and b.get("n"):
        r.append(f"Backtest: selecciones de este mercado anunciadas en {b['lo']:.0%}–{b['hi']:.0%} se cumplieron "
                 f"{b['freq']:.0%} de las veces (n={b['n']}).")
    if ctx.get("h2h"):
        r.append("Últimos enfrentamientos: " + " · ".join(ctx["h2h"][:3]))
    for f in ctx["flags"]:
        r.append(f"⚠ {f['message']}")
    return r

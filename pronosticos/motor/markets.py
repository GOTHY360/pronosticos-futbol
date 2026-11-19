"""Convierte las distribuciones del modelo en probabilidades por mercado."""
from __future__ import annotations

import numpy as np

COUNT_STATS = {            # stat -> (familia de mercado, nombre en español, líneas, total o por equipo)
    "corners": ("OU_CORNERS", "córners", [7.5, 8.5, 9.5, 10.5, 11.5, 12.5], "total"),
    "cards": ("OU_CARDS", "tarjetas", [2.5, 3.5, 4.5, 5.5, 6.5], "total"),
    "shots": ("TEAM_SHOTS", "tiros", [7.5, 9.5, 11.5, 13.5, 15.5, 17.5], "team"),
    "sot": ("TEAM_SOT", "tiros a puerta", [2.5, 3.5, 4.5, 5.5, 6.5], "team"),
    "saves": ("GK_SAVES", "atajadas del portero", [1.5, 2.5, 3.5, 4.5], "team"),
}


def _sel(market, selection, line, prob, label, extra=None):
    d = {"market": market, "selection": selection, "line": line, "prob_raw": float(min(max(prob, 1e-6), 1 - 1e-6)),
         "label": label}
    if extra:
        d.update(extra)
    return d


def goal_markets(m: np.ndarray, home: str, away: str) -> list[dict]:
    n = m.shape[0]
    i, j = np.indices(m.shape)
    ph, pd, pa = m[i > j].sum(), m[i == j].sum(), m[i < j].sum()
    tot = i + j
    out = [
        _sel("1X2", "H", 0, ph, f"Gana {home}"),
        _sel("1X2", "D", 0, pd, "Empate"),
        _sel("1X2", "A", 0, pa, f"Gana {away}"),
        _sel("DC", "1X", 0, ph + pd, f"Doble oportunidad 1X ({home} o empate)"),
        _sel("DC", "X2", 0, pa + pd, f"Doble oportunidad X2 ({away} o empate)"),
        _sel("DC", "12", 0, ph + pa, "Doble oportunidad 12 (no hay empate)"),
    ]
    for line in (0.5, 1.5, 2.5, 3.5, 4.5):
        po = m[tot > line].sum()
        out.append(_sel("OU_GOALS", "over", line, po, f"Más de {line} goles"))
        out.append(_sel("OU_GOALS", "under", line, 1 - po, f"Menos de {line} goles"))
    btts = m[1:, 1:].sum()
    out.append(_sel("BTTS", "yes", 0, btts, "Ambos equipos marcan: Sí"))
    out.append(_sel("BTTS", "no", 0, 1 - btts, "Ambos equipos marcan: No"))
    ph_goals, pa_goals = m.sum(axis=1), m.sum(axis=0)
    for line in (0.5, 1.5):
        out.append(_sel("TEAM_GOALS", "home_over", line, ph_goals[int(line) + 1:].sum(), f"{home} más de {line} goles"))
        out.append(_sel("TEAM_GOALS", "away_over", line, pa_goals[int(line) + 1:].sum(), f"{away} más de {line} goles"))
    return out


def count_markets(stat: str, dh: np.ndarray, da: np.ndarray, home: str, away: str) -> list[dict]:
    fam, name, lines, kind = COUNT_STATS[stat]
    out = []
    if kind == "total":
        dt = np.convolve(dh, da)
        for line in lines:
            po = dt[int(line) + 1:].sum()
            out.append(_sel(fam, "over", line, po, f"Más de {line} {name} en el partido"))
            out.append(_sel(fam, "under", line, 1 - po, f"Menos de {line} {name} en el partido"))
    else:
        for side, d, team in (("home", dh, home), ("away", da, away)):
            for line in lines:
                po = d[int(line) + 1:].sum()
                out.append(_sel(fam, f"{side}_over", line, po, f"{team}: más de {line} {name}"))
                out.append(_sel(fam, f"{side}_under", line, 1 - po, f"{team}: menos de {line} {name}"))
    return out


def settle(market: str, selection: str, line: float, res: dict) -> int | None:
    """1 si la selección se cumplió, 0 si no, None si falta el dato. res: goles y estadísticas finales."""
    hg, ag = res.get("home_goals"), res.get("away_goals")
    if market in ("1X2", "DC", "OU_GOALS", "BTTS", "TEAM_GOALS") and (hg is None or ag is None):
        return None
    if market == "1X2":
        return int({"H": hg > ag, "D": hg == ag, "A": hg < ag}[selection])
    if market == "DC":
        return int({"1X": hg >= ag, "X2": ag >= hg, "12": hg != ag}[selection])
    if market == "OU_GOALS":
        return int((hg + ag > line) == (selection == "over"))
    if market == "BTTS":
        return int((hg > 0 and ag > 0) == (selection == "yes"))
    if market == "TEAM_GOALS":
        v = hg if selection.startswith("home") else ag
        return int(v > line)
    for stat, (fam, _, _, kind) in COUNT_STATS.items():
        if market == fam:
            h, a = res.get(f"h_{stat}"), res.get(f"a_{stat}")
            if kind == "total":
                if h is None or a is None:
                    return None
                return int((h + a > line) == (selection == "over"))
            v = h if selection.startswith("home") else a
            if v is None:
                return None
            return int((v > line) == selection.endswith("over"))
    return None

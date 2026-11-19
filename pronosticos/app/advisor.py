"""Asesor de inversión: avisa cuando los resultados REALES justifican pagar APIs.

Una racha por sí sola no prueba nada (con pronósticos del 66 %, 8 aciertos seguidos salen por
azar ~1 de cada 28 veces). Por eso hay dos niveles y cada uno se avisa una sola vez:

  1 · racha  — racha ≥ 10 aciertos, ≥ 50 liquidados y efectividad real ≥ anunciada.
               Mensaje: vamos bien, pero aún no pagar.
  2 · invertir — ≥ 100 liquidados, efectividad real ≥ anunciada − 2 puntos y el límite inferior
               (95 %) de la efectividad supera el punto de equilibrio de las cuotas mínimas.
               Mensaje: conviene pagar el plan de API-Football con cuotas de todos los mercados.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

SCHEMA = "CREATE TABLE IF NOT EXISTS advisor (level TEXT PRIMARY KEY, reached_at TEXT, metrics TEXT, notified INTEGER DEFAULT 0)"

LEVEL_STREAK = {"streak": 10, "min_n": 50}
LEVEL_INVEST = {"min_n": 100, "max_gap": 0.02}


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    den = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den


def metrics(con) -> dict:
    rows = con.execute("""SELECT status, prob, COALESCE(odds_initial, min_odds) odds FROM picks
                          WHERE status IN ('won','lost') ORDER BY kickoff_utc, pick_id""").fetchall()
    n = len(rows)
    wins = sum(r["status"] == "won" for r in rows)
    streak = best = 0
    for r in rows:
        streak = streak + 1 if r["status"] == "won" else 0
        best = max(best, streak)
    with_odds = [r for r in rows if r["odds"]]
    breakeven = sum(1 / r["odds"] for r in with_odds) / len(with_odds) if with_odds else None
    profit = sum((r["odds"] - 1) if r["status"] == "won" else -1 for r in with_odds)
    return {"n": n, "wins": wins, "hit": wins / n if n else None,
            "expected": sum(r["prob"] for r in rows) / n if n else None,
            "hit_low95": wilson_lower(wins, n), "breakeven": breakeven,
            "current_streak": streak, "best_streak": best,
            "roi_at_min_odds": profit / len(with_odds) if with_odds else None}


def evaluate(con, record: bool = True) -> dict:
    """record=False: sólo consulta (para el tablero), sin registrar ni avisar."""
    con.execute(SCHEMA)
    m = metrics(con)
    reached = {r["level"]: dict(r) for r in con.execute("SELECT * FROM advisor")}
    lvl = None
    if m["n"] >= LEVEL_INVEST["min_n"] and m["hit"] >= m["expected"] - LEVEL_INVEST["max_gap"] \
            and m["breakeven"] and m["hit_low95"] > m["breakeven"]:
        lvl = "invertir"
    elif m["current_streak"] >= LEVEL_STREAK["streak"] and m["n"] >= LEVEL_STREAK["min_n"] \
            and m["hit"] >= m["expected"]:
        lvl = "racha"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = None
    if record and lvl and lvl not in reached:
        con.execute("INSERT INTO advisor(level, reached_at, metrics) VALUES (?,?,?)", (lvl, now, json.dumps(m)))
        con.commit()
        new = lvl
    progress = {
        "liquidados": f"{m['n']} / {LEVEL_INVEST['min_n']}",
        "efectividad": None if m["hit"] is None else f"{m['hit']:.1%} real vs. {m['expected']:.1%} anunciada",
        "racha_actual": m["current_streak"], "mejor_racha": m["best_streak"],
    }
    return {"level": lvl, "new": new, "reached": list(reached) + ([new] if new else []), "metrics": m,
            "progress": progress, "message": message(lvl, m) if lvl else None}


def message(level: str, m: dict) -> str:
    base = (f"Pronósticos liquidados: {m['n']} · efectividad real {m['hit']:.1%} (anunciada {m['expected']:.1%}) · "
            f"racha actual {m['current_streak']} (mejor {m['best_streak']}).")
    if level == "racha":
        return ("🔔 RACHA PROMETEDORA\n" + base +
                "\nVamos bien, pero una racha puede ser suerte: TODAVÍA NO PAGUES APIs. "
                f"El aviso de inversión llegará al confirmar el rendimiento con {LEVEL_INVEST['min_n']} pronósticos.")
    roi = f"{m['roi_at_min_odds']:+.1%}" if m["roi_at_min_odds"] is not None else "n/d"
    return ("💰 MOMENTO DE INVERTIR EN DATOS\n" + base +
            f"\nCon 95 % de confianza la efectividad ({m['hit_low95']:.1%} como mínimo) supera el punto de equilibrio "
            f"de las cuotas ({m['breakeven']:.1%}). Rendimiento a cuota mínima: {roi}."
            "\nRecomendación: plan pago de API-Football (desde ~19 USD/mes; confirma el precio en api-football.com/pricing) "
            "para traer cuotas reales de córners, tarjetas, tiros y ambos marcan y medir el valor en todos los mercados. "
            "Más adelante, Sportmonks si quieres incorporar xG.")


def notify_if_new(con, result: dict, send) -> None:
    """Envía el aviso por Telegram una sola vez por nivel (si Telegram está configurado)."""
    if not result.get("new"):
        return
    if send(result["message"]):
        con.execute("UPDATE advisor SET notified=1 WHERE level=?", (result["new"],))
        con.commit()

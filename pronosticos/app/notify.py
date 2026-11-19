"""Entrega de los pronósticos por Telegram (gratis). Requiere dos variables de entorno:
TELEGRAM_TOKEN (del bot creado con @BotFather) y TELEGRAM_CHAT_ID (tu chat con el bot).
Opcional: DASHBOARD_URL con el enlace al tablero publicado.

    python -m pronosticos.app.notify
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime

from ..config import SITE_DATA_DIR, TZ


def build_message(latest: dict) -> str:
    d = datetime.fromisoformat(latest["target_date"])
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    lines = [f"⚽ Pronósticos para el {dias[d.weekday()]} {d.day}/{d.month}",
             f"Partidos analizados: {len(latest.get('analyzed', []))}"]
    picks = latest.get("picks", [])
    if not picks:
        lines.append("\nHoy no hay oportunidades con respaldo suficiente. No pronosticar también es una buena decisión.")
    for i, p in enumerate(picks, 1):
        hora = datetime.fromisoformat(p["kickoff_utc"].replace("Z", "+00:00")).astimezone(TZ).strftime("%H:%M")
        cuota = f"cuota real {p['odds_dec']:.2f}" if p.get("odds_dec") else f"apostar sólo a cuota ≥ {p['min_odds']:.2f}"
        rev = {"aprobado": "✅ aprobado", "pendiente": "⏳ revisión pendiente"}.get(p.get("review_status"), p.get("review_status"))
        lines.append(f"\n{i}. {p['match_name']} ({p['league_name']}, {hora} COL)\n   👉 {p['label']}\n"
                     f"   Prob. {p['prob']:.0%} · {cuota} · {rev}")
    m = latest.get("mega")
    if m:
        lines.append(f"\n🎯 Súper combinada ({m['n_legs']} selecciones): prob. {m['prob']:.0%}, cuota justa {m['fair_odds']:.2f}. Alto riesgo.")
    if latest.get("in_review"):
        lines.append(f"\n🔎 {len(latest['in_review'])} pronóstico(s) en revisión hasta conocer alineaciones.")
    url = os.environ.get("DASHBOARD_URL")
    if url:
        lines.append(f"\nDetalle completo: {url}")
    lines.append("\nProbabilidad alta no es garantía. Apuesta sólo lo que puedas perder.")
    return "\n".join(lines)


def send(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram no configurado (faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_ID).")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20) as r:
        ok = json.loads(r.read().decode()).get("ok", False)
    print("Mensaje enviado." if ok else "Telegram rechazó el mensaje.")
    return ok


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    path = SITE_DATA_DIR / "latest.json"
    if not path.exists():
        print("No hay informe para enviar.")
        return
    text = build_message(json.loads(path.read_text(encoding="utf-8")))
    print(text)
    send(text)


if __name__ == "__main__":
    main()

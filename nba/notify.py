"""Entrega por Telegram de los pronósticos NBA (usa el mismo bot que fútbol)."""
import json
import os
import sys

from pronosticos.app.notify import build_message, send

from .config import SITE_DATA_DIR


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    path = SITE_DATA_DIR / "latest.json"
    if not path.exists():
        print("No hay informe NBA para enviar.")
        return
    latest = json.loads(path.read_text(encoding="utf-8"))
    if not latest.get("analyzed"):
        print("Sin partidos NBA mañana: no se envía mensaje.")
        return
    url = os.environ.get("DASHBOARD_URL")
    if url:
        os.environ["DASHBOARD_URL"] = url.rstrip("/") + "/nba/"
    text = build_message(latest).replace("⚽ Pronósticos", "🏀 Pronósticos NBA")
    print(text)
    send(text)


if __name__ == "__main__":
    main()

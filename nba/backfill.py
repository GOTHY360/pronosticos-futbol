"""Descarga inicial del historial NBA (2024-25 en adelante)."""
import sys

from .db import connect
from .ingest import sync_calendar, sync_details, sync_injuries


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    con = connect()
    log = lambda s: print(s, flush=True)
    log("1) Calendario y resultados…")
    sync_calendar(con, log=log)
    log("2) Box scores de cada partido…")
    sync_details(con, log=log)
    log("3) Parte de lesiones…")
    sync_injuries(con, log=log)
    log("Listo.")


if __name__ == "__main__":
    main()

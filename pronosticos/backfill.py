"""Descarga inicial del historial (se ejecuta una vez; luego lo hace run_daily)."""
import sys

from .db import connect
from .datos.ingest import sync_calendar, sync_details


def main():
    con = connect()
    print("1) Calendario y resultados por mes…", flush=True)
    sync_calendar(con, log=lambda s: print(s, flush=True))
    print("2) Estadísticas detalladas de cada partido terminado…", flush=True)
    sync_details(con, limit=int(sys.argv[1]) if len(sys.argv) > 1 else None,
                 log=lambda s: print(s, flush=True))
    print("Listo.", flush=True)


if __name__ == "__main__":
    main()

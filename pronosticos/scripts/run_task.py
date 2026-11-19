"""Ejecuta una tarea programada sin ventana (con pythonw) y guarda la salida en un log.

    pythonw run_task.py nightly    # análisis preliminar de mañana (+ tuning/backtest los domingos)
    pythonw run_task.py update     # alineaciones y cambios de última hora
    pythonw run_task.py open       # abre el tablero en el navegador
"""
import datetime
import os
import runpy
import sys
import traceback
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # carpeta DAPORTIVAS
LOGS = os.path.join(ROOT, "pronosticos", "data", "logs")
URL = "http://localhost:8765"


def run_module(mod, *args):
    """Cada módulo se aísla: un fallo en fútbol no impide la NBA (y viceversa)."""
    sys.argv = [mod, *args]
    try:
        runpy.run_module(mod, run_name="__main__", alter_sys=True)
    except SystemExit:
        pass
    except Exception:
        traceback.print_exc()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "nightly"
    if mode == "open":
        webbrowser.open(URL)
        return
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    os.makedirs(LOGS, exist_ok=True)
    log = open(os.path.join(LOGS, f"{mode}.log"), "a", encoding="utf-8")
    sys.stdout = sys.stderr = log
    print(f"\n===== {mode} · {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====", flush=True)
    try:
        if mode == "nightly":
            run_module("pronosticos.app.run_daily")
            run_module("nba.run_daily")
            if datetime.date.today().weekday() == 6:          # domingo: reajuste semanal
                run_module("pronosticos.calidad.tuning")
                run_module("pronosticos.calidad.backtest")
                run_module("nba.backtest")
        elif mode == "update":
            run_module("pronosticos.app.run_daily", "--update")
            run_module("nba.run_daily", "--update")
        elif mode == "review":
            run_module("pronosticos.app.run_daily", "--review")
            run_module("nba.run_daily", "--review")
    except Exception:
        traceback.print_exc()
    finally:
        log.flush()


if __name__ == "__main__":
    main()

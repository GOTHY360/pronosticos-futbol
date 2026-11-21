"""Espera hasta una hora de Colombia (HH:MM). Si esa hora ya pasó hace poco, no espera.

    python pronosticos/scripts/wait_until.py 21:00

Sirve para que la nube arranque temprano (absorbe los retrasos de GitHub) y entregue a la hora exacta.
Si la hora objetivo quedó más de 12 h atrás se entiende que es la de mañana (p. ej. 00:00 a las 23:50).
"""
import datetime
import sys
import time

TZ = datetime.timezone(datetime.timedelta(hours=-5))


def main():
    hh, mm = map(int, sys.argv[1].split(":"))
    now = datetime.datetime.now(TZ)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    diff = (target - now).total_seconds()
    if diff < -12 * 3600:
        diff += 24 * 3600
    if diff > 0:
        print(f"Esperando {diff / 60:.0f} min hasta las {sys.argv[1]} (Colombia)", flush=True)
        time.sleep(diff)
    else:
        print(f"Ya son más de las {sys.argv[1]}: sigue sin esperar", flush=True)


if __name__ == "__main__":
    main()

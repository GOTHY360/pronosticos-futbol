"""Atajo: python -m pronosticos.backtest"""
import runpy

if __name__ == "__main__":
    runpy.run_module("pronosticos.calidad.backtest", run_name="__main__", alter_sys=True)

"""Atajo: python -m pronosticos.run_daily"""
import runpy

if __name__ == "__main__":
    runpy.run_module("pronosticos.app.run_daily", run_name="__main__", alter_sys=True)

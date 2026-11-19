"""Atajo: python -m pronosticos.tuning"""
import runpy

if __name__ == "__main__":
    runpy.run_module("pronosticos.calidad.tuning", run_name="__main__", alter_sys=True)

"""Configuración central del proyecto AI Football Predictor."""
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "pronosticos.sqlite"
SITE_DIR = ROOT / "web"
SITE_DATA_DIR = SITE_DIR / "data"
REVIEW_DIR = DATA_DIR / "revision"          # intercambio con el analista (Claude)
MODEL_PARAMS_PATH = DATA_DIR / "model_params.json"
MODEL_VERSION = "2.0"

# Zona horaria del usuario: "mañana" se calcula con esta zona.
TZ = timezone(timedelta(hours=-5), "COT")  # America/Bogota (sin horario de verano)

# ---------------------------------------------------------------------------
# Competiciones (código ESPN). "pool" = conjunto de partidos con el que se ajustan las
# fuerzas de los equipos:
#   - liga nacional: su propia liga (más preciso para partidos de liga)
#   - "clubs": todas las ligas + copas continentales (conecta equipos de distintos países)
#   - "national": todos los partidos de selecciones (amistosos, eliminatorias, torneos)
# kind: league | cup | national | tournament (sede neutral) | friendly
# ---------------------------------------------------------------------------
C = lambda name, pool, kind, start="2024-07": {"name": name, "pool": pool, "kind": kind, "start": start}
COMPETITIONS = {
    # Ligas
    "eng.1": C("Premier League", "eng.1", "league"),
    "esp.1": C("LaLiga", "esp.1", "league"),
    "ita.1": C("Serie A", "ita.1", "league"),
    "ger.1": C("Bundesliga", "ger.1", "league"),
    "fra.1": C("Ligue 1", "fra.1", "league"),
    "ned.1": C("Eredivisie", "ned.1", "league"),
    "por.1": C("Primeira Liga", "por.1", "league"),
    "mex.1": C("Liga MX", "mex.1", "league"),
    "bra.1": C("Brasileirão", "bra.1", "league"),
    "arg.1": C("Liga Profesional (ARG)", "arg.1", "league"),
    "col.1": C("Primera A (COL)", "col.1", "league"),
    # Copas de clubes
    "uefa.champions": C("Champions League", "clubs", "cup"),
    "uefa.europa": C("Europa League", "clubs", "cup"),
    "uefa.europa.conf": C("Conference League", "clubs", "cup"),
    "conmebol.libertadores": C("Copa Libertadores", "clubs", "cup"),
    "conmebol.sudamericana": C("Copa Sudamericana", "clubs", "cup"),
    # Selecciones
    "uefa.nations": C("UEFA Nations League", "national", "national", "2023-01"),
    "concacaf.nations.league": C("Liga de Naciones CONCACAF", "national", "national", "2023-01"),
    "fifa.worldq.uefa": C("Eliminatorias UEFA", "national", "national", "2023-01"),
    "fifa.worldq.conmebol": C("Eliminatorias CONMEBOL", "national", "national", "2023-01"),
    "fifa.world": C("Copa del Mundo", "national", "tournament", "2022-11"),
    "uefa.euro": C("Eurocopa", "national", "tournament", "2024-06"),
    "conmebol.america": C("Copa América", "national", "tournament", "2024-06"),
    "fifa.friendly": C("Amistosos internacionales", "national", "friendly", "2023-01"),
}
LEAGUES = {k: v["name"] for k, v in COMPETITIONS.items()}       # compatibilidad
HISTORY_START = "2024-07"

# Amistosos: se analizan pero NO se publican por defecto (rotaciones y motivación inciertas).
PUBLISH_FRIENDLIES = False

# ---------------------------------------------------------------------------
# Filtro inteligente de calidad: máxima precisión, 0 a 5 pronósticos al día.
# ---------------------------------------------------------------------------
SELECTION = {
    "max_picks": 5,
    "min_picks_target": 3,           # objetivo, NO obligación
    # Rango de cuotas de los pronósticos individuales (elegido por el usuario): 1.35 – 1.90.
    # Con cuota real se exige que esté en el rango y tenga valor. Sin cuota real, la "cuota mínima
    # para apostar" (cuota justa + valor mínimo) debe caer en el rango: sólo se apuesta si la casa
    # paga al menos esa cuota.
    "odds_min": 1.35,
    "odds_max": 1.90,
    # Preferencia dentro del rango (validado fuera de muestra, mar–nov 2026):
    #   1.36 → acierto ~77%   1.45 → ~71%   1.55 → ~67%   1.65 → ~62%   1.75 → ~58%
    "target_odds": 1.55,
    "typical_margin": 0.06,          # margen típico de la casa por selección (para estimar su cuota)
    "min_prob_low": 0.40,            # peor caso del intervalo Monte Carlo (p5)
    "min_edge": 0.02,                # valor esperado mínimo exigido
    # Súper combinada: selecciones de alta probabilidad (rango anterior)
    "combo_min_prob": 0.72,
    "combo_min_prob_no_odds": 0.78,
    "combo_min_prob_low": 0.60,
    "min_fair_odds": 1.18,           # descarta líneas triviales
    "max_market_gap": 0.12,          # modelo vs. mercado: más diferencia = información faltante
    "max_disagreement_elo": 0.18,    # modelo vs. Elo en el 1X2
    "max_interval_width": 0.30,      # ancho máximo del intervalo de probabilidad (p95 - p5)
    "max_odds_move": 0.08,           # movimiento de prob. implícita que dispara alerta
    "min_team_matches": 10,
    "min_market_backtest_n": 150,
    "max_calibration_gap": 0.05,
    "max_picks_per_match": 1,
    "max_picks_per_league": 2,
    "max_picks_per_market": 2,
    "min_quality_score": 70,         # calidad de datos mínima (0–100)
}

MODEL = {
    "half_life_days": 180,           # valor por defecto; tuning.py lo ajusta por mercado
    "prior_strength": 4.0,
    "max_goals": 10,
    "elo_k": 20,
    "elo_home_adv": 60,
    "mc_draws": 400,                 # simulaciones de parámetros (Monte Carlo)
}

HTTP = {"delay_s": 0.35, "timeout_s": 25, "retries": 4}

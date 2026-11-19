"""Configuración del AI NBA Predictor (mismos principios que la app de fútbol)."""
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "nba.sqlite"
REVIEW_DIR = DATA_DIR / "revision"
SITE_DATA_DIR = ROOT.parent / "pronosticos" / "web" / "nba" / "data"   # se publica junto a la app de fútbol
MODEL_VERSION = "nba-1.0"

TZ = timezone(timedelta(hours=-5), "COT")
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
HISTORY_START = "2024-10"            # temporadas 2024-25, 2025-26 y la actual

SELECTION = {
    "max_picks": 5,
    "odds_min": 1.35, "odds_max": 1.90, "target_odds": 1.55,   # rango elegido por el usuario
    "min_edge": 0.02,
    "typical_margin": 0.07,             # los props de jugador suelen tener más margen
    "min_prob_low": 0.40,
    "max_interval_width": 0.30,
    "max_market_gap": 0.12,             # modelo vs. mercado (ganador / hándicap / total)
    "min_team_games": 8,
    "min_player_games": 8,              # partidos con minutos para pronosticar a un jugador
    "min_player_minutes": 20,           # minutos proyectados mínimos para props de jugador
    "max_minutes_cv": 0.30,             # volatilidad máxima de minutos (desv./media)
    "blowout_spread": 12.0,             # con hándicap mayor, riesgo de minutos basura: sin overs de titulares
    "min_market_backtest_n": 300,
    "max_calibration_gap": 0.05,
    "max_picks_per_game": 1,
    "max_picks_per_market": 2,
    "min_quality_score": 70,
    # Mercados prioritarios (pedidos por el usuario) y mercados excluidos (rara vez ofrecidos por las casas)
    "priority_markets": ["P_PTS", "P_3PM", "P_AST", "P_PRA", "ML", "TOTAL", "SPREAD"],
    "excluded_markets": ["P_STOCKS"],
    # media mínima del jugador en esa estadística: evita líneas triviales (p. ej. "pívot: menos de 1.5 triples")
    "min_player_mean": {"P_PTS": 9.0, "P_3PM": 1.3, "P_REB": 3.5, "P_AST": 2.5, "P_PRA": 16.0, "P_PR": 13.0,
                        "P_PA": 12.0, "P_RA": 6.0, "P_DD": 0.15},
    "priority_bonus": 0.03,
    # súper combinada
    "combo_min_prob": 0.72, "combo_min_prob_low": 0.60, "min_fair_odds": 1.18,
}

MODEL = {
    "half_life_days": 60,               # la NBA cambia rápido: más peso a lo reciente
    "prior_strength": 6.0,
    "player_half_life_games": 12,       # ponderación de los últimos partidos del jugador
    "player_prior_games": 6,            # encogimiento hacia el promedio de la temporada/posición
    "mc_draws": 3000,
    "elo_k": 20, "elo_home_adv": 70,
}

HTTP = {"delay_s": 0.3, "timeout_s": 25, "retries": 4}

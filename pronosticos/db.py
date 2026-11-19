"""Base de datos SQLite: historial, cuotas, noticias, predicciones, memoria de pronósticos y alertas."""
import sqlite3

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    event_id TEXT PRIMARY KEY, league TEXT, date_utc TEXT, status TEXT,
    home_id TEXT, home_name TEXT, away_id TEXT, away_name TEXT,
    home_goals INTEGER, away_goals INTEGER, venue TEXT,
    has_details INTEGER DEFAULT 0, neutral INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_matches_league_date ON matches(league, date_utc);
CREATE INDEX IF NOT EXISTS ix_matches_date ON matches(date_utc);

CREATE TABLE IF NOT EXISTS team_stats (
    event_id TEXT, team_id TEXT, is_home INTEGER,
    goals INTEGER, shots INTEGER, sot INTEGER, corners INTEGER,
    yellow INTEGER, red INTEGER, saves INTEGER, fouls INTEGER, possession REAL,
    PRIMARY KEY (event_id, team_id)
);

CREATE TABLE IF NOT EXISTS player_stats (
    event_id TEXT, team_id TEXT, player_id TEXT, name TEXT, position TEXT,
    starter INTEGER, sub_in INTEGER, shots INTEGER, sot INTEGER, goals INTEGER,
    saves INTEGER, yellow INTEGER, red INTEGER,
    PRIMARY KEY (event_id, player_id)
);
CREATE INDEX IF NOT EXISTS ix_player_team ON player_stats(team_id);

-- Cuotas previas capturadas por nosotros en cada ejecución: serie temporal para detectar movimientos.
CREATE TABLE IF NOT EXISTS odds (
    event_id TEXT, taken_at TEXT, kind TEXT, provider TEXT,
    home_dec REAL, draw_dec REAL, away_dec REAL,
    total_line REAL, over_dec REAL, under_dec REAL,
    PRIMARY KEY (event_id, taken_at)
);

-- Titulares de noticias (partido y liga): insumo del detector de riesgos y de la revisión de Claude.
CREATE TABLE IF NOT EXISTS news (
    news_id TEXT PRIMARY KEY, league TEXT, event_id TEXT, published TEXT,
    headline TEXT, description TEXT, url TEXT
);

-- Todas las probabilidades calculadas (no sólo las publicadas): memoria para evaluar cada modelo.
CREATE TABLE IF NOT EXISTS predictions (
    run_id TEXT, phase TEXT, event_id TEXT, market TEXT, selection TEXT, line REAL,
    model TEXT, model_version TEXT, prob_raw REAL, prob REAL, prob_low REAL, prob_high REAL,
    market_prob REAL, odds_dec REAL, outcome INTEGER,
    PRIMARY KEY (run_id, event_id, market, selection, line)
);

-- Registro de TODOS los pronósticos publicados (incluidos fallados, retirados y nulos).
CREATE TABLE IF NOT EXISTS picks (
    pick_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, target_date TEXT, event_id TEXT, league TEXT, match_name TEXT, kickoff_utc TEXT,
    market TEXT, selection TEXT, line REAL, label TEXT, model TEXT, model_version TEXT,
    phase TEXT DEFAULT 'preliminar',
    prob REAL, prob_low REAL, prob_high REAL, prob_initial REAL,
    market_prob REAL, odds_dec REAL, odds_initial REAL, overround REAL, edge REAL,
    risk TEXT, quality_score INTEGER, quality_checks TEXT, reasons TEXT,
    lineup_status TEXT DEFAULT 'pendiente',
    review_status TEXT DEFAULT 'pendiente', review_notes TEXT,
    status TEXT DEFAULT 'pending', result_detail TEXT, settled_at TEXT, updated_at TEXT,
    UNIQUE (target_date, event_id, market, selection, line)
);

-- Cambios de última hora detectados (alineaciones, cuotas, aplazamientos, noticias).
CREATE TABLE IF NOT EXISTS alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, event_id TEXT, pick_id INTEGER,
    kind TEXT, severity TEXT, message TEXT, action TEXT
);

-- Backtest fuera de muestra (regla 6): calibración, Brier y simulación del selector.
CREATE TABLE IF NOT EXISTS backtest (
    event_id TEXT, league TEXT, date_utc TEXT, market TEXT, selection TEXT, line REAL,
    prob REAL, outcome INTEGER,
    PRIMARY KEY (event_id, market, selection, line)
);

-- Súper combinada diaria (alto riesgo): también se registra y liquida siempre.
CREATE TABLE IF NOT EXISTS combos (
    combo_id INTEGER PRIMARY KEY AUTOINCREMENT, target_date TEXT UNIQUE, created_at TEXT,
    legs TEXT, n_legs INTEGER, prob REAL, prob_low REAL, prob_high REAL,
    fair_odds REAL, est_odds REAL, status TEXT DEFAULT 'pending', result_detail TEXT, settled_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT, notes TEXT
);
"""


def _migrate(con):
    def cols(t):
        return {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
    if "neutral" not in cols("matches"):
        con.execute("ALTER TABLE matches ADD COLUMN neutral INTEGER DEFAULT 0")
    if "red" not in cols("player_stats"):
        con.execute("ALTER TABLE player_stats ADD COLUMN red INTEGER")
    # Versiones anteriores (sólo datos de prueba): se recrean con el esquema nuevo.
    if "phase" not in cols("picks"):
        con.execute("DROP TABLE picks")
    if "phase" not in cols("predictions"):
        con.execute("DROP TABLE predictions")
    pc = cols("picks")
    if pc and "min_odds" not in pc:
        con.execute("ALTER TABLE picks ADD COLUMN min_odds REAL")
    if pc and "est_book_odds" not in pc:
        con.execute("ALTER TABLE picks ADD COLUMN est_book_odds REAL")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    _migrate(con)
    con.executescript(SCHEMA)
    con.commit()
    return con

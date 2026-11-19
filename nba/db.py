"""Base de datos de la NBA."""
import sqlite3

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    event_id TEXT PRIMARY KEY, season INTEGER, season_type INTEGER, date_utc TEXT, status TEXT,
    home_id TEXT, home TEXT, away_id TEXT, away TEXT, home_pts INTEGER, away_pts INTEGER,
    periods INTEGER, neutral INTEGER DEFAULT 0, venue TEXT, has_details INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_games_date ON games(date_utc);

CREATE TABLE IF NOT EXISTS team_games (
    event_id TEXT, team_id TEXT, is_home INTEGER, pts INTEGER, fgm INTEGER, fga INTEGER, fg3m INTEGER,
    fg3a INTEGER, ftm INTEGER, fta INTEGER, oreb INTEGER, dreb INTEGER, reb INTEGER, ast INTEGER,
    stl INTEGER, blk INTEGER, tov INTEGER, fouls INTEGER, tech INTEGER, flagrant INTEGER, poss REAL,
    PRIMARY KEY (event_id, team_id)
);

CREATE TABLE IF NOT EXISTS player_games (
    event_id TEXT, team_id TEXT, player_id TEXT, name TEXT, pos TEXT, starter INTEGER,
    minutes REAL, pts INTEGER, fg3m INTEGER, fg3a INTEGER, fga INTEGER, fta INTEGER, reb INTEGER,
    ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER, plus_minus INTEGER,
    dnp INTEGER, dnp_reason TEXT, ejected INTEGER,
    PRIMARY KEY (event_id, player_id)
);
CREATE INDEX IF NOT EXISTS ix_pg_player ON player_games(player_id);
CREATE INDEX IF NOT EXISTS ix_pg_team ON player_games(team_id);

CREATE TABLE IF NOT EXISTS odds (
    event_id TEXT, taken_at TEXT, provider TEXT, home_dec REAL, away_dec REAL,
    spread_home REAL, spread_home_dec REAL, spread_away_dec REAL,
    total_line REAL, over_dec REAL, under_dec REAL,
    PRIMARY KEY (event_id, taken_at)
);

-- Parte oficial de lesiones (instantáneas: permite ver cambios de estado durante el día)
CREATE TABLE IF NOT EXISTS injuries (
    taken_at TEXT, team TEXT, player_id TEXT, name TEXT, status TEXT, detail TEXT,
    return_date TEXT, comment TEXT, report_date TEXT,
    PRIMARY KEY (taken_at, player_id)
);

CREATE TABLE IF NOT EXISTS news (
    news_id TEXT PRIMARY KEY, published TEXT, headline TEXT, description TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    run_id TEXT, phase TEXT, event_id TEXT, market TEXT, selection TEXT, line REAL, player_id TEXT,
    model TEXT, prob_raw REAL, prob REAL, prob_low REAL, prob_high REAL, outcome INTEGER,
    PRIMARY KEY (run_id, event_id, market, selection, line, player_id)
);

CREATE TABLE IF NOT EXISTS picks (
    pick_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, target_date TEXT, event_id TEXT, match_name TEXT, kickoff_utc TEXT,
    market TEXT, selection TEXT, line REAL, player_id TEXT, player TEXT, label TEXT, model TEXT,
    phase TEXT DEFAULT 'preliminar', prob REAL, prob_low REAL, prob_high REAL, prob_initial REAL,
    market_prob REAL, odds_dec REAL, odds_initial REAL, edge REAL, min_odds REAL, est_book_odds REAL,
    neighbors TEXT, risk TEXT, quality_score INTEGER, quality_checks TEXT, reasons TEXT,
    lineup_status TEXT DEFAULT 'pendiente', review_status TEXT DEFAULT 'pendiente', review_notes TEXT,
    status TEXT DEFAULT 'pending', result_detail TEXT, settled_at TEXT, updated_at TEXT,
    UNIQUE (target_date, event_id, market, selection, line, player_id)
);

CREATE TABLE IF NOT EXISTS combos (
    combo_id INTEGER PRIMARY KEY AUTOINCREMENT, target_date TEXT UNIQUE, created_at TEXT, legs TEXT,
    n_legs INTEGER, prob REAL, prob_low REAL, prob_high REAL, fair_odds REAL, est_odds REAL,
    status TEXT DEFAULT 'pending', result_detail TEXT, settled_at TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, event_id TEXT, pick_id INTEGER,
    kind TEXT, severity TEXT, message TEXT, action TEXT
);

-- backtest fuera de muestra (misma estructura que fútbol para reutilizar la calibración)
CREATE TABLE IF NOT EXISTS backtest (
    event_id TEXT, league TEXT, date_utc TEXT, market TEXT, selection TEXT, line REAL, player_id TEXT,
    prob REAL, outcome INTEGER,
    PRIMARY KEY (event_id, market, selection, line, player_id)
);

CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT, notes TEXT);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con

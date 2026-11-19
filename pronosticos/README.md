# AI Football Predictor

Cada noche analiza los partidos del día siguiente y publica **de 0 a 5 pronósticos**: solo los que superan todas las verificaciones. No tiene la obligación de producir pronósticos todos los días.

> Que la probabilidad sea alta no garantiza el acierto. El sistema lo demuestra con un backtest fuera de muestra y con el registro público de todos sus pronósticos, incluidos los fallados y los retirados.

## Los 4 módulos

```
pronosticos/
├── datos/      Módulo 1 · Inteligencia de datos
│   ├── espn.py       API pública de ESPN (gratis, sin clave)
│   └── ingest.py     calendario, resultados, estadísticas, jugadores, alineaciones, cuotas, noticias
├── motor/      Módulo 2 · Motor predictivo (independiente de Claude)
│   ├── models.py     Poisson + Dixon-Coles (goles) · binomial negativa (córners, tarjetas, tiros,
│   │                 tiros a puerta, atajadas) · Elo · sede neutral
│   ├── engine.py     un modelo especializado por mercado, con sus propios parámetros
│   ├── montecarlo.py incertidumbre (intervalo p5–p95) y 10.000 partidos simulados por encuentro
│   └── availability.py  titulares habituales, peso de cada jugador, suspensiones y ajuste por bajas
├── calidad/    Módulo 3 · Control de calidad
│   ├── backtest.py   walk-forward fuera de muestra + simulación del selector
│   ├── calibration.py  Brier, log-loss y curvas de calibración (también validadas fuera de muestra)
│   ├── tuning.py     ajuste de hiperparámetros por mercado
│   ├── danger.py     detector de partidos peligrosos (aprende a decir NO)
│   ├── quality.py    filtro inteligente de calidad + detector de valor en las cuotas
│   └── review.py     triple verificación: cola y veredictos del analista (Claude)
├── app/        Módulo 4 · Aplicación
│   ├── run_daily.py  fase preliminar (noche) · fase actualizada (alineaciones) · revisión · liquidación
│   └── export.py     informes JSON para el tablero
└── web/        tablero para PC y celular (instalable)
```

## Cómo se aplican las 12 mejoras

| # | Mejora | Implementación |
|---|---|---|
| 1 | Triple verificación | (1) Modelo Poisson/binomial negativa con control Elo, (2) mercado (probabilidad sin margen) y (3) analista Claude con `ANALISTA.md`. Si hay discrepancia importante, el pronóstico se descarta o queda en "revisar". |
| 2 | Partidos peligrosos | Se bloquean por: amistosos, datos insuficientes, cambio de entrenador en las noticias, 4 o más titulares fuera, movimiento brusco de cuotas, partido aplazado o 2 o más advertencias juntas. Son advertencias el rendimiento irregular, un cambio reciente de forma, noticias de lesiones y suspensiones probables. |
| 3 | Alineaciones | Fase 1 (preliminar) la noche anterior. Fase 2 cada hora: con las alineaciones oficiales se recalcula según el peso de cada titular ausente, y el pronóstico se actualiza o se retira. |
| 4 | Modelos por mercado | Un modelo por estadística, con vida media y previa elegidas por verosimilitud fuera de muestra (`tuning.py`). Los mercados que rinden peor de lo anunciado se desactivan solos. |
| 5 | Valor en las cuotas | Probabilidad del sistema, probabilidad implícita, cuota, margen de la casa, valor esperado y valor esperado en el peor caso del intervalo. |
| 6 | Memoria | La tabla `predictions` guarda todas las probabilidades calculadas, con modelo, versión, fase y resultado real. La tabla `picks` guarda los pronósticos publicados, con cuota inicial y final, probabilidad inicial y final, calidad, revisión y resultado. |
| 7 | Monte Carlo | Simulación de parámetros (incertidumbre) y 10.000 marcadores por partido (escenarios más probables). |
| 8 | Última hora | La fase 2 vigila alineaciones, suspensiones, cuotas, aplazamientos y noticias. Cada cambio queda en la tabla `alerts`. |
| 9 | Transparencia | Cada pronóstico muestra su probabilidad, intervalo, calidad de datos (0–100), la lista del filtro y sus razones. |
| 10 | Rendimiento real | Efectividad general y por liga, mercado, modelo, fase, calidad y riesgo. Además: rentabilidad simulada, evolución, Brier, calibración y todos los aciertos y fallos. |
| 11 | Filtro de calidad | 9 comprobaciones: datos, calibración, probabilidad, incertidumbre, contradicciones, valor, riesgos, alineaciones y revisión, más una puntuación mínima de 70. |
| 12 | 4 módulos | Datos, motor, calidad y aplicación (ver arriba). |

## Competiciones

- **Ligas:** Premier League, LaLiga, Serie A, Bundesliga, Ligue 1, Eredivisie, Primeira Liga, Liga MX, Brasileirão, Argentina y Colombia.
- **Copas de clubes:** Champions, Europa League, Conference League, Libertadores y Sudamericana. Se analizan con un modelo conjunto de todos los clubes que conecta ligas de distintos países.
- **Selecciones:** Nations League (UEFA y CONCACAF), eliminatorias UEFA y CONMEBOL, Mundial, Eurocopa, Copa América y amistosos. Usan un modelo internacional con sede neutral en los torneos. Los amistosos se analizan pero no se publican.

Para añadir más competiciones, edita `COMPETITIONS` en `config.py` con el código de ESPN.

## Uso

```bash
python -m pronosticos.backfill               # una vez: historial
python -m pronosticos.tuning                 # parámetros de cada modelo
python -m pronosticos.backtest               # validación fuera de muestra
python -m pronosticos.run_daily              # fase 1: análisis preliminar de mañana
python -m pronosticos.run_daily --update     # fase 2: alineaciones y cambios de última hora
python -m pronosticos.run_daily --review     # aplica los veredictos del analista
python -m pronosticos.run_daily --settle     # liquida resultados
scripts\serve.bat                            # tablero en http://localhost:8765
```

## Automatización

| Cuándo | Qué hace |
|---|---|
| 22:00 diario | `run_nightly.bat`: fase 1, más tuning y backtest los domingos |
| 22:30 diario | Tarea de Claude con `ANALISTA.md`: verificación 3 |
| Cada hora, 08–22 | `check_lineups.bat`: fase 2 |

Hay dos formas de dejarlo programado: tareas de Windows (`scripts/register_tasks.ps1`) o GitHub Actions con GitHub Pages (`.github/workflows/pronosticos.yml`). La revisión de Claude se hace con una tarea programada de la app de Claude, sin coste de API.

## Limitaciones honestas (fuentes gratuitas)

- **Lesiones:** no hay un parte de lesiones gratuito. Se cubren con noticias, alineaciones confirmadas, suspensiones por tarjeta roja y movimientos de cuotas.
- **Cuotas:** solo 1X2 y la línea principal de goles (DraftKings vía ESPN). Los demás mercados exigen más probabilidad.
- **Árbitro, clima y xG:** no están disponibles gratis. El modelo usa goles, tiros y tiros a puerta.
- **Mercados de jugador** (disparos, atajadas de un portero concreto): el motor ya calcula el peso de cada jugador. Publicarlos exige la alineación confirmada, y queda como siguiente paso.
- **football-data.co.uk** está bloqueado por los proveedores de internet en Colombia.

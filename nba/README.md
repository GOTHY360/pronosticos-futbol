# AI NBA Predictor

Misma filosofía que la app de fútbol: motor matemático independiente, validación fuera de muestra, filtro de calidad, cuotas entre **1.35 y 1.90** con **cuota mínima para apostar**, de 0 a 5 pronósticos al día, súper combinada, revisión del analista (Claude) y registro de todo. **Todos los mercados incluyen la prórroga.**

## Mercados
| Equipos | Jugadores (prórroga incluida) |
|---|---|
| Ganador | Puntos |
| Hándicap | Triples anotados |
| Total de puntos (más / menos) | Rebotes |
| Puntos de cada equipo | Asistencias |
| | Puntos + rebotes + asistencias (PRA) |
| | Puntos + rebotes · Puntos + asistencias · Rebotes + asistencias |
| | Robos + tapones · Doble-doble |

## Datos (gratis, ESPN)
- Box score de cada partido: minutos, puntos, triples, rebotes, asistencias, robos, tapones, pérdidas, **faltas personales**, +/−, titular o suplente, **motivo de no jugar** (descanso, decisión del entrenador, lesión) y **expulsiones**.
- Equipo: posesiones, **técnicas y flagrantes**, cuartos jugados (de ahí sale la **prórroga**).
- **Parte oficial de lesiones** de toda la liga: fuera, dudoso, en duda, tipo de lesión y fecha estimada de regreso. Se actualiza cada hora en días de partido.
- Cuotas de ganador, hándicap y total (DraftKings). Los props de jugador no tienen cuota gratuita, por eso cada uno indica su cuota mínima.

## Modelo
**Equipos.** Puntos esperados = base · localía · ataque · defensa del rival, ponderados en el tiempo (vida media de 60 días). Encima se suman **efectos de fatiga aprendidos de los datos** mediante regresión sobre los residuos: noches consecutivas, 3 partidos en 4 noches, 3 o más días de descanso, gira de 4 o más partidos fuera, altitud (Denver y Utah), y descanso o fatiga del rival. El margen y el total siguen una distribución normal bivariada. Como control independiente se usa un Elo.

**Jugadores.**
1. **Minutos proyectados:** media ponderada de los partidos recientes, con estos ajustes:
   - −1 minuto en noches consecutivas para quienes juegan 30 minutos o más.
   - Menos minutos para los titulares cuando se espera una **paliza**.
   - Reparto de los **minutos que dejan libres los compañeros fuera o en duda**.
   - Más incertidumbre para los jugadores **propensos a faltas**.
2. **Tasas por minuto** con encogimiento hacia la media de la liga, ajustadas por el **ritmo** del partido, la **defensa del rival** y el **aumento de uso** cuando faltan compañeros.
3. **Prórroga:** se estima su probabilidad a partir del margen esperado y añade minutos.
4. **Monte Carlo:** 3.000 simulaciones por jugador (minutos inciertos × binomial negativa), más dos simulaciones extra (pesimista y optimista) para el intervalo de incertidumbre.

## Filtro de calidad y detector de riesgos
- **Bloquean los props de un jugador:** figurar en el parte de lesiones (cualquier estado), tener menos de 8 partidos, proyectar menos de 20 minutos o tener minutos muy volátiles.
- **Bloquean solo los "más de":**
  - **Gestión de carga:** noche consecutiva y el jugador ya descansó en otras.
  - **Riesgo de paliza:** hándicap de 12 o más puntos (minutos basura).
- **Advierten (dos advertencias bloquean):**
  - **Problemas de faltas:** 5 o más por 36 minutos.
  - **Expulsiones** en la temporada.
  - 3 o más jugadores de rotación fuera en un equipo.
  - Varios jugadores en duda.
  - Movimientos bruscos de cuotas.
- **Fase 2, cada hora:**
  - Se retira el pronóstico si el jugador aparece de pronto en el parte de lesiones.
  - Se retira si un titular habitual no figura entre los titulares confirmados.
  - Si nada de eso ocurre, se recalcula con los titulares confirmados.

## Uso
```bash
python -m nba.backfill            # una vez: historial 2024-25 en adelante (~30 min)
python -m nba.backtest            # validación fuera de muestra
python -m nba.run_daily           # análisis de mañana
python -m nba.run_daily --update  # lesiones, titulares y cuotas de última hora
python -m nba.run_daily --review  # aplica los veredictos del analista
```
El tablero está en `http://localhost:8765/nba/` (en la nube, `…/pronosticos-futbol/nba/`). Funciona con las mismas tareas programadas, el mismo bot de Telegram y la misma revisión del analista que la app de fútbol.

# Instrucciones del analista (verificación 3 de 3)

Las ejecuta cada noche una tarea programada de Claude, después del análisis matemático. El analista **no calcula probabilidades**: el motor estadístico es la fuente. Su trabajo es **cuestionar** los pronósticos.

## Pasos

1. Abre `pronosticos/data/revision/cola_<FECHA>.json`, donde FECHA es mañana en hora de Colombia. Si no existe, no hay nada que revisar.
2. Revisa cada pronóstico con este criterio:
   - **Contradicciones:** ¿el Elo, el mercado (`prob_mercado`, `cuota`) y el modelo cuentan historias distintas? ¿Las razones del modelo apoyan de verdad la selección?
   - **Noticias** (`noticias_local`, `noticias_visitante`): lesiones de jugadores clave, sanciones, cambio de entrenador, crisis interna, viajes largos.
   - **Motivación:** partido sin nada en juego, rotaciones por la Champions o la Copa, final de temporada, partido de vuelta con la eliminatoria ya resuelta, clásicos o derbis (más tarjetas).
   - **Exceso de optimismo:** intervalo Monte Carlo muy ancho, pocos datos o alertas de riesgo.
   - Si tienes acceso a la web, puedes comprobar noticias de última hora de ambos equipos.
3. Escribe `pronosticos/data/revision/veredicto_<FECHA>.json` con este formato:

```json
[
  {"pick_id": 12, "verdict": "aprobar", "notes": "Sin bajas relevantes; modelo y mercado coinciden."},
  {"pick_id": 13, "verdict": "rechazar", "notes": "El delantero titular está lesionado (noticia del 19/11)."},
  {"pick_id": 14, "verdict": "revisar", "notes": "Posibles rotaciones antes de la Champions; esperar alineación."}
]
```

   - `aprobar`: el pronóstico se mantiene.
   - `revisar`: no se recomienda hasta que se confirmen las alineaciones. Si llega el partido sin resolverse, se retira.
   - `rechazar`: se retira antes del partido. Queda registrado como retirado.
   - Ante la duda, elige `revisar` o `rechazar`. No pronosticar también es una buena decisión.
4. Ejecuta `python -m pronosticos.run_daily --review` para aplicar los veredictos y actualizar el tablero.
5. Resume en 3 a 5 líneas qué aprobaste, qué retiraste y por qué.

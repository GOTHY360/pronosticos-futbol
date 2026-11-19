"""Triple verificación: (1) modelos estadísticos, (2) mercado de apuestas y (3) revisión del analista.

El analista (Claude) no calcula probabilidades: lee la cola de revisión, busca contradicciones,
noticias de última hora y motivos para dudar, y emite un veredicto por pronóstico:
  aprobar  · se mantiene
  revisar  · queda "pendiente de revisión" (no se recomienda hasta resolverse; si llega el partido, se retira)
  rechazar · se retira antes del partido (queda registrado como retirado)

Intercambio por archivos en data/revision/ (funciona con una tarea programada de Claude, sin coste de API):
  cola_YYYY-MM-DD.json      → lo genera el sistema
  veredicto_YYYY-MM-DD.json → lo escribe el analista: [{"pick_id": 1, "verdict": "aprobar", "notes": "..."}]
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..config import REVIEW_DIR
from .danger import team_patterns

INSTRUCTIONS = (
    "Eres el analista de control de calidad. NO recalcules probabilidades: el modelo matemático es la fuente. "
    "Para cada pronóstico revisa: (a) contradicciones entre modelo, Elo y mercado; (b) noticias de lesiones, "
    "sanciones o cambio de entrenador; (c) motivación (calendario, rotaciones, partido sin nada en juego, "
    "eliminatoria); (d) si la probabilidad parece demasiado optimista para el contexto. Cuestiona la predicción. "
    "Ante la duda, 'revisar' o 'rechazar'. Responde SOLO con un JSON: "
    '[{"pick_id": <int>, "verdict": "aprobar|revisar|rechazar", "notes": "<motivo breve en español>"}]'
)


def _news_for(con, name: str, days: int = 14) -> list[str]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    pats = team_patterns(con, name)
    out = []
    for r in con.execute("SELECT published, headline FROM news WHERE published >= ? ORDER BY published DESC", (since,)):
        if any(p.search((r["headline"] or "").lower()) for p in pats):
            out.append(f"{(r['published'] or '')[:10]} · {r['headline']}")
    return out[:8]


def write_queue(con, target: str, contexts: dict) -> str | None:
    picks = [dict(r) for r in con.execute(
        "SELECT * FROM picks WHERE target_date=? AND status='pending' ORDER BY prob DESC", (target,))]
    if not picks:
        return None
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for p in picks:
        ctx = contexts.get(p["event_id"], {})
        home, away = p["match_name"].split(" vs ", 1) if " vs " in p["match_name"] else (p["match_name"], "")
        items.append({
            "pick_id": p["pick_id"], "partido": p["match_name"], "competicion": p["league"],
            "inicio_utc": p["kickoff_utc"], "pronostico": p["label"], "probabilidad": round(p["prob"], 3),
            "intervalo": [round(p["prob_low"] or 0, 3), round(p["prob_high"] or 0, 3)],
            "prob_mercado": p["market_prob"], "cuota": p["odds_dec"], "valor_esperado": p["edge"],
            "fase": p["phase"], "calidad": p["quality_score"],
            "comprobaciones": json.loads(p["quality_checks"] or "[]"),
            "razones_del_modelo": json.loads(p["reasons"] or "[]"),
            "elo_1x2": ctx.get("elo"), "alertas": ctx.get("flags", []),
            "noticias_local": _news_for(con, home), "noticias_visitante": _news_for(con, away),
        })
    path = REVIEW_DIR / f"cola_{target}.json"
    path.write_text(json.dumps({"fecha": target, "instrucciones": INSTRUCTIONS, "pronosticos": items},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return str(path)


def apply_verdicts(con, target: str, log=print) -> int:
    path = REVIEW_DIR / f"veredicto_{target}.json"
    if not path.exists():
        return 0
    try:
        verdicts = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        log(f"  ! veredicto ilegible: {e}")
        return 0
    if isinstance(verdicts, dict):
        verdicts = verdicts.get("veredictos") or verdicts.get("verdicts") or []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for v in verdicts:
        try:
            pid, verdict = int(v["pick_id"]), str(v["verdict"]).lower().strip()
        except (KeyError, ValueError, TypeError):
            continue
        notes = str(v.get("notes", ""))[:500]
        row = con.execute("SELECT status, review_status FROM picks WHERE pick_id=? AND target_date=?",
                          (pid, target)).fetchone()
        if not row or row["status"] != "pending":
            continue
        if verdict == "rechazar":
            con.execute("""UPDATE picks SET status='withdrawn', review_status='rechazado', review_notes=?,
                           result_detail=?, updated_at=? WHERE pick_id=?""",
                        (notes, "Retirado por la revisión del analista: " + notes, now, pid))
            con.execute("INSERT INTO alerts(created_at, pick_id, kind, severity, message, action) VALUES (?,?,?,?,?,?)",
                        (now, pid, "revision", "alta", notes, "retirado"))
        elif verdict == "revisar":
            con.execute("UPDATE picks SET review_status='revisar', review_notes=?, updated_at=? WHERE pick_id=?",
                        (notes, now, pid))
        elif verdict == "aprobar":
            con.execute("UPDATE picks SET review_status='aprobado', review_notes=?, updated_at=? WHERE pick_id=?",
                        (notes, now, pid))
        else:
            continue
        n += 1
    con.commit()
    from .combo import prune
    prune(con, target)
    log(f"  veredictos del analista aplicados: {n}")
    return n

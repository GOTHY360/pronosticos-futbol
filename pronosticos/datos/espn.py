"""Cliente mínimo para la API pública de ESPN (sin clave)."""
import gzip
import json
import time
import urllib.error
import urllib.request

from ..config import CACHE_DIR, HTTP

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
_last_call = [0.0]


def _get(url: str) -> dict:
    for attempt in range(HTTP["retries"]):
        wait = HTTP["delay_s"] - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (pronosticos-personal)",
                "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=HTTP["timeout_s"]) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            if attempt == HTTP["retries"] - 1:
                raise RuntimeError(f"ESPN no respondió: {url} ({e})")
            time.sleep(2 * (attempt + 1))
    return {}


def scoreboard(league: str, dates: str) -> list[dict]:
    """dates: 'YYYYMMDD' o 'YYYYMM' (mes completo)."""
    d = _get(f"{BASE}/{league}/scoreboard?dates={dates}&limit=500")
    return d.get("events", [])


def news(league: str, limit: int = 30) -> list[dict]:
    return _get(f"{BASE}/{league}/news?limit={limit}").get("articles", [])


def summary(league: str, event_id: str, use_cache: bool = True) -> dict:
    """Resumen completo del partido. Se cachea en disco sólo si ya terminó."""
    path = CACHE_DIR / league / f"{event_id}.json"
    if use_cache and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    d = _get(f"{BASE}/{league}/summary?event={event_id}")
    status = (d.get("header", {}).get("competitions") or [{}])[0].get("status", {}).get("type", {})
    if status.get("completed"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d), encoding="utf-8")
    return d

"""Servidor del tablero sin ventana (pythonw). http.server necesita un stderr válido para registrar
cada petición; con pythonw no existe, así que se redirige a un archivo de log."""
import functools
import http.server
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")
LOGS = os.path.join(os.path.dirname(HERE), "data", "logs")
os.makedirs(LOGS, exist_ok=True)
sys.stdout = sys.stderr = open(os.path.join(LOGS, "server.log"), "a", encoding="utf-8", buffering=1)

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=WEB)
http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()

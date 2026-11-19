@echo off
REM Sirve el tablero en http://localhost:8765 (y en la red local para abrirlo desde el celular).
cd /d "%~dp0..\web"
echo Abre en el PC:      http://localhost:8765
echo En el celular (misma red Wi-Fi): http://IP-DE-ESTE-PC:8765   (ver IP con: ipconfig)
python -m http.server 8765 --bind 0.0.0.0

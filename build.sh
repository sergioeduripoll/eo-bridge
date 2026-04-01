#!/bin/bash
# build.sh — Script de build para Render
# Garantiza el orden correcto de instalación de dependencias

set -e

echo "=== [BUILD] Limpiando conflictos de websocket ==="
pip uninstall -y websocket 2>/dev/null || true
pip uninstall -y websocket-client 2>/dev/null || true

echo "=== [BUILD] Instalando websocket-client primero ==="
pip install websocket-client==1.7.0

echo "=== [BUILD] Instalando ExpertOptionAPI ==="
pip install ExpertOptionAPI --no-deps
pip install requests>=2.31.0 simplejson

echo "=== [BUILD] Instalando Flask + Gunicorn ==="
pip install Flask==3.0.3 gunicorn==22.0.0

echo "=== [BUILD] Verificando módulos ==="
python3 -c "import websocket; print(f'websocket: {websocket.__file__}')"
python3 -c "from expert import EoApi; print('EoApi: OK')" 2>/dev/null || \
python3 -c "from ExpertOptionAPI.expert import EoApi; print('EoApi (alt): OK')" 2>/dev/null || \
echo "⚠️ EoApi import fallará — ver debug en app.py"

echo "=== [BUILD] pip list ==="
pip list | grep -i -E "expert|websocket|flask|gunicorn"

echo "=== [BUILD] Completo ==="

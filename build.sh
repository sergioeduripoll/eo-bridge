#!/bin/bash
# build.sh — Script de construcción "Sniper"
# Limpia, ordena e instala todas las piezas manuales de ExpertOptionAPI

set -e

echo "=== [BUILD] 1. Limpiando conflictos de websocket ==="
pip uninstall -y websocket 2>/dev/null || true
pip uninstall -y websocket-client 2>/dev/null || true

echo "=== [BUILD] 2. Instalando motor de comunicación (el bueno) ==="
pip install websocket-client==1.7.0

echo "=== [BUILD] 3. Instalando piezas del motor y servidor ==="
# Agregamos 'pause', que fue el que nos detuvo en el último intento
pip install Flask==3.0.3 gunicorn==22.0.0 requests>=2.31.0 simplejson pause

echo "=== [BUILD] 4. Instalando ExpertOptionAPI (la carrocería) ==="
pip install ExpertOptionAPI --no-deps

echo "=== [BUILD] 5. Verificando que todo encaje ==="
python3 -c "import websocket; print('✅ WebSocketApp: OK')"
python3 -c "import simplejson; print('✅ simplejson: OK')"
python3 -c "import pause; print('✅ pause: OK')"

echo "=== [BUILD] 6. Resumen de instalación ==="
pip list | grep -i -E "expert|websocket|flask|gunicorn|simplejson|pause"

echo "=== [BUILD] ¡Todo listo para arrancar! ==="

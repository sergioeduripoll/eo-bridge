#!/bin/bash
# build.sh — Script de build para Render
# Garantiza el orden correcto de instalación de dependencias y limpieza de conflictos

set -e

echo "=== [BUILD] 1. Limpiando conflictos de websocket ==="
pip uninstall -y websocket 2>/dev/null || true
pip uninstall -y websocket-client 2>/dev/null || true

echo "=== [BUILD] 2. Instalando motor de comunicación (el bueno) ==="
pip install websocket-client==1.7.0

echo "=== [BUILD] 3. Instalando dependencias de servidor ==="
pip install Flask==3.0.3 gunicorn==22.0.0 requests>=2.31.0 simplejson

echo "=== [BUILD] 4. Instalando ExpertOptionAPI sin pisar nada ==="
# Usamos --no-deps para que no intente bajar el paquete 'websocket' roto
pip install ExpertOptionAPI --no-deps

echo "=== [BUILD] 5. Verificando integridad de módulos ==="
python3 -c "import websocket; print(f'✅ websocket-client: {websocket.__file__}')"
python3 -c "import simplejson; print('✅ simplejson: OK')"

# Intentar detectar la ruta de importación de la API
python3 -c "from expert import EoApi; print('✅ EoApi (standard): OK')" 2>/dev/null || \
python3 -c "from ExpertOptionAPI.expert import EoApi; print('✅ EoApi (ExpertOptionAPI.expert): OK')" 2>/dev/null || \
echo "⚠️ Advertencia: No se pudo importar EoApi en build, se reintentará en app.py"

echo "=== [BUILD] 6. Resumen de paquetes instalados ==="
pip list | grep -i -E "expert|websocket|flask|gunicorn|simplejson"

echo "=== [BUILD] ¡Proceso completo! ==="

"""
app.py — Puente ExpertOption LAZY para Render Free (512MB)
Conecta SOLO cuando llega un trade, ejecuta, y desconecta.
No mantiene WebSocket abierto → mínimo uso de RAM.
"""

import os
import sys
import time
import threading
import subprocess

# ═══════════════════════════════════════════════════════════════════
# SAFETY: Instalar dependencias faltantes
# ═══════════════════════════════════════════════════════════════════

def ensure_deps():
    deps = ['pause', 'simplejson', 'websocket']
    missing = []
    for dep in deps:
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)
    if missing:
        print(f"[SAFETY] Instalando: {missing}")
        pkg_map = {'websocket': 'websocket-client==1.7.0', 'pause': 'pause', 'simplejson': 'simplejson'}
        for m in missing:
            subprocess.run([sys.executable, '-m', 'pip', 'install', pkg_map.get(m, m), '-q'], check=False)

ensure_deps()

from flask import Flask, request, jsonify

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════════
# IMPORT EoApi (4 intentos)
# ═══════════════════════════════════════════════════════════════════

EoApi = None
for import_path in ['expert', 'ExpertOptionAPI.expert', 'expert.api']:
    try:
        mod = __import__(import_path, fromlist=['EoApi'])
        EoApi = getattr(mod, 'EoApi')
        print(f"[BRIDGE] ✅ EoApi desde {import_path}")
        break
    except (ImportError, AttributeError):
        continue

if EoApi is None:
    print("[BRIDGE] ❌ EoApi no disponible")

# ═══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════

TOKEN = os.environ.get("EO_TOKEN", "")
SERVER = os.environ.get("EO_SERVER", "wss://fr24g1eu.expertoption.com/")
API_SECRET = os.environ.get("BRIDGE_SECRET", "cambiar_esto")
AMOUNT = int(os.environ.get("TRADE_AMOUNT", "10"))
EXP_TIME = int(os.environ.get("EXP_TIME", "60"))

ASSET_MAP = {
    "BTC/USD":  160,
    "ETH/USD":  162,
    "XRP/USD":  173,
    "ADA/USD":  235,
    "SOL/USD":  464,
    "DOGE/USD": 463,
    "BNB/USD":  462
}

# ═══════════════════════════════════════════════════════════════════
# CONEXIÓN LAZY: Conecta → Ejecuta → Desconecta
# No mantiene WebSocket abierto → mínimo RAM
# ═══════════════════════════════════════════════════════════════════

def execute_trade(asset_str, direction):
    """Conecta, opera, desconecta. Todo en una llamada."""
    if EoApi is None:
        return {"status": "error", "message": "EoApi no cargado"}
    if not TOKEN:
        return {"status": "error", "message": "EO_TOKEN no configurado"}

    eo_type = "call" if direction == "BUY" else "put"
    asset_id = ASSET_MAP.get(asset_str)
    if asset_id is None:
        return {"status": "error", "message": f"Activo no disponible: {asset_str}"}

    expert = None
    try:
        print(f"[TRADE] 🔌 Conectando...")
        expert = EoApi(token=TOKEN, server_region=SERVER)
        expert.connect()
        time.sleep(2)

        print(f"[TRADE] 🔄 SetDemo...")
        expert.SetDemo()
        time.sleep(0.5)

        print(f"[TRADE] 🎯 {eo_type.upper()} {asset_str} (ID:{asset_id}) ${AMOUNT}")
        result = expert.Buy(
            amount=AMOUNT,
            type=eo_type,
            assetid=asset_id,
            exptime=EXP_TIME,
            isdemo=1,
            strike_time=time.time()
        )
        print(f"[TRADE] ✅ Respuesta: {result}")

        return {
            "status": "success",
            "asset": asset_str,
            "direction": direction,
            "type": eo_type,
            "asset_id": asset_id,
            "amount": AMOUNT
        }

    except Exception as e:
        print(f"[TRADE] ❌ Error: {e}")
        return {"status": "error", "message": str(e)}

    finally:
        # SIEMPRE desconectar para liberar RAM
        if expert is not None:
            try:
                if hasattr(expert, 'websocket_client') and expert.websocket_client:
                    expert.websocket_client.close()
                print(f"[TRADE] 🔌 Desconectado")
            except Exception:
                pass

# ═══════════════════════════════════════════════════════════════════
# KEEP-ALIVE: Solo ping HTTP (no WS)
# ═══════════════════════════════════════════════════════════════════

def keep_alive():
    import urllib.request
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    while True:
        time.sleep(600)
        if render_url:
            try:
                urllib.request.urlopen(f"{render_url}/health", timeout=10)
                print("[KEEP-ALIVE] OK")
            except Exception:
                pass

threading.Thread(target=keep_alive, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════

def check_auth():
    auth = request.headers.get('Authorization', '')
    return auth.startswith('Bearer ') and auth.split(' ', 1)[1] == API_SECRET

# ═══════════════════════════════════════════════════════════════════
# RUTAS
# ═══════════════════════════════════════════════════════════════════

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "mode": "LAZY_DEMO", "eoapi": EoApi is not None}), 200

@app.route('/broker-status', methods=['GET'])
def broker_status():
    if not check_auth():
        return jsonify({"status": "error", "message": "No autorizado"}), 401
    return jsonify({
        "status": "ready" if EoApi and TOKEN else "not_configured",
        "connected": bool(EoApi and TOKEN),
        "mode": "LAZY_DEMO",
        "assets": list(ASSET_MAP.keys())
    }), 200

@app.route('/trade', methods=['POST'])
def trade():
    if not check_auth():
        return jsonify({"status": "error", "message": "No autorizado"}), 401

    data = request.json
    if not data:
        return jsonify({"status": "error", "message": "Body vacío"}), 400

    asset_str = data.get('asset', '')
    direction = data.get('direction', '').upper()

    if direction not in ('BUY', 'SELL'):
        return jsonify({"status": "error", "message": f"Dirección inválida: {direction}"}), 400

    print(f"[TRADE] ━━━ {asset_str} {direction} ━━━")
    result = execute_trade(asset_str, direction)
    print(f"[TRADE] ━━━ {result['status']} ━━━")

    code = 200 if result["status"] == "success" else 500
    return jsonify(result), code

# ═══════════════════════════════════════════════════════════════════
# STARTUP — NO conecta (lazy)
# ═══════════════════════════════════════════════════════════════════

print(f"[BRIDGE] Modo LAZY | EoApi: {'OK' if EoApi else 'NO'} | Token: {'OK' if TOKEN else 'NO'}")
print(f"[BRIDGE] Assets: {list(ASSET_MAP.keys())}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

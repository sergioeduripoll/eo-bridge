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

def kill_expert(expert):
    """Cierre agresivo: WebSocket + hilos de la librería."""
    if expert is None:
        return
    try:
        if hasattr(expert, 'websocket_client') and expert.websocket_client:
            expert.websocket_client.close()
    except Exception:
        pass
    # Matar hilos de la librería (ping_thread, websocket_thread)
    for attr in ['websocket_thread', 'ping_thread']:
        try:
            t = getattr(expert, attr, None)
            if t and hasattr(t, 'is_alive') and t.is_alive():
                t.join(timeout=1)
        except Exception:
            pass

def wait_for_ws(expert, timeout=8):
    """Espera hasta que el WebSocket esté realmente conectado."""
    import time as _time
    start = _time.time()
    while _time.time() - start < timeout:
        try:
            ws = getattr(expert, 'websocket_client', None)
            if ws and hasattr(ws, 'sock') and ws.sock and ws.sock.connected:
                return True
        except Exception:
            pass
        _time.sleep(0.5)
    return False

def execute_trade(asset_str, direction, tf='5M'):
    """Conecta, opera, desconecta. Todo en una llamada."""
    if EoApi is None:
        return {"status": "error", "message": "EoApi no cargado"}
    if not TOKEN:
        return {"status": "error", "message": "EO_TOKEN no configurado"}

    eo_type = "call" if direction == "BUY" else "put"
    asset_id = ASSET_MAP.get(asset_str)
    if asset_id is None:
        return {"status": "error", "message": f"Activo no disponible: {asset_str}"}

    TF_TO_EXP = {'5M': 300, '15M': 900, '30M': 1800, '1H': 3600}
    exp_time = TF_TO_EXP.get(tf, 300)

    expert = None
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"[TRADE] 🔌 Conectando (intento {attempt + 1}/{max_retries})...")
            expert = EoApi(token=TOKEN, server_region=SERVER)
            expert.connect()

            # Esperar a que el WS esté realmente conectado
            if wait_for_ws(expert, timeout=10):
                print(f"[TRADE] ✅ WebSocket conectado")
            else:
                print(f"[TRADE] ⚠️ WS no verificado, intentando operar igual...")

            expert.SetDemo()
            time.sleep(1)

            print(f"[TRADE] 🎯 {eo_type.upper()} {asset_str} (ID:{asset_id}) ${AMOUNT} exp:{exp_time}s ({tf})")
            result = expert.Buy(
                amount=AMOUNT,
                type=eo_type,
                assetid=asset_id,
                exptime=exp_time,
                isdemo=1,
                strike_time=time.time()
            )
            print(f"[TRADE] ✅ Respuesta: {result}")

            kill_expert(expert)
            expert = None
            print(f"[TRADE] 🔌 Desconectado")

            return {
                "status": "success",
                "asset": asset_str,
                "direction": direction,
                "type": eo_type,
                "asset_id": asset_id,
                "amount": AMOUNT,
                "tf": tf,
                "exp_seconds": exp_time
            }

        except Exception as e:
            print(f"[TRADE] ❌ Intento {attempt + 1} falló: {e}")
            kill_expert(expert)
            expert = None

            if attempt < max_retries - 1:
                wait = 3 + (attempt * 2)  # 3s, 5s
                print(f"[TRADE] 🔄 Reintentando en {wait}s...")
                time.sleep(wait)
            else:
                return {"status": "error", "message": str(e)}

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
    tf = data.get('tf', '5M')

    if direction not in ('BUY', 'SELL'):
        return jsonify({"status": "error", "message": f"Dirección inválida: {direction}"}), 400

    print(f"[TRADE] ━━━ {asset_str} {direction} {tf} ━━━")
    result = execute_trade(asset_str, direction, tf)
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

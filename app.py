"""
app.py — Puente ExpertOption para Render/Cloud
Con autenticación, reconexión automática y keep-alive.
"""

import os
import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN (usar variables de entorno en Render)
# ═══════════════════════════════════════════════════════════════════

TOKEN = os.environ.get("EO_TOKEN", "")
SERVER = os.environ.get("EO_SERVER", "wss://fr24g1eu.expertoption.com/")
# Clave secreta para autenticar requests del bot Node.js
API_SECRET = os.environ.get("BRIDGE_SECRET", "cambiar_esto_por_clave_segura")
# Monto por operación (USD demo)
AMOUNT = int(os.environ.get("TRADE_AMOUNT", "10"))
# Expiración en segundos
EXP_TIME = int(os.environ.get("EXP_TIME", "60"))

ASSET_MAP = {
    "BTC/USD":  240,
    "ETH/USD":  241,
    "XRP/USD":  243,
    "ADA/USD":  244,
    "SOL/USD":  245,
    "DOGE/USD": 246,
    "BNB/USD":  247
}

# ═══════════════════════════════════════════════════════════════════
# CONEXIÓN A EXPERTOPTION (con reconexión automática)
# ═══════════════════════════════════════════════════════════════════

expert = None

def connect_expert():
    """Conecta a ExpertOption en modo DEMO."""
    global expert
    try:
        import ExpertOptionAPI
        from ExpertOptionAPI.expert import EoApi as ExpertAPI
        print("[BRIDGE] Conectando a ExpertOption...")
        expert = ExpertAPI(token=TOKEN, server_region=SERVER)
        expert.connect()
        expert.SetDemo()
        print("[BRIDGE] ✅ Conectado a ExpertOption DEMO")
        return True
    except Exception as e:
        print(f"[BRIDGE] ❌ Error de conexión: {e}")
        expert = None
        return False

def ensure_connection():
    """Reconecta si es necesario. Retorna True si hay conexión."""
    global expert
    if expert is not None:
        return True
    return connect_expert()

# ═══════════════════════════════════════════════════════════════════
# KEEP-ALIVE: Evita que Render duerma la instancia
# Hace un self-ping cada 10 minutos
# ═══════════════════════════════════════════════════════════════════

def keep_alive():
    """Ping periódico para mantener la instancia activa en Render free."""
    import urllib.request
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    while True:
        time.sleep(600)  # 10 minutos
        if render_url:
            try:
                urllib.request.urlopen(f"{render_url}/health", timeout=10)
                print("[KEEP-ALIVE] Ping OK")
            except Exception:
                pass
        # Reconectar ExpertOption si se cayó
        ensure_connection()

# Arrancar keep-alive en background
keepalive_thread = threading.Thread(target=keep_alive, daemon=True)
keepalive_thread.start()

# ═══════════════════════════════════════════════════════════════════
# MIDDLEWARE DE AUTENTICACIÓN
# ═══════════════════════════════════════════════════════════════════

def check_auth():
    """Verifica header Authorization: Bearer <secret>."""
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer ') or auth.split(' ', 1)[1] != API_SECRET:
        return False
    return True

# ═══════════════════════════════════════════════════════════════════
# RUTAS
# ═══════════════════════════════════════════════════════════════════

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "connected": expert is not None,
        "mode": "DEMO"
    }), 200

@app.route('/trade', methods=['POST'])
def trade():
    # Autenticación
    if not check_auth():
        return jsonify({"status": "error", "message": "No autorizado"}), 401

    data = request.json
    if not data:
        return jsonify({"status": "error", "message": "Body vacío"}), 400

    asset_str = data.get('asset', '')
    direction = data.get('direction', '').upper()

    if direction not in ('BUY', 'SELL'):
        return jsonify({"status": "error", "message": f"Dirección inválida: {direction}"}), 400

    eo_type = "call" if direction == "BUY" else "put"
    asset_id = ASSET_MAP.get(asset_str, 240)

    # Asegurar conexión
    if not ensure_connection():
        return jsonify({"status": "error", "message": "Sin conexión a ExpertOption"}), 503

    try:
        expert.Buy(
            amount=AMOUNT,
            type=eo_type,
            assetid=asset_id,
            exptime=EXP_TIME,
            isdemo=1,
            strike_time=time.time()
        )
        print(f"[BRIDGE] 🎯 {eo_type.upper()} en {asset_str} (ID:{asset_id}) por ${AMOUNT}")
        return jsonify({
            "status": "success",
            "asset": asset_str,
            "direction": direction,
            "type": eo_type,
            "amount": AMOUNT
        }), 200

    except Exception as e:
        print(f"[BRIDGE] ❌ Error: {e}")
        expert = None  # Forzar reconexión en próximo intento
        return jsonify({"status": "error", "message": str(e)}), 500

# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

if TOKEN:
    connect_expert()
else:
    print("[BRIDGE] ⚠️ EO_TOKEN no configurado — arrancar sin conexión")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

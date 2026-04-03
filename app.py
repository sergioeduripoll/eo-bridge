"""
app.py — Puente ExpertOption LAZY v2
Fixes: Dynamic asset mapping, balance reading, robust WS, silent logs
"""

import os
import sys
import time
import json
import threading
import subprocess

# ═══════════════════════════════════════════════════════════════════
# DEPS
# ═══════════════════════════════════════════════════════════════════

def ensure_deps():
    for dep, pkg in [('pause','pause'), ('simplejson','simplejson'), ('websocket','websocket-client==1.7.0')]:
        try:
            __import__(dep)
        except ImportError:
            subprocess.run([sys.executable, '-m', 'pip', 'install', pkg, '-q'], check=False)

ensure_deps()

from flask import Flask, request, jsonify

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════════
# IMPORT EoApi
# ═══════════════════════════════════════════════════════════════════

EoApi = None
for path in ['expert', 'ExpertOptionAPI.expert']:
    try:
        mod = __import__(path, fromlist=['EoApi'])
        EoApi = getattr(mod, 'EoApi')
        print(f"[BRIDGE] ✅ EoApi desde {path}")
        break
    except (ImportError, AttributeError):
        continue

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

TOKEN = os.environ.get("EO_TOKEN", "")
SERVER = os.environ.get("EO_SERVER", "wss://fr24g1eu.expertoption.com/")
API_SECRET = os.environ.get("BRIDGE_SECRET", "cambiar_esto")
AMOUNT = int(os.environ.get("TRADE_AMOUNT", "10"))

# Fallback estático — se usa SOLO si el mapeo dinámico falla
STATIC_ASSET_MAP = {
    "BTC/USD":  160, "ETH/USD":  162, "XRP/USD":  173,
    "ADA/USD":  235, "SOL/USD":  464, "DOGE/USD": 463, "BNB/USD":  462
}

# Símbolos para buscar en assets dinámicos
SYMBOL_SEARCH = {
    "BTC/USD":  ["BTCUSD", "btcusd", "bitcoin"],
    "ETH/USD":  ["ETHUSD", "ethusd", "ethereum"],
    "XRP/USD":  ["XRPUSD", "xrpusd", "ripple"],
    "ADA/USD":  ["ADAUSD", "adausd", "cardano"],
    "SOL/USD":  ["SOLUSD", "solusd", "solana"],
    "DOGE/USD": ["DOGEUSD", "dogeusd", "doge"],
    "BNB/USD":  ["BNBUSD", "bnbusd"],
}

TF_TO_EXP = {'5M': 300, '15M': 900, '30M': 1800, '1H': 3600}

# ═══════════════════════════════════════════════════════════════════
# DYNAMIC ASSET DISCOVERY
# Cache de assets descubiertos del WS
# ═══════════════════════════════════════════════════════════════════

DYNAMIC_ASSETS = {}       # {"BTC/USD": 160, ...} — actualizado por WS
DYNAMIC_ASSETS_TS = 0     # timestamp de última actualización
DYNAMIC_LOCK = threading.Lock()

def capture_assets_from_ws(expert, timeout=6):
    """Escucha mensajes WS por `timeout` segundos y extrae asset IDs activos."""
    global DYNAMIC_ASSETS, DYNAMIC_ASSETS_TS
    found = {}

    try:
        # Buscar en msg_by_action
        if hasattr(expert, 'msg_by_action') and isinstance(expert.msg_by_action, dict):
            for key, val in expert.msg_by_action.items():
                _extract_assets(val, found)

        # Buscar assets como atributo directo
        for attr_name in ['assets', 'instruments']:
            if hasattr(expert, attr_name):
                _extract_assets(getattr(expert, attr_name), found)

    except Exception as e:
        print(f"[ASSETS] ⚠️ Error extrayendo: {e}")

    if found:
        with DYNAMIC_LOCK:
            DYNAMIC_ASSETS = found
            DYNAMIC_ASSETS_TS = time.time()
        print(f"[ASSETS] ✅ Dinámicos: {found}")
    else:
        print(f"[ASSETS] ⚠️ No se encontraron dinámicos, usando estáticos")

def _extract_assets(data, found):
    """Recursivamente extrae assets de estructuras de datos del WS."""
    if data is None:
        return

    # Si es una lista de assets
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                _check_asset_dict(item, found)
    # Si es un dict con assets dentro
    elif isinstance(data, dict):
        if 'id' in data and ('symbol' in data or '_name' in data or 'name' in data):
            _check_asset_dict(data, found)
        # Recurse into values
        for v in data.values():
            if isinstance(v, (list, dict)):
                _extract_assets(v, found)

def _check_asset_dict(item, found):
    """Chequea si un dict es un asset y lo agrega al mapa."""
    asset_id = item.get('id')
    symbol = item.get('symbol', '').upper()
    name = (item.get('_name', '') or item.get('name', '')).lower()
    is_active = item.get('isActive', item.get('is_active', 0))

    if not asset_id:
        return

    for our_name, search_terms in SYMBOL_SEARCH.items():
        for term in search_terms:
            if term.upper() in symbol or term.lower() in name:
                # Preferir activos que están activos; si ya tenemos uno activo, no pisar con inactivo
                if our_name in found and is_active != 1:
                    continue
                found[our_name] = int(asset_id)
                break

def resolve_asset_id(asset_str):
    """Devuelve el ID del asset: primero dinámico, luego estático."""
    with DYNAMIC_LOCK:
        if asset_str in DYNAMIC_ASSETS:
            return DYNAMIC_ASSETS[asset_str]
    return STATIC_ASSET_MAP.get(asset_str)

# ═══════════════════════════════════════════════════════════════════
# BALANCE READER
# ═══════════════════════════════════════════════════════════════════

def get_demo_balance(expert):
    """Intenta obtener el saldo demo de múltiples fuentes."""
    sources = [
        # 1. Atributo profile (dict cacheado)
        lambda: _dig_balance(getattr(expert, 'profile', None)),
        # 2. msg_by_action.profile
        lambda: _dig_balance((getattr(expert, 'msg_by_action', {}) or {}).get('profile')),
        # 3. Método Profile()
        lambda: _dig_balance(expert.Profile() if hasattr(expert, 'Profile') else None),
    ]

    for src in sources:
        try:
            bal = src()
            if bal is not None and bal > 0:
                return bal
        except Exception:
            continue
    return None

def _dig_balance(data):
    """Extrae balance de un dict/objeto profile."""
    if data is None:
        return None
    if isinstance(data, dict):
        for key in ['demo_balance', 'demoBalance', 'balance', 'd']:
            if key in data:
                try:
                    return float(data[key])
                except (ValueError, TypeError):
                    continue
    return None

# ═══════════════════════════════════════════════════════════════════
# ROBUST WS CONNECTION
# ═══════════════════════════════════════════════════════════════════

def kill_expert(expert):
    """Cierre agresivo de conexión y hilos."""
    if expert is None:
        return
    try:
        ws = getattr(expert, 'websocket_client', None)
        if ws:
            ws.close()
    except Exception:
        pass
    for attr in ['websocket_thread', 'ping_thread']:
        try:
            t = getattr(expert, attr, None)
            if t and hasattr(t, 'is_alive') and t.is_alive():
                t.join(timeout=2)
        except Exception:
            pass

def wait_ws_ready(expert, timeout=12):
    """Espera que el WS esté conectado verificando sock.connected."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            ws = getattr(expert, 'websocket_client', None)
            if ws and hasattr(ws, 'sock') and ws.sock and ws.sock.connected:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    # Fallback: si hay msg_by_action con datos, probablemente está conectado
    try:
        mba = getattr(expert, 'msg_by_action', None)
        if mba and len(mba) > 0:
            return True
    except Exception:
        pass
    return False

# ═══════════════════════════════════════════════════════════════════
# TRADE EXECUTION — LAZY MODE
# ═══════════════════════════════════════════════════════════════════

def execute_trade(asset_str, direction, tf='5M'):
    """Conecta → Descubre assets → Lee saldo → Opera → Desconecta."""
    if EoApi is None:
        return {"status": "error", "message": "EoApi no cargado"}
    if not TOKEN:
        return {"status": "error", "message": "EO_TOKEN no configurado"}

    eo_type = "call" if direction == "BUY" else "put"
    exp_time = TF_TO_EXP.get(tf, 300)

    expert = None
    max_retries = 3
    for attempt in range(max_retries):
        try:
            print(f"[TRADE] 🔌 Conectando ({attempt + 1}/{max_retries})...")
            expert = EoApi(token=TOKEN, server_region=SERVER)
            expert.connect()

            # Esperar WS listo
            ws_ok = wait_ws_ready(expert, timeout=12)
            if ws_ok:
                print(f"[TRADE] ✅ WS conectado")
            else:
                raise Exception("WebSocket no se conectó en 12s")

            # SetDemo
            expert.SetDemo()
            time.sleep(1)

            # Descubrir assets dinámicos (solo si cache > 5 min)
            if time.time() - DYNAMIC_ASSETS_TS > 300:
                capture_assets_from_ws(expert)

            # Resolver asset ID (dinámico > estático)
            asset_id = resolve_asset_id(asset_str)
            if asset_id is None:
                kill_expert(expert)
                return {"status": "error", "message": f"Activo no mapeado: {asset_str}"}

            # Leer saldo y calcular 10%
            trade_amount = AMOUNT
            balance = get_demo_balance(expert)
            if balance is not None and balance > 0:
                trade_amount = max(1, int(balance * 0.10))
                print(f"[TRADE] 💰 Saldo: ${balance:.0f} → 10% = ${trade_amount}")
            else:
                print(f"[TRADE] ⚠️ Saldo no disponible → fijo ${trade_amount}")

            # EJECUTAR
            print(f"[TRADE] 🎯 {eo_type.upper()} {asset_str} (ID:{asset_id}) ${trade_amount} exp:{exp_time}s ({tf})")
            result = expert.Buy(
                amount=trade_amount,
                type=eo_type,
                assetid=asset_id,
                exptime=exp_time,
                isdemo=1,
                strike_time=time.time()
            )
            print(f"[TRADE] ✅ Respuesta: {result}")

            kill_expert(expert)
            expert = None

            return {
                "status": "success", "asset": asset_str, "direction": direction,
                "type": eo_type, "asset_id": asset_id, "amount": trade_amount,
                "tf": tf, "exp_seconds": exp_time
            }

        except Exception as e:
            print(f"[TRADE] ❌ Intento {attempt + 1}: {e}")
            kill_expert(expert)
            expert = None

            if attempt < max_retries - 1:
                wait = 3 + (attempt * 3)  # 3s, 6s
                print(f"[TRADE] 🔄 Retry en {wait}s...")
                time.sleep(wait)
            else:
                return {"status": "error", "message": str(e)}

# ═══════════════════════════════════════════════════════════════════
# KEEP-ALIVE — SILENCIOSO
# ═══════════════════════════════════════════════════════════════════

def keep_alive():
    import urllib.request
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    while True:
        time.sleep(600)
        if render_url:
            try:
                urllib.request.urlopen(f"{render_url}/health", timeout=10)
                # SILENCIOSO — no printear cada 10 min
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
    return jsonify({"status": "ok", "mode": "LAZY_v2", "eoapi": EoApi is not None}), 200

@app.route('/broker-status', methods=['GET'])
def broker_status():
    if not check_auth():
        return jsonify({"status": "error", "message": "No autorizado"}), 401
    with DYNAMIC_LOCK:
        dyn = dict(DYNAMIC_ASSETS) if DYNAMIC_ASSETS else None
    return jsonify({
        "connected": bool(EoApi and TOKEN),
        "mode": "LAZY_v2",
        "static_assets": list(STATIC_ASSET_MAP.keys()),
        "dynamic_assets": dyn,
        "dynamic_age_s": int(time.time() - DYNAMIC_ASSETS_TS) if DYNAMIC_ASSETS_TS else None
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
# STARTUP
# ═══════════════════════════════════════════════════════════════════

print(f"[BRIDGE] LAZY v2 | EoApi: {'OK' if EoApi else 'NO'} | Token: {'OK' if TOKEN else 'NO'}")
print(f"[BRIDGE] Assets estáticos: {list(STATIC_ASSET_MAP.keys())}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

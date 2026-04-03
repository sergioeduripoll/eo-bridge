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
# RAW WS INTERCEPTOR — Captura mensajes crudos que la librería descarta
# ═══════════════════════════════════════════════════════════════════

WS_RAW_DATA = {
    'balance': None,
    'assets': {},       # {asset_id: {name, symbol, isActive, ...}}
    'profile': None,
    'ready': False
}
WS_RAW_LOCK = threading.Lock()

def inject_ws_interceptor(expert):
    """
    Monkey-patch el on_message del websocket_client de la librería.
    Captura los mensajes JSON crudos ANTES de que la librería los descarte.
    """
    ws = getattr(expert, 'websocket_client', None)
    if ws is None:
        print("[WS-HOOK] ⚠️ websocket_client no encontrado")
        return False

    # Guardar callback original
    original_on_message = ws.on_message

    def intercepted_on_message(ws_app, message):
        """Intercepta cada mensaje WS, extrae lo que necesitamos, luego pasa al original."""
        try:
            if isinstance(message, str):
                data = json.loads(message)
                action = data.get('action', '')

                # Capturar profile (contiene demo_balance)
                if action == 'profile':
                    msg = data.get('message', data)
                    with WS_RAW_LOCK:
                        WS_RAW_DATA['profile'] = msg
                        # Extraer balance
                        if isinstance(msg, dict):
                            for key in ['demo_balance', 'demoBalance', 'balance', 'd']:
                                if key in msg:
                                    try:
                                        WS_RAW_DATA['balance'] = float(msg[key])
                                    except (ValueError, TypeError):
                                        pass

                # Capturar assets
                elif action == 'assets':
                    msg = data.get('message', data)
                    _parse_raw_assets(msg)

                # Capturar multipleAction (contiene profile + assets dentro)
                elif action == 'multipleAction':
                    msg = data.get('message', data.get('data', []))
                    if isinstance(msg, list):
                        for sub in msg:
                            if isinstance(sub, dict):
                                sub_action = sub.get('action', '')
                                if sub_action == 'profile':
                                    sub_msg = sub.get('message', sub)
                                    with WS_RAW_LOCK:
                                        WS_RAW_DATA['profile'] = sub_msg
                                        if isinstance(sub_msg, dict):
                                            for key in ['demo_balance', 'demoBalance', 'balance']:
                                                if key in sub_msg:
                                                    try:
                                                        WS_RAW_DATA['balance'] = float(sub_msg[key])
                                                    except:
                                                        pass
                                elif sub_action == 'assets':
                                    _parse_raw_assets(sub.get('message', sub))

        except Exception:
            pass  # Nunca romper el WS por un error de parsing

        # Pasar al handler original de la librería
        if original_on_message:
            try:
                original_on_message(ws_app, message)
            except Exception:
                pass

    # Inyectar
    ws.on_message = intercepted_on_message
    print("[WS-HOOK] ✅ Interceptor inyectado")
    return True

def _parse_raw_assets(msg):
    """Extrae assets de un mensaje crudo del WS."""
    if msg is None:
        return
    with WS_RAW_LOCK:
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict) and 'id' in item:
                    _index_asset(item)
        elif isinstance(msg, dict):
            # Puede ser {data: [...]} o directamente un asset
            if 'data' in msg and isinstance(msg['data'], list):
                for item in msg['data']:
                    if isinstance(item, dict) and 'id' in item:
                        _index_asset(item)
            elif 'id' in msg:
                _index_asset(msg)

def _index_asset(item):
    """Indexa un asset individual en WS_RAW_DATA['assets']."""
    aid = item.get('id')
    if not aid:
        return
    WS_RAW_DATA['assets'][int(aid)] = {
        'name': item.get('_name', item.get('name', '')),
        'symbol': item.get('symbol', ''),
        'isActive': item.get('isActive', item.get('is_active', 0)),
        'id': int(aid)
    }

# ═══════════════════════════════════════════════════════════════════
# ASSET RESOLVER — Usa datos interceptados del WS
# ═══════════════════════════════════════════════════════════════════

def resolve_asset_id(asset_str):
    """
    Busca el ID del activo en los datos crudos del WS.
    Prioriza activos con isActive=1. Fallback a mapa estático.
    """
    search_terms = SYMBOL_SEARCH.get(asset_str, [])
    if not search_terms:
        return STATIC_ASSET_MAP.get(asset_str)

    best_id = None
    best_active = False

    with WS_RAW_LOCK:
        for aid, info in WS_RAW_DATA['assets'].items():
            symbol = info.get('symbol', '').upper()
            name = info.get('name', '').lower()
            active = info.get('isActive') == 1

            for term in search_terms:
                if term.upper() in symbol or term.lower() in name:
                    # Si encontramos uno activo, preferirlo
                    if active and not best_active:
                        best_id = aid
                        best_active = True
                    elif not best_id:
                        best_id = aid
                    break

    if best_id:
        return best_id
    return STATIC_ASSET_MAP.get(asset_str)

def get_intercepted_balance():
    """Lee el balance capturado por el interceptor WS."""
    with WS_RAW_LOCK:
        return WS_RAW_DATA['balance']

def get_intercepted_asset_count():
    """Cantidad de assets capturados."""
    with WS_RAW_LOCK:
        return len(WS_RAW_DATA['assets'])

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
            # Reset raw data para esta conexión
            with WS_RAW_LOCK:
                WS_RAW_DATA['balance'] = None
                WS_RAW_DATA['assets'] = {}
                WS_RAW_DATA['profile'] = None

            print(f"[TRADE] 🔌 Conectando ({attempt + 1}/{max_retries})...")
            expert = EoApi(token=TOKEN, server_region=SERVER)
            expert.connect()

            # Inyectar interceptor INMEDIATAMENTE después de connect
            inject_ws_interceptor(expert)

            # Esperar WS listo
            ws_ok = wait_ws_ready(expert, timeout=12)
            if ws_ok:
                print(f"[TRADE] ✅ WS conectado")
            else:
                raise Exception("WebSocket no se conectó en 12s")

            # SetDemo
            expert.SetDemo()

            # ═══ POLLING: esperar hasta 5s a que el interceptor capture datos ═══
            poll_start = time.time()
            poll_timeout = 5
            balance = None
            asset_count = 0
            while time.time() - poll_start < poll_timeout:
                balance = get_intercepted_balance()
                asset_count = get_intercepted_asset_count()

                if balance is not None and balance > 0 and asset_count > 0:
                    print(f"[TRADE] ✅ Datos WS en {time.time() - poll_start:.1f}s (saldo: ${balance:.0f}, assets: {asset_count})")
                    break
                time.sleep(0.5)
            else:
                print(f"[TRADE] ⚠️ Polling {time.time() - poll_start:.1f}s — balance: {balance}, assets: {asset_count}")

            # Resolver asset ID (dinámico > estático)
            asset_id = resolve_asset_id(asset_str)
            if asset_id is None:
                kill_expert(expert)
                return {"status": "error", "message": f"Activo no mapeado: {asset_str}"}

            # Calcular monto: 10% del saldo (entero, mín 1) o fallback fijo
            trade_amount = AMOUNT
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
    return jsonify({"status": "ok", "mode": "LAZY_v3", "eoapi": EoApi is not None}), 200

@app.route('/broker-status', methods=['GET'])
def broker_status():
    if not check_auth():
        return jsonify({"status": "error", "message": "No autorizado"}), 401
    with WS_RAW_LOCK:
        asset_count = len(WS_RAW_DATA['assets'])
        balance = WS_RAW_DATA['balance']
    return jsonify({
        "connected": bool(EoApi and TOKEN),
        "mode": "LAZY_v3_interceptor",
        "static_assets": list(STATIC_ASSET_MAP.keys()),
        "ws_assets_captured": asset_count,
        "ws_balance": balance
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

print(f"[BRIDGE] LAZY v3 + WS Interceptor | EoApi: {'OK' if EoApi else 'NO'} | Token: {'OK' if TOKEN else 'NO'}")
print(f"[BRIDGE] Assets estáticos: {list(STATIC_ASSET_MAP.keys())}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

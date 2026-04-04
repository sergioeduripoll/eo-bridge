"""
app.py — Puente ExpertOption v4 — Conexión Global Persistente
- UNA sola instancia global de EoApi conectada al arrancar
- WS Interceptor inyectado una vez, mantiene WS_RAW_DATA en segundo plano
- /trade NO crea instancias nuevas, usa la global
- gc.collect() periódico para limpiar basura
- Sin ThreadPoolExecutor (evita bloquear workers de Flask)
"""

import os
import sys
import time
import json
import gc
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

STATIC_ASSET_MAP = {
    "BTC/USD":  160, "ETH/USD":  162, "XRP/USD":  173,
    "ADA/USD":  235, "SOL/USD":  464, "DOGE/USD": 463, "BNB/USD":  462
}

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
# WS RAW DATA — Actualizado en segundo plano por el interceptor
# ═══════════════════════════════════════════════════════════════════

WS_RAW_DATA = {
    'balance': None,
    'assets': {},
    'profile': None,
}
WS_RAW_LOCK = threading.Lock()

# ═══════════════════════════════════════════════════════════════════
# WS INTERCEPTOR — Se inyecta UNA VEZ en la conexión global
# ═══════════════════════════════════════════════════════════════════

def inject_ws_interceptor(expert):
    """Monkey-patch on_message para capturar datos crudos del WS."""
    ws = getattr(expert, 'websocket_client', None)
    if ws is None:
        print("[WS-HOOK] ⚠️ websocket_client no encontrado")
        return False

    original_on_message = ws.on_message

    def intercepted_on_message(ws_app, message):
        try:
            if isinstance(message, str):
                data = json.loads(message)
                action = data.get('action', '')

                if action == 'profile':
                    msg = data.get('message', data)
                    with WS_RAW_LOCK:
                        WS_RAW_DATA['profile'] = msg
                        if isinstance(msg, dict):
                            for key in ['demo_balance', 'demoBalance', 'balance', 'd']:
                                if key in msg:
                                    try:
                                        WS_RAW_DATA['balance'] = float(msg[key])
                                    except (ValueError, TypeError):
                                        pass

                elif action == 'assets':
                    msg = data.get('message', data)
                    _parse_raw_assets(msg)

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
            pass

        if original_on_message:
            try:
                original_on_message(ws_app, message)
            except Exception:
                pass

    ws.on_message = intercepted_on_message
    print("[WS-HOOK] ✅ Interceptor inyectado en conexión global")
    return True

def _parse_raw_assets(msg):
    if msg is None:
        return
    with WS_RAW_LOCK:
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict) and 'id' in item:
                    _index_asset(item)
        elif isinstance(msg, dict):
            if 'data' in msg and isinstance(msg['data'], list):
                for item in msg['data']:
                    if isinstance(item, dict) and 'id' in item:
                        _index_asset(item)
            elif 'id' in msg:
                _index_asset(msg)

def _index_asset(item):
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
# ASSET RESOLVER
# ═══════════════════════════════════════════════════════════════════

def resolve_asset_id(asset_str):
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
                    if active and not best_active:
                        best_id = aid
                        best_active = True
                    elif not best_id:
                        best_id = aid
                    break

    if best_id:
        return best_id
    return STATIC_ASSET_MAP.get(asset_str)

# ═══════════════════════════════════════════════════════════════════
# CONEXIÓN GLOBAL PERSISTENTE
# ═══════════════════════════════════════════════════════════════════

expert = None          # Instancia global única
expert_lock = threading.Lock()
GC_COUNTER = 0         # Contador para gc.collect() periódico

def is_ws_alive():
    """Verifica si el WebSocket global sigue vivo."""
    global expert
    if expert is None:
        return False
    try:
        ws = getattr(expert, 'websocket_client', None)
        if ws and hasattr(ws, 'sock') and ws.sock and ws.sock.connected:
            return True
    except Exception:
        pass
    # Fallback: chequear msg_by_action
    try:
        mba = getattr(expert, 'msg_by_action', None)
        if mba and len(mba) > 0:
            return True
    except Exception:
        pass
    return False

def connect_global():
    """Conecta la instancia global y inyecta el interceptor UNA VEZ."""
    global expert
    if EoApi is None:
        print("[GLOBAL] ❌ EoApi no disponible")
        return False
    if not TOKEN:
        print("[GLOBAL] ❌ EO_TOKEN no configurado")
        return False

    try:
        # Matar instancia anterior si existe
        if expert is not None:
            try:
                ws = getattr(expert, 'websocket_client', None)
                if ws:
                    ws.close()
            except Exception:
                pass
            expert = None
            gc.collect()

        print("[GLOBAL] 🔌 Conectando a ExpertOption...")
        expert = EoApi(token=TOKEN, server_region=SERVER)
        expert.connect()

        # Esperar WS listo
        start = time.time()
        while time.time() - start < 12:
            try:
                ws = getattr(expert, 'websocket_client', None)
                if ws and hasattr(ws, 'sock') and ws.sock and ws.sock.connected:
                    break
            except Exception:
                pass
            time.sleep(0.3)

        # Inyectar interceptor UNA VEZ
        inject_ws_interceptor(expert)

        # SetDemo
        expert.SetDemo()

        # Pedir datos activamente
        try:
            ws_sock = getattr(expert.websocket_client, 'sock', None)
            if ws_sock and ws_sock.connected:
                ws_sock.send(json.dumps({"action": "profile"}))
                ws_sock.send(json.dumps({"action": "assets"}))
        except Exception:
            pass

        # Esperar datos del interceptor (hasta 5s)
        poll_start = time.time()
        while time.time() - poll_start < 5:
            with WS_RAW_LOCK:
                bal = WS_RAW_DATA['balance']
                ac = len(WS_RAW_DATA['assets'])
            if bal is not None and bal > 0:
                print(f"[GLOBAL] ✅ Conectado | Saldo: ${bal:.0f} | Assets: {ac}")
                return True
            time.sleep(0.5)

        print(f"[GLOBAL] ✅ Conectado (sin datos WS aún, usará fallback)")
        return True

    except Exception as e:
        print(f"[GLOBAL] ❌ Error: {e}")
        expert = None
        return False

def ensure_connection():
    """Verifica conexión global, reconecta si es necesario."""
    global expert
    with expert_lock:
        if is_ws_alive():
            return True
        print("[GLOBAL] 🔄 Reconectando...")
        return connect_global()

# ═══════════════════════════════════════════════════════════════════
# TRADE EXECUTION — Usa conexión global, NO crea instancias nuevas
# ═══════════════════════════════════════════════════════════════════

def execute_trade(asset_str, direction, tf='5M'):
    """Usa la conexión global para ejecutar el trade."""
    global expert, GC_COUNTER

    if EoApi is None:
        return {"status": "error", "message": "EoApi no cargado"}
    if not TOKEN:
        return {"status": "error", "message": "EO_TOKEN no configurado"}

    eo_type = "call" if direction == "BUY" else "put"
    exp_time = TF_TO_EXP.get(tf, 300)

    # Asegurar conexión global
    if not ensure_connection():
        return {"status": "error", "message": "No se pudo conectar a ExpertOption"}

    # Resolver asset ID (dinámico > estático)
    asset_id = resolve_asset_id(asset_str)
    if asset_id is None:
        return {"status": "error", "message": f"Activo no mapeado: {asset_str}"}

    # Leer saldo del interceptor
    with WS_RAW_LOCK:
        balance = WS_RAW_DATA['balance']

    trade_amount = AMOUNT
    if balance is not None and balance > 0:
        trade_amount = max(1, int(balance * 0.10))
        print(f"[TRADE] 💰 Saldo: ${balance:.0f} → 10% = ${trade_amount}")
    else:
        print(f"[TRADE] ⚠️ Saldo no disponible → fijo ${trade_amount}")

    # EJECUTAR — directo, sin ThreadPoolExecutor
    print(f"[TRADE] 🎯 {eo_type.upper()} {asset_str} (ID:{asset_id}) ${trade_amount} exp:{exp_time}s ({tf})")
    try:
        result = expert.Buy(
            amount=trade_amount,
            type=eo_type,
            assetid=asset_id,
            exptime=exp_time,
            isdemo=1,
            strike_time=time.time()
        )
        print(f"[TRADE] ✅ Respuesta: {result}")
    except Exception as e:
        print(f"[TRADE] ❌ Error en Buy(): {e}")
        # Marcar conexión como muerta para reconectar en próximo trade
        expert = None
        return {"status": "error", "message": str(e)}

    # GC periódico (cada 10 trades)
    GC_COUNTER += 1
    if GC_COUNTER % 10 == 0:
        collected = gc.collect()
        if collected > 0:
            print(f"[GC] Limpiados {collected} objetos")

    return {
        "status": "success", "asset": asset_str, "direction": direction,
        "type": eo_type, "asset_id": asset_id, "amount": trade_amount,
        "tf": tf, "exp_seconds": exp_time
    }

# ═══════════════════════════════════════════════════════════════════
# KEEP-ALIVE + WATCHDOG — Silencioso, verifica conexión global
# ═══════════════════════════════════════════════════════════════════

def keep_alive_watchdog():
    import urllib.request
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    gc_cycle = 0
    while True:
        time.sleep(600)  # 10 min
        # Ping HTTP para que Render no duerma
        if render_url:
            try:
                urllib.request.urlopen(f"{render_url}/health", timeout=10)
            except Exception:
                pass
        # Verificar conexión global (reconectar si cayó)
        if not is_ws_alive():
            with expert_lock:
                connect_global()
        # GC periódico
        gc_cycle += 1
        if gc_cycle % 3 == 0:  # Cada 30 min
            collected = gc.collect()
            if collected > 0:
                print(f"[GC] Watchdog: limpiados {collected} objetos")

threading.Thread(target=keep_alive_watchdog, daemon=True).start()

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
    return jsonify({
        "status": "ok",
        "mode": "GLOBAL_v4",
        "eoapi": EoApi is not None,
        "ws_alive": is_ws_alive()
    }), 200

@app.route('/broker-status', methods=['GET'])
def broker_status():
    if not check_auth():
        return jsonify({"status": "error", "message": "No autorizado"}), 401
    with WS_RAW_LOCK:
        asset_count = len(WS_RAW_DATA['assets'])
        balance = WS_RAW_DATA['balance']
    return jsonify({
        "connected": is_ws_alive(),
        "mode": "GLOBAL_v4",
        "ws_balance": balance,
        "ws_assets_captured": asset_count,
        "static_assets": list(STATIC_ASSET_MAP.keys())
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
# STARTUP — Conectar la instancia global al arrancar
# ═══════════════════════════════════════════════════════════════════

print(f"[BRIDGE] GLOBAL v4 | EoApi: {'OK' if EoApi else 'NO'} | Token: {'OK' if TOKEN else 'NO'}")
print(f"[BRIDGE] Assets estáticos: {list(STATIC_ASSET_MAP.keys())}")

# Conectar al arrancar
if EoApi and TOKEN:
    connect_global()
else:
    print("[BRIDGE] ⚠️ No se conecta al arrancar (falta EoApi o Token)")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

"""
app.py — Puente ExpertOption v4 — Conexión Global Persistente (Optimizado)
- UNA sola instancia global de EoApi conectada al arrancar
- WS Interceptor inyectado una vez, mantiene WS_RAW_DATA en segundo plano
- /trade NO crea instancias nuevas, usa la global
- gc.collect() periódico optimizado (cada 20 ciclos)
- Sin ThreadPoolExecutor para evitar fugas de memoria y OOM en Render
- Logs controlados por ENV para evitar saturación de disco
- Fix WS: Manejo limpio de acciones (candles, pong, tradersChoice)
"""

import os
import sys
import time
import json
import gc
import threading
import signal

class TimeoutException(Exception):
    pass

def timeout_handler(signum, frame):
    raise TimeoutException()

# ═══════════════════════════════════════════════════════════════════
# CONFIG & LOGS
# ═══════════════════════════════════════════════════════════════════

def log(msg):
    if os.environ.get("ENV") != "prod":
        print(msg)

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
        log(f"[BRIDGE] ✅ EoApi desde {path}")
        break
    except (ImportError, AttributeError):
        continue

TOKEN = os.environ.get("EO_TOKEN", "")
SERVER = os.environ.get("EO_SERVER", "wss://fr24g1eu.expertoption.com/")
API_SECRET = os.environ.get("BRIDGE_SECRET", "cambiar_esto")
AMOUNT = int(os.environ.get("TRADE_AMOUNT", "10"))
IS_DEMO = int(os.environ.get("EO_DEMO", "0"))

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
# WS RAW DATA
# ═══════════════════════════════════════════════════════════════════

WS_RAW_DATA = {
    'balance': None,
    'assets': {},
    'profile': None,
}
WS_RAW_LOCK = threading.Lock()
MAX_ASSETS = 200

# ═══════════════════════════════════════════════════════════════════
# WS HANDLERS (Silenciosos)
# ═══════════════════════════════════════════════════════════════════

def handle_candles(data):
    candles = data.get("data", [])
    if not candles:
        return
    # last_candle = candles[-1]
    # TODO: conectar con tu lógica de señales si es necesario en el futuro
    # log(f"[CANDLES] Recibidas {len(candles)} velas") 

def handle_traders_choice(data):
    # log(f"[TRADERS_CHOICE] {data}")
    pass

# ═══════════════════════════════════════════════════════════════════
# WS INTERCEPTOR
# ═══════════════════════════════════════════════════════════════════

def inject_ws_interceptor(expert):
    ws = getattr(expert, 'websocket_client', None)
    if ws is None:
        log("[WS-HOOK] ⚠️ websocket_client no encontrado")
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
                                    except:
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

                elif action == 'candles':
                    handle_candles(data)

                elif action == 'pong':
                    return

                elif action == 'tradersChoice':
                    handle_traders_choice(data)

                else:
                    # Solo loggear acciones realmente inesperadas
                    if action not in ["candles", "pong", "tradersChoice", "assets", "profile", "multipleAction"]:
                        log(f"[WARN] Unknown action ignorada: {action}")

        except Exception:
            pass

        if original_on_message:
            try:
                original_on_message(ws_app, message)
            except Exception:
                pass

    ws.on_message = intercepted_on_message
    log("[WS-HOOK] ✅ Interceptor inyectado en conexión global")
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

    with WS_RAW_LOCK:
        # 🔥 Limitar crecimiento de memoria
        if len(WS_RAW_DATA['assets']) > MAX_ASSETS:
            WS_RAW_DATA['assets'] = dict(list(WS_RAW_DATA['assets'].items())[-100:])

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

expert = None
expert_lock = threading.Lock()
GC_COUNTER = 0
LAST_RECONNECT = 0

def is_ws_alive():
    global expert
    if expert is None:
        return False
    try:
        ws = getattr(expert, 'websocket_client', None)
        if ws and hasattr(ws, 'sock') and ws.sock and ws.sock.connected:
            return True
    except Exception:
        pass
    return False

def connect_global():
    global expert
    if EoApi is None:
        log("[GLOBAL] ❌ EoApi no disponible")
        return False
    if not TOKEN:
        log("[GLOBAL] ❌ EO_TOKEN no configurado")
        return False

    try:
        if expert is not None:
            try:
                ws = getattr(expert, 'websocket_client', None)
                if ws: ws.close()
            except: pass
            expert = None
            gc.collect()

        log("[GLOBAL] 🔌 Conectando a ExpertOption...")
        expert = EoApi(token=TOKEN, server_region=SERVER)
        expert.connect()

        start = time.time()
        while time.time() - start < 12:
            if is_ws_alive(): break
            time.sleep(0.3)

        inject_ws_interceptor(expert)
        expert.SetReal()

        try:
            ws_sock = getattr(expert.websocket_client, 'sock', None)
            if ws_sock and ws_sock.connected:
                ws_sock.send(json.dumps({"action": "profile"}))
                ws_sock.send(json.dumps({"action": "assets"}))
        except: pass

        poll_start = time.time()
        while time.time() - poll_start < 5:
            with WS_RAW_LOCK:
                bal = WS_RAW_DATA['balance']
                ac = len(WS_RAW_DATA['assets'])
            if bal is not None and bal > 0:
                log(f"[GLOBAL] ✅ Conectado | Saldo: ${bal:.0f} | Assets: {ac}")
                return True
            time.sleep(0.5)

        return True
    except Exception as e:
        log(f"[GLOBAL] ❌ Error: {e}")
        expert = None
        return False

def ensure_connection():
    global expert, LAST_RECONNECT

    with expert_lock:
        if is_ws_alive():
            return True

        # Evita reconexión en loop
        if time.time() - LAST_RECONNECT < 10:
            return False

        LAST_RECONNECT = time.time()
        log("[GLOBAL] 🔄 Reconectando con cooldown...")
        return connect_global()

# ═══════════════════════════════════════════════════════════════════
# TRADE EXECUTION
# ═══════════════════════════════════════════════════════════════════

def execute_trade(asset_str, direction, tf='5M'):
    global expert, GC_COUNTER

    if EoApi is None:
        return {"status": "error", "reason": "EXECUTION_ERROR", "details": "EoApi no cargado"}
    if not TOKEN:
        return {"status": "error", "reason": "AUTH_ERROR", "details": "EO_TOKEN no configurado"}

    eo_type = "call" if direction == "BUY" else "put"
    exp_time = TF_TO_EXP.get(tf, 300)

    if not ensure_connection():
        return {"status": "error", "reason": "NO_CONNECTION", "details": "No se pudo conectar a ExpertOption"}

    asset_id = resolve_asset_id(asset_str)
    if asset_id is None:
        return {"status": "error", "reason": "INVALID_ASSET", "details": f"Activo no mapeado: {asset_str}"}

    # 🔥 Espera inteligente del Balance
    with WS_RAW_LOCK:
        balance = WS_RAW_DATA['balance']
    
    wait_time = 0
    while balance is None and wait_time < 3:
        time.sleep(0.5)
        wait_time += 0.5
        with WS_RAW_LOCK:
            balance = WS_RAW_DATA['balance']

    trade_amount = AMOUNT
    log(f"[TRADE] 💰 Saldo: ${balance if balance is not None else '?'}")

    # 🔥 EJECUCIÓN QUIRÚRGICA SIN THREADPOOL
    log(f"[TRADE] 🎯 {eo_type.upper()} {asset_str} (ID:{asset_id}) ${trade_amount}")
    
    def safe_buy():
        return expert.Buy(amount=trade_amount, type=eo_type, assetid=asset_id, exptime=exp_time, isdemo=IS_DEMO, strike_time=time.time())

    start_exec = time.time()
    result = None

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(8)

    try:
        for attempt in range(2):
            try:
                result = safe_buy()
                break
            except Exception as e:
                if attempt == 1:
                    raise e
                time.sleep(0.5)
        signal.alarm(0)
    except TimeoutException:
        return {
            "status": "error",
            "reason": "TIMEOUT",
            "details": "Broker no respondió en 8s"
        }
    except Exception as e:
        signal.alarm(0)
        return {
            "status": "error",
            "reason": "EXECUTION_ERROR",
            "details": str(e)
        }

    log(f"[TRADE] ✅ Respuesta: {result}")

    # Validación robusta
    if not isinstance(result, dict) or not result:
        return {
            "status": "error",
            "reason": "ASSET_INACTIVE",
            "details": f"{asset_str} no disponible o mercado cerrado"
        }

    # 🔥 GC MÁS EFICIENTE
    GC_COUNTER += 1
    if GC_COUNTER % 20 == 0:
        collected = gc.collect()
        log(f"[GC] Limpiados {collected} objetos")

    return {
        "status": "success", "asset": asset_str, "direction": direction,
        "type": eo_type, "asset_id": asset_id, "amount": trade_amount,
        "tf": tf, "exp_seconds": exp_time
    }

# ═══════════════════════════════════════════════════════════════════
# KEEP-ALIVE
# ═══════════════════════════════════════════════════════════════════

def keep_alive_watchdog():
    import urllib.request
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    gc_cycle = 0
    while True:
        time.sleep(600)
        if render_url:
            try:
                urllib.request.urlopen(f"{render_url}/health", timeout=10)
            except Exception: pass
        
        if not is_ws_alive():
            with expert_lock: connect_global()
        
        gc_cycle += 1
        if gc_cycle % 3 == 0:
            collected = gc.collect()
            log(f"[GC] Watchdog: limpiados {collected} objetos")

threading.Thread(target=keep_alive_watchdog, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# RUTAS
# ═══════════════════════════════════════════════════════════════════

def check_auth():
    auth = request.headers.get('Authorization', '')
    return auth.startswith('Bearer ') and auth.split(' ', 1)[1] == API_SECRET

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "ws_alive": is_ws_alive(),
        "balance": WS_RAW_DATA['balance'],
        "assets_loaded": len(WS_RAW_DATA['assets'])
    }), 200

@app.route('/trade', methods=['POST'])
def trade():
    if not check_auth():
        return jsonify({"status": "error", "reason": "AUTH_ERROR", "details": "No autorizado"}), 401

    data = request.json
    if not data:
        return jsonify({"status": "error", "reason": "EXECUTION_ERROR", "details": "Body vacío"}), 400

    asset_str = data.get('asset', '')
    direction = data.get('direction', '').upper()
    tf = data.get('tf', '5M')

    if direction not in ('BUY', 'SELL'):
        return jsonify({"status": "error", "reason": "INVALID_ASSET", "details": f"Dirección inválida: {direction}"}), 400

    log(f"[TRADE] ━━━ {asset_str} {direction} ━━━")
    result = execute_trade(asset_str, direction, tf)
    
    code = 200 if result["status"] == "success" else 500
    return jsonify(result), code

# ═══════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════

if EoApi and TOKEN:
    connect_global()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

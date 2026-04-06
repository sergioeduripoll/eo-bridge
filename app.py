"""
app.py — Puente ExpertOption v6
- FIX RAÍZ: Lee balance de expert.msg_by_action (la librería lo guarda internamente)
- No depende del interceptor para el balance
- Interceptor solo captura trade_id en tiempo real
- Desuscribe candles para reducir tráfico
"""

import os, sys, time, json, gc, threading, signal

class TimeoutException(Exception):
    pass

def timeout_handler(signum, frame):
    raise TimeoutException()

def log(msg):
    print(msg, flush=True)

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

TOKEN  = os.environ.get("EO_TOKEN", "")
SERVER = os.environ.get("EO_SERVER", "wss://fr24g1eu.expertoption.com/")
API_SECRET = os.environ.get("BRIDGE_SECRET", "cambiar_esto")

# ═══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN — EDITAR ACÁ
# ═══════════════════════════════════════════════════════════════════
AMOUNT  = int(os.environ.get("TRADE_AMOUNT", "1"))  # <── MONTO $1
IS_DEMO = int(os.environ.get("EO_DEMO", "0"))       # <── 0=REAL 1=DEMO
# ═══════════════════════════════════════════════════════════════════

STATIC_ASSET_MAP = {
    "BTC/USD": 160, "ETH/USD": 162, "XRP/USD": 173,
    "ADA/USD": 235, "SOL/USD": 464, "DOGE/USD": 463, "BNB/USD": 462
}
TF_TO_EXP = {'5M': 300, '15M': 900, '30M': 1800, '1H': 3600}

# ═══════════════════════════════════════════════════════════════════
# ESTADO
# ═══════════════════════════════════════════════════════════════════
expert = None
expert_lock = threading.Lock()
GC_COUNTER = 0
LAST_RECONNECT = 0
LAST_TRADE = {'trade_id': None, 'result': None}
TRADE_LOCK = threading.Lock()

# ═══════════════════════════════════════════════════════════════════
# BALANCE — Lee de la librería directamente
# ═══════════════════════════════════════════════════════════════════
def extract_balance():
    """Lee balance de expert.msg_by_action['profile'] — la librería lo guarda."""
    if expert is None:
        return None, None
    try:
        store = getattr(expert, 'msg_by_action', None)
        if not store or not isinstance(store, dict):
            return None, None
        
        profile_msg = store.get('profile')
        if not profile_msg:
            return None, None
        
        # Navegar la estructura: puede ser dict o anidado
        if isinstance(profile_msg, dict):
            p = profile_msg.get('profile', profile_msg)
            if isinstance(p, dict):
                return p.get('demo_balance'), p.get('real_balance')
        
        if isinstance(profile_msg, list) and len(profile_msg) > 0:
            last = profile_msg[-1]
            if isinstance(last, dict):
                p = last.get('profile', last)
                if isinstance(p, dict):
                    return p.get('demo_balance'), p.get('real_balance')
    except:
        pass
    return None, None

def get_balance():
    demo, real = extract_balance()
    return demo if IS_DEMO else real

# ═══════════════════════════════════════════════════════════════════
# WS INTERCEPTOR — Solo para capturar trade_id
# ═══════════════════════════════════════════════════════════════════
def inject_interceptor(exp):
    ws = getattr(exp, 'websocket_client', None)
    if ws is None:
        return False
    original = ws.on_message

    def on_msg(ws_app, message):
        try:
            if isinstance(message, str) and '"action":"buyOption"' in message:
                data = json.loads(message)
                msg = data.get('message', {})
                if isinstance(msg, dict) and 'trade_id' in msg:
                    with TRADE_LOCK:
                        LAST_TRADE['trade_id'] = msg['trade_id']
                    log(f"[WS] 🎫 trade_id: {msg['trade_id']}")

            elif isinstance(message, str) and '"action":"openTradeSuccessful"' in message:
                data = json.loads(message)
                trade = data.get('message', {}).get('trade', {})
                if trade:
                    with TRADE_LOCK:
                        LAST_TRADE['result'] = {
                            'id': trade.get('id'),
                            'open_rate': trade.get('open_rate'),
                            'is_demo': trade.get('is_demo'),
                        }
                    log(f"[WS] ✅ Trade OK: id={trade.get('id')} open={trade.get('open_rate')}")
        except:
            pass
        if original:
            try: original(ws_app, message)
            except: pass

    ws.on_message = on_msg
    log("[WS-HOOK] ✅ Interceptor inyectado")
    return True

# ═══════════════════════════════════════════════════════════════════
# CONEXIÓN
# ═══════════════════════════════════════════════════════════════════
def is_ws_alive():
    if expert is None: return False
    try:
        ws = getattr(expert, 'websocket_client', None)
        return ws and hasattr(ws, 'sock') and ws.sock and ws.sock.connected
    except:
        return False

def connect_global():
    global expert
    if not EoApi or not TOKEN:
        log("[GLOBAL] ❌ EoApi o TOKEN no disponible")
        return False
    try:
        if expert is not None:
            try:
                ws = getattr(expert, 'websocket_client', None)
                if ws:
                    try: ws.on_message = None
                    except: pass
                    try: ws.close()
                    except: pass
            except: pass
            expert = None
            gc.collect()

        mode = "DEMO" if IS_DEMO else "REAL"
        log(f"[GLOBAL] 🔌 Conectando ({mode}, ${AMOUNT})...")
        expert = EoApi(token=TOKEN, server_region=SERVER)
        expert.connect()

        # Esperar WS
        for _ in range(50):
            if is_ws_alive(): break
            time.sleep(0.3)
        
        if not is_ws_alive():
            log("[GLOBAL] ❌ WS no conectó")
            return False

        # Inyectar interceptor
        inject_interceptor(expert)
        
        # Esperar que la librería procese profile
        time.sleep(3)
        
        demo, real = extract_balance()
        if demo is not None or real is not None:
            active = demo if IS_DEMO else real
            log(f"[GLOBAL] ✅ Conectado | Demo:${demo} Real:${real} Activo:${active}")
        else:
            # Pedir profile
            try:
                ws_sock = expert.websocket_client.sock
                if ws_sock and ws_sock.connected:
                    ws_sock.send(json.dumps({"action": "profile"}))
                    time.sleep(3)
                    demo, real = extract_balance()
                    if demo or real:
                        log(f"[GLOBAL] ✅ Balance: Demo=${demo} Real=${real}")
                    else:
                        # Debug
                        keys = list(getattr(expert, 'msg_by_action', {}).keys())
                        log(f"[GLOBAL] ⚠️ Sin balance. msg_by_action keys: {keys}")
            except Exception as e:
                log(f"[GLOBAL] Error: {e}")

        # Desuscribir candles
        try:
            ws_sock = expert.websocket_client.sock
            if ws_sock and ws_sock.connected:
                ws_sock.send(json.dumps({"action": "unsubscribeCandles"}))
        except: pass

        return True
    except Exception as e:
        log(f"[GLOBAL] ❌ {e}")
        expert = None
        gc.collect()
        return False

def ensure_connection():
    global LAST_RECONNECT
    with expert_lock:
        if is_ws_alive(): return True
        if time.time() - LAST_RECONNECT < 10: return False
        LAST_RECONNECT = time.time()
        return connect_global()

# ═══════════════════════════════════════════════════════════════════
# TRADE
# ═══════════════════════════════════════════════════════════════════
def execute_trade(asset_str, direction, tf='5M'):
    global GC_COUNTER
    if not EoApi: return {"status":"error","reason":"EXECUTION_ERROR","details":"EoApi no cargado"}
    if not TOKEN:  return {"status":"error","reason":"AUTH_ERROR","details":"EO_TOKEN no configurado"}

    eo_type = "call" if direction == "BUY" else "put"
    exp_time = TF_TO_EXP.get(tf, 300)

    if not ensure_connection():
        return {"status":"error","reason":"NO_CONNECTION","details":"No se pudo conectar"}

    asset_id = STATIC_ASSET_MAP.get(asset_str)
    if not asset_id:
        return {"status":"error","reason":"INVALID_ASSET","details":f"No mapeado: {asset_str}"}

    bal = get_balance()
    if bal is not None and bal < AMOUNT:
        return {"status":"error","reason":"INSUFFICIENT_BALANCE","details":f"${bal:.2f} < ${AMOUNT}"}

    mode = "DEMO" if IS_DEMO else "REAL"
    log(f"[TRADE] 🎯 {eo_type} {asset_str}(ID:{asset_id}) ${AMOUNT} [{mode}]")

    with TRADE_LOCK:
        LAST_TRADE['trade_id'] = None
        LAST_TRADE['result'] = None

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(8)
    result = None
    try:
        for attempt in range(2):
            try:
                result = expert.Buy(amount=AMOUNT, type=eo_type, assetid=asset_id,
                                     exptime=exp_time, isdemo=IS_DEMO, strike_time=time.time())
                break
            except Exception as e:
                if attempt == 1: raise e
                time.sleep(0.5)
        signal.alarm(0)
    except TimeoutException:
        return {"status":"error","reason":"TIMEOUT","details":"8s sin respuesta"}
    except Exception as e:
        signal.alarm(0)
        return {"status":"error","reason":"EXECUTION_ERROR","details":str(e)}

    # Esperar trade_id
    trade_id = None
    for _ in range(10):
        with TRADE_LOCK: trade_id = LAST_TRADE['trade_id']
        if trade_id: break
        time.sleep(0.3)

    if not isinstance(result, dict) or not result:
        return {"status":"error","reason":"ASSET_INACTIVE","details":f"{asset_str} inactivo"}

    GC_COUNTER += 1
    if GC_COUNTER % 20 == 0: gc.collect()

    resp = {"status":"success","asset":asset_str,"direction":direction,"type":eo_type,
            "asset_id":asset_id,"amount":AMOUNT,"tf":tf,"mode":mode,"trade_id":trade_id}
    with TRADE_LOCK:
        tr = LAST_TRADE.get('result')
    if tr: resp["open_rate"] = tr.get("open_rate")
    log(f"[TRADE] 📋 {json.dumps(resp)}")
    return resp

# ═══════════════════════════════════════════════════════════════════
# WATCHDOG
# ═══════════════════════════════════════════════════════════════════
def watchdog():
    import urllib.request
    url = os.environ.get("RENDER_EXTERNAL_URL", "")
    while True:
        time.sleep(300)
        if url:
            try: urllib.request.urlopen(f"{url}/health", timeout=10)
            except: pass
        if not is_ws_alive():
            with expert_lock: connect_global()
        gc.collect()
        log(f"[WATCHDOG] WS:{'✅' if is_ws_alive() else '❌'} Bal:${get_balance()}")

threading.Thread(target=watchdog, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# RUTAS
# ═══════════════════════════════════════════════════════════════════
def check_auth():
    auth = request.headers.get('Authorization', '')
    return auth.startswith('Bearer ') and auth.split(' ', 1)[1] == API_SECRET

@app.route('/health')
def health():
    bal = get_balance()
    if bal is None: extract_balance()
    demo, real = extract_balance()
    return jsonify({"status":"ok","ws_alive":is_ws_alive(),"mode":"DEMO" if IS_DEMO else "REAL",
                    "balance":get_balance(),"demo_balance":demo,"real_balance":real,"trade_amount":AMOUNT}), 200

@app.route('/trade', methods=['POST'])
def trade():
    if not check_auth():
        return jsonify({"status":"error","reason":"AUTH_ERROR","details":"No autorizado"}), 401
    data = request.json
    if not data:
        return jsonify({"status":"error","reason":"EXECUTION_ERROR","details":"Body vacío"}), 400
    d = data.get('direction','').upper()
    if d not in ('BUY','SELL'):
        return jsonify({"status":"error","reason":"INVALID_ASSET","details":f"Dirección inválida: {d}"}), 400
    result = execute_trade(data.get('asset',''), d, data.get('tf','5M'))
    return jsonify(result), 200 if result["status"]=="success" else 500

@app.route('/broker-status')
def broker_status():
    alive = is_ws_alive()
    if alive: extract_balance()
    demo, real = extract_balance()
    return jsonify({"connected":alive,"mode":"DEMO" if IS_DEMO else "REAL",
                    "balance":get_balance(),"demo_balance":demo,"real_balance":real,
                    "trade_amount":AMOUNT,"message":"OK" if alive else "Sin WS"}), 200

@app.route('/debug')
def debug():
    info = {"ws_alive":is_ws_alive(),"expert":expert is not None}
    if expert:
        try:
            store = getattr(expert, 'msg_by_action', {})
            info["msg_keys"] = list(store.keys())
            p = store.get('profile')
            if p:
                info["profile_type"] = str(type(p).__name__)
                if isinstance(p, dict):
                    info["profile_keys"] = list(p.keys())
                    inner = p.get('profile', p)
                    if isinstance(inner, dict):
                        info["demo_balance"] = inner.get('demo_balance')
                        info["real_balance"] = inner.get('real_balance')
        except Exception as e:
            info["error"] = str(e)
        try:
            info["attrs"] = [a for a in dir(expert) if not a.startswith('_')][:25]
        except: pass
    return jsonify(info), 200

# ═══════════════════════════════════════════════════════════════════
if EoApi and TOKEN:
    connect_global()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)), debug=False)

"""
app.py — Puente ExpertOption v7
FIX RAÍZ: Conexión LAZY (en primer request), no al cargar módulo.
Gunicorn fork mata el WS del proceso padre. Conectamos dentro del worker.
Balance se lee de expert.msg_by_action (la librería lo guarda internamente).
"""

import os, time, json, gc, threading, signal as sig

class TimeoutError_(Exception):
    pass
def _alarm(signum, frame):
    raise TimeoutError_()

def log(msg):
    print(msg, flush=True)

from flask import Flask, request, jsonify
app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════════
EoApi = None
for p in ['expert', 'ExpertOptionAPI.expert']:
    try:
        EoApi = getattr(__import__(p, fromlist=['EoApi']), 'EoApi')
        log(f"[BRIDGE] ✅ EoApi desde {p}")
        break
    except: continue

TOKEN      = os.environ.get("EO_TOKEN", "")
SERVER     = os.environ.get("EO_SERVER", "wss://fr24g1eu.expertoption.com/")
API_SECRET = os.environ.get("BRIDGE_SECRET", "cambiar_esto")
AMOUNT     = int(os.environ.get("TRADE_AMOUNT", "1"))
IS_DEMO    = int(os.environ.get("EO_DEMO", "0"))

ASSET_MAP = {
    "BTC/USD": 160, "ETH/USD": 162, "XRP/USD": 173,
    "ADA/USD": 235, "SOL/USD": 464, "DOGE/USD": 463, "BNB/USD": 462
}
TF_EXP = {'5M': 300, '15M': 900, '30M': 1800, '1H': 3600}

# Estado global
expert = None
_lock = threading.Lock()
_last_reconnect = 0
_trade_info = {'trade_id': None, 'result': None}
_trade_lock = threading.Lock()
_initialized = False

# ═══════════════════════════════════════════════════════════════════
# BALANCE — Lee directo de la librería
# ═══════════════════════════════════════════════════════════════════
def _extract_balance():
    if not expert: return None, None
    try:
        store = getattr(expert, 'msg_by_action', {})
        if not store: return None, None
        pm = store.get('profile')
        if isinstance(pm, dict):
            p = pm.get('profile', pm)
            if isinstance(p, dict):
                return p.get('demo_balance'), p.get('real_balance')
        if isinstance(pm, list) and pm:
            p = pm[-1]
            if isinstance(p, dict):
                inner = p.get('profile', p)
                if isinstance(inner, dict):
                    return inner.get('demo_balance'), inner.get('real_balance')
    except: pass
    return None, None

def _active_bal():
    d, r = _extract_balance()
    return d if IS_DEMO else r

# ═══════════════════════════════════════════════════════════════════
# INTERCEPTOR — Solo captura trade_id y openTradeSuccessful
# ═══════════════════════════════════════════════════════════════════
def _inject(exp):
    ws = getattr(exp, 'websocket_client', None)
    if not ws: return
    orig = ws.on_message
    def _on(ws_app, msg):
        try:
            if isinstance(msg, str):
                if '"trade_id"' in msg and '"buyOption"' in msg:
                    d = json.loads(msg)
                    tid = d.get('message',{}).get('trade_id')
                    if tid:
                        with _trade_lock: _trade_info['trade_id'] = tid
                        log(f"[WS] 🎫 trade_id: {tid}")
                elif '"openTradeSuccessful"' in msg:
                    d = json.loads(msg)
                    t = d.get('message',{}).get('trade',{})
                    if t:
                        with _trade_lock:
                            _trade_info['result'] = {'id':t.get('id'),'open_rate':t.get('open_rate'),'is_demo':t.get('is_demo')}
                        log(f"[WS] ✅ id={t.get('id')} open={t.get('open_rate')}")
        except: pass
        if orig:
            try: orig(ws_app, msg)
            except: pass
    ws.on_message = _on
    log("[WS] ✅ Interceptor OK")

# ═══════════════════════════════════════════════════════════════════
# CONEXIÓN — is_ws_alive usa thread check de la librería
# ═══════════════════════════════════════════════════════════════════
def _alive():
    if not expert: return False
    try:
        ws = getattr(expert, 'websocket_client', None)
        if not ws: return False
        # Check 1: sock.connected
        if hasattr(ws, 'sock') and ws.sock:
            try:
                if ws.sock.connected: return True
            except: pass
        # Check 2: thread alive (la librería corre WS en un daemon thread)
        t = getattr(ws, '_thread', None) or getattr(ws, 'thread', None)
        if t and t.is_alive(): return True
        # Check 3: el WS tiene un run_forever thread
        for t in threading.enumerate():
            if 'websocket' in t.name.lower() and t.is_alive():
                return True
    except: pass
    return False

def _connect():
    global expert
    if not EoApi or not TOKEN:
        log("[CONN] ❌ Sin EoApi o TOKEN")
        return False
    try:
        # Limpiar viejo
        if expert:
            try:
                ws = getattr(expert, 'websocket_client', None)
                if ws:
                    try: ws.close()
                    except: pass
            except: pass
            expert = None
            gc.collect()

        m = "DEMO" if IS_DEMO else "REAL"
        log(f"[CONN] 🔌 Conectando ({m}, ${AMOUNT})...")
        expert = EoApi(token=TOKEN, server_region=SERVER)
        expert.connect()

        # Esperar que el WS se establezca (la librería lo hace en thread)
        for i in range(60):  # Hasta 18 segundos
            if _alive(): break
            time.sleep(0.3)

        if not _alive():
            # Aún si no detectamos alive, la librería puede estar funcionando
            # Verificamos si recibió datos
            time.sleep(3)
            d, r = _extract_balance()
            if d is not None or r is not None:
                log(f"[CONN] ✅ WS activo (balance detectado): Demo=${d} Real=${r}")
                _inject(expert)
                return True
            log(f"[CONN] ⚠️ WS status incierto, pero continuamos")
            _inject(expert)
            # Intentar pedir profile
            try:
                ws = getattr(expert, 'websocket_client', None)
                if ws and hasattr(ws, 'sock') and ws.sock:
                    ws.sock.send(json.dumps({"action":"profile"}))
                    time.sleep(3)
            except: pass
            return True  # Continuamos de todos modos

        _inject(expert)

        # Esperar balance (la librería ya recibió profile en connect)
        time.sleep(2)
        d, r = _extract_balance()
        if d is not None or r is not None:
            a = d if IS_DEMO else r
            log(f"[CONN] ✅ Demo=${d} Real=${r} Activo=${a}")
        else:
            # Pedir profile explícito
            try:
                ws = getattr(expert, 'websocket_client', None)
                if ws and hasattr(ws, 'sock') and ws.sock:
                    ws.sock.send(json.dumps({"action":"profile"}))
                    time.sleep(3)
                    d, r = _extract_balance()
                    log(f"[CONN] Balance post-request: Demo=${d} Real=${r}")
            except: pass
            if d is None and r is None:
                keys = list(getattr(expert, 'msg_by_action', {}).keys())
                log(f"[CONN] ⚠️ Sin balance. Keys: {keys}")

        return True
    except Exception as e:
        log(f"[CONN] ❌ {e}")
        expert = None
        return False

def _ensure():
    global _last_reconnect
    with _lock:
        # Lazy init
        global _initialized
        if not _initialized:
            _initialized = True
            _connect()
            return _alive() or expert is not None

        if _alive(): return True
        # También verificar si tiene balance (WS puede estar vivo pero sock check falla)
        d, r = _extract_balance()
        if d is not None or r is not None:
            return True
        if time.time() - _last_reconnect < 15: return expert is not None
        _last_reconnect = time.time()
        return _connect()

# ═══════════════════════════════════════════════════════════════════
# TRADE
# ═══════════════════════════════════════════════════════════════════
def _execute(asset, direction, tf='5M'):
    if not EoApi or not TOKEN:
        return {"status":"error","reason":"NO_CONFIG","details":"EoApi/TOKEN falta"}

    _ensure()
    if not expert:
        return {"status":"error","reason":"NO_CONNECTION","details":"Sin conexión"}

    eo_type = "call" if direction == "BUY" else "put"
    exp_t = TF_EXP.get(tf, 300)
    aid = ASSET_MAP.get(asset)
    if not aid:
        return {"status":"error","reason":"INVALID_ASSET","details":f"No mapeado: {asset}"}

    bal = _active_bal()
    if bal is not None and bal < AMOUNT:
        return {"status":"error","reason":"LOW_BALANCE","details":f"${bal:.2f}<${AMOUNT}"}

    m = "DEMO" if IS_DEMO else "REAL"
    log(f"[TRADE] 🎯 {eo_type} {asset}(ID:{aid}) ${AMOUNT} [{m}]")

    with _trade_lock:
        _trade_info['trade_id'] = None
        _trade_info['result'] = None

    sig.signal(sig.SIGALRM, _alarm)
    sig.alarm(10)
    result = None
    try:
        for attempt in range(2):
            try:
                result = expert.Buy(amount=AMOUNT, type=eo_type, assetid=aid,
                                     exptime=exp_t, isdemo=IS_DEMO, strike_time=time.time())
                break
            except Exception as e:
                if attempt == 1: raise
                time.sleep(0.5)
        sig.alarm(0)
    except TimeoutError_:
        return {"status":"error","reason":"TIMEOUT","details":"10s sin respuesta"}
    except Exception as e:
        sig.alarm(0)
        return {"status":"error","reason":"EXEC_ERROR","details":str(e)}

    # Esperar trade_id
    tid = None
    for _ in range(10):
        with _trade_lock: tid = _trade_info['trade_id']
        if tid: break
        time.sleep(0.3)

    if not isinstance(result, dict) or not result:
        return {"status":"error","reason":"REJECTED","details":f"{asset} inactivo/rechazado"}

    resp = {"status":"success","asset":asset,"direction":direction,"type":eo_type,
            "asset_id":aid,"amount":AMOUNT,"tf":tf,"mode":m,"trade_id":tid}
    with _trade_lock:
        tr = _trade_info.get('result')
    if tr: resp["open_rate"] = tr.get("open_rate")
    log(f"[TRADE] 📋 {json.dumps(resp)}")
    return resp

# ═══════════════════════════════════════════════════════════════════
# WATCHDOG (dentro del worker)
# ═══════════════════════════════════════════════════════════════════
def _watchdog():
    import urllib.request
    url = os.environ.get("RENDER_EXTERNAL_URL", "")
    while True:
        time.sleep(300)
        if url:
            try: urllib.request.urlopen(f"{url}/health", timeout=10)
            except: pass
        if _initialized and not _alive():
            d, r = _extract_balance()
            if d is None and r is None:
                with _lock: _connect()
        gc.collect()
        log(f"[WD] WS:{'✅' if _alive() else '❌'} Bal:${_active_bal()}")

threading.Thread(target=_watchdog, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# RUTAS
# ═══════════════════════════════════════════════════════════════════
def _auth():
    a = request.headers.get('Authorization','')
    return a.startswith('Bearer ') and a.split(' ',1)[1] == API_SECRET

@app.route('/health')
def health():
    _ensure()
    d, r = _extract_balance()
    return jsonify({"status":"ok","ws_alive":_alive(),"mode":"DEMO" if IS_DEMO else "REAL",
                    "balance":_active_bal(),"demo_balance":d,"real_balance":r,"trade_amount":AMOUNT})

@app.route('/trade', methods=['POST'])
def trade_route():
    if not _auth():
        return jsonify({"status":"error","reason":"AUTH","details":"No autorizado"}), 401
    data = request.json or {}
    d = data.get('direction','').upper()
    if d not in ('BUY','SELL'):
        return jsonify({"status":"error","reason":"BAD_DIR","details":f"Dirección: {d}"}), 400
    r = _execute(data.get('asset',''), d, data.get('tf','5M'))
    return jsonify(r), 200 if r["status"]=="success" else 500

@app.route('/broker-status')
def broker_status():
    _ensure()
    d, r = _extract_balance()
    alive = _alive() or (d is not None or r is not None)
    return jsonify({"connected":alive,"mode":"DEMO" if IS_DEMO else "REAL",
                    "balance":_active_bal(),"demo_balance":d,"real_balance":r,
                    "trade_amount":AMOUNT,"message":"OK" if alive else "Sin WS"})

@app.route('/debug')
def debug():
    _ensure()
    info = {"ws_alive":_alive(),"has_expert":expert is not None,"initialized":_initialized}
    if expert:
        try:
            store = getattr(expert, 'msg_by_action', {})
            info["msg_keys"] = list(store.keys()) if store else []
            pm = store.get('profile') if store else None
            if isinstance(pm, dict):
                inner = pm.get('profile', pm)
                if isinstance(inner, dict):
                    info["demo_balance"] = inner.get('demo_balance')
                    info["real_balance"] = inner.get('real_balance')
                    info["is_demo"] = inner.get('is_demo')
            info["threads"] = [t.name for t in threading.enumerate() if 'websocket' in t.name.lower() or 'thread' in t.name.lower()]
        except Exception as e:
            info["error"] = str(e)
    return jsonify(info)

# ═══════════════════════════════════════════════════════════════════
# NO conectar acá — gunicorn fork mata el proceso.
# La conexión se hace lazy en _ensure() al primer request.
# ═══════════════════════════════════════════════════════════════════
log(f"[BRIDGE] Módulo cargado. Modo={'DEMO' if IS_DEMO else 'REAL'} Monto=${AMOUNT}")
log(f"[BRIDGE] Conexión se hará al primer request (lazy init)")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

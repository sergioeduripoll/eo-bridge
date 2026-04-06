"""
app.py v8 — Puente ExpertOption FINAL
- Conexión LAZY (dentro del worker de gunicorn)
- _alive() usa websocket_thread.is_alive() (no sock.connected que falla)
- Interceptor captura profile/balance del stream WS
- Pide profile explícito después de inyectar interceptor
- Balance se cachea en _BAL dict
"""

import os, time, json, gc, threading, signal as sig

class TimeoutErr(Exception): pass
def _alarm_h(s, f): raise TimeoutErr()

def log(msg): print(msg, flush=True)

from flask import Flask, request, jsonify
app = Flask(__name__)

EoApi = None
for p in ['expert', 'ExpertOptionAPI.expert']:
    try:
        EoApi = getattr(__import__(p, fromlist=['EoApi']), 'EoApi')
        log(f"[BRIDGE] ✅ EoApi desde {p}")
        break
    except: continue

TOKEN  = os.environ.get("EO_TOKEN", "")
SERVER = os.environ.get("EO_SERVER", "wss://fr24g1eu.expertoption.com/")
SECRET = os.environ.get("BRIDGE_SECRET", "cambiar_esto")
AMOUNT = int(os.environ.get("TRADE_AMOUNT", "1"))
IS_DEMO= int(os.environ.get("EO_DEMO", "0"))

ASSETS = {"BTC/USD":160,"ETH/USD":162,"XRP/USD":173,"ADA/USD":235,"SOL/USD":464,"DOGE/USD":463,"BNB/USD":462}
TF_EXP = {'5M':300,'15M':900,'30M':1800,'1H':3600}

expert = None
_lock = threading.Lock()
_last_reconn = 0
_initialized = False

# Balance cache — actualizado por el interceptor
_BAL = {'demo': None, 'real': None, 'ts': 0}
_BAL_LOCK = threading.Lock()

# Trade info — actualizado por el interceptor  
_TRD = {'trade_id': None, 'result': None}
_TRD_LOCK = threading.Lock()

# ═══════════════════════════════════════════════════════════════════
def _alive():
    """Check si el WS está vivo usando el thread de la librería."""
    if not expert: return False
    try:
        wt = getattr(expert, 'websocket_thread', None)
        if wt and wt.is_alive(): return True
    except: pass
    return False

def _get_bal():
    with _BAL_LOCK:
        return _BAL['demo'] if IS_DEMO else _BAL['real']

def _parse_profile_from_msg(data):
    """Extrae balance de cualquier mensaje que contenga profile."""
    try:
        if not isinstance(data, dict): return
        
        # Directo: {"action":"profile","message":{"profile":{...}}}
        msg = data.get('message', data)
        if isinstance(msg, dict):
            prof = msg.get('profile', msg)
            if isinstance(prof, dict) and ('demo_balance' in prof or 'real_balance' in prof):
                with _BAL_LOCK:
                    if prof.get('demo_balance') is not None: _BAL['demo'] = float(prof['demo_balance'])
                    if prof.get('real_balance') is not None: _BAL['real'] = float(prof['real_balance'])
                    _BAL['ts'] = time.time()
                log(f"[BAL] Demo=${_BAL['demo']} Real=${_BAL['real']}")
                return
        
        # Dentro de multipleAction: {"action":"multipleAction","message":[{...},{...}]}
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    _parse_profile_from_msg(item)
    except: pass

# ═══════════════════════════════════════════════════════════════════
def _inject(exp):
    """Intercepta TODOS los mensajes del WS para capturar profile y trade_id."""
    ws = getattr(exp, 'websocket_client', None)
    if not ws: return False
    orig = ws.on_message

    def _on(ws_app, raw):
        # Primero pasar al original (la librería necesita procesar)
        if orig:
            try: orig(ws_app, raw)
            except: pass
        
        # Ahora nosotros procesamos
        try:
            if not isinstance(raw, str): return
            if len(raw) < 20: return
            
            # FAST: solo parsear si contiene algo que nos interesa
            if 'profile' in raw or 'balance' in raw:
                data = json.loads(raw)
                _parse_profile_from_msg(data)
            
            elif '"trade_id"' in raw:
                data = json.loads(raw)
                tid = data.get('message',{}).get('trade_id')
                if tid:
                    with _TRD_LOCK: _TRD['trade_id'] = tid
                    log(f"[WS] 🎫 trade_id: {tid}")
            
            elif 'openTradeSuccessful' in raw:
                data = json.loads(raw)
                t = data.get('message',{}).get('trade',{})
                if t:
                    with _TRD_LOCK:
                        _TRD['result'] = {'id':t.get('id'),'open_rate':t.get('open_rate'),'is_demo':t.get('is_demo')}
                    log(f"[WS] ✅ id={t.get('id')} open={t.get('open_rate')}")
        except: pass

    ws.on_message = _on
    log("[WS] ✅ Interceptor inyectado")
    return True

# ═══════════════════════════════════════════════════════════════════
def _connect():
    global expert
    if not EoApi or not TOKEN:
        log("[CONN] ❌ Sin EoApi/TOKEN"); return False
    try:
        if expert:
            try:
                ws = getattr(expert, 'websocket_client', None)
                if ws:
                    try: ws.close()
                    except: pass
            except: pass
            expert = None; gc.collect()

        m = "DEMO" if IS_DEMO else "REAL"
        log(f"[CONN] 🔌 Conectando ({m}, ${AMOUNT})...")
        expert = EoApi(token=TOKEN, server_region=SERVER)
        expert.connect()

        # Esperar que el thread del WS arranque
        for _ in range(50):
            if _alive(): break
            time.sleep(0.3)
        
        if not _alive():
            log("[CONN] ⚠️ Thread WS no detectado, esperando más...")
            time.sleep(5)
        
        # Inyectar interceptor — AHORA capturamos profile del stream
        _inject(expert)
        
        # Esperar a que la librería reciba profile (llega en los primeros segundos)
        time.sleep(3)
        
        # Si no llegó balance aún, pedir profile explícitamente
        if _get_bal() is None:
            log("[CONN] 📡 Solicitando profile...")
            try:
                ws = getattr(expert, 'websocket_client', None)
                if ws:
                    # Intentar enviar via el send de la librería
                    try:
                        ws.send(json.dumps({"action":"profile","ns":99}))
                    except:
                        # Fallback: enviar via sock directo
                        sock = getattr(ws, 'sock', None)
                        if sock:
                            sock.send(json.dumps({"action":"profile","ns":99}))
            except Exception as e:
                log(f"[CONN] Error solicitando profile: {e}")
            
            time.sleep(5)
            
            if _get_bal() is not None:
                log(f"[CONN] ✅ Balance obtenido: ${_get_bal()}")
            else:
                log(f"[CONN] ⚠️ Sin balance aún. El interceptor lo capturará cuando llegue.")
        else:
            log(f"[CONN] ✅ Balance inmediato: Demo=${_BAL['demo']} Real=${_BAL['real']}")
        
        return True
    except Exception as e:
        log(f"[CONN] ❌ {e}"); expert = None; return False

def _ensure():
    global _last_reconn, _initialized
    with _lock:
        if not _initialized:
            _initialized = True
            return _connect()
        if _alive(): return True
        if time.time() - _last_reconn < 15: return expert is not None
        _last_reconn = time.time()
        return _connect()

# ═══════════════════════════════════════════════════════════════════
def _execute(asset, direction, tf='5M'):
    if not EoApi or not TOKEN:
        return {"status":"error","reason":"NO_CONFIG","details":"Sin EoApi/TOKEN"}
    _ensure()
    if not expert:
        return {"status":"error","reason":"NO_CONNECTION","details":"Sin conexión"}

    eo_type = "call" if direction == "BUY" else "put"
    exp_t = TF_EXP.get(tf, 300)
    aid = ASSETS.get(asset)
    if not aid:
        return {"status":"error","reason":"INVALID_ASSET","details":f"No mapeado: {asset}"}

    bal = _get_bal()
    if bal is not None and bal < AMOUNT:
        return {"status":"error","reason":"LOW_BAL","details":f"${bal:.2f}<${AMOUNT}"}

    m = "DEMO" if IS_DEMO else "REAL"
    log(f"[TRADE] 🎯 {eo_type} {asset}(ID:{aid}) ${AMOUNT} [{m}] bal=${bal}")

    with _TRD_LOCK: _TRD['trade_id'] = None; _TRD['result'] = None

    sig.signal(sig.SIGALRM, _alarm_h)
    sig.alarm(10)
    result = None
    try:
        for att in range(2):
            try:
                result = expert.Buy(amount=AMOUNT, type=eo_type, assetid=aid,
                                     exptime=exp_t, isdemo=IS_DEMO, strike_time=time.time())
                break
            except Exception as e:
                if att == 1: raise
                time.sleep(0.5)
        sig.alarm(0)
    except TimeoutErr:
        return {"status":"error","reason":"TIMEOUT","details":"10s"}
    except Exception as e:
        sig.alarm(0)
        return {"status":"error","reason":"EXEC_ERROR","details":str(e)}

    tid = None
    for _ in range(10):
        with _TRD_LOCK: tid = _TRD['trade_id']
        if tid: break
        time.sleep(0.3)

    if not isinstance(result, dict) or not result:
        return {"status":"error","reason":"REJECTED","details":f"{asset} inactivo"}

    resp = {"status":"success","asset":asset,"direction":direction,"type":eo_type,
            "asset_id":aid,"amount":AMOUNT,"tf":tf,"mode":m,"trade_id":tid}
    with _TRD_LOCK:
        tr = _TRD.get('result')
    if tr: resp["open_rate"] = tr.get("open_rate")
    log(f"[TRADE] 📋 {json.dumps(resp)}")
    return resp

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
            with _lock: _connect()
        gc.collect()
        log(f"[WD] alive={'✅' if _alive() else '❌'} bal=${_get_bal()}")
threading.Thread(target=_watchdog, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
def _auth():
    a = request.headers.get('Authorization','')
    return a.startswith('Bearer ') and a.split(' ',1)[1] == SECRET

@app.route('/health')
def health():
    _ensure()
    return jsonify({"status":"ok","ws_alive":_alive(),"mode":"DEMO" if IS_DEMO else "REAL",
                    "balance":_get_bal(),"demo_balance":_BAL['demo'],"real_balance":_BAL['real'],
                    "trade_amount":AMOUNT})

@app.route('/trade', methods=['POST'])
def trade_route():
    if not _auth():
        return jsonify({"status":"error","reason":"AUTH","details":"No autorizado"}), 401
    d = (request.json or {})
    dr = d.get('direction','').upper()
    if dr not in ('BUY','SELL'):
        return jsonify({"status":"error","reason":"BAD_DIR","details":dr}), 400
    r = _execute(d.get('asset',''), dr, d.get('tf','5M'))
    return jsonify(r), 200 if r["status"]=="success" else 500

@app.route('/broker-status')
def broker_status():
    _ensure()
    alive = _alive()
    # Si estamos vivos pero sin balance, pedir profile
    if alive and _get_bal() is None:
        try:
            ws = getattr(expert, 'websocket_client', None)
            if ws:
                try: ws.send(json.dumps({"action":"profile","ns":99}))
                except:
                    sock = getattr(ws, 'sock', None)
                    if sock: sock.send(json.dumps({"action":"profile","ns":99}))
                time.sleep(3)
        except: pass
    return jsonify({"connected":alive,"mode":"DEMO" if IS_DEMO else "REAL",
                    "balance":_get_bal(),"demo_balance":_BAL['demo'],"real_balance":_BAL['real'],
                    "trade_amount":AMOUNT,"message":"OK" if alive else "Sin WS"})

@app.route('/debug')
def debug():
    _ensure()
    info = {"ws_alive":_alive(),"expert":expert is not None,"init":_initialized,
            "bal_demo":_BAL['demo'],"bal_real":_BAL['real'],"bal_ts":_BAL['ts']}
    if expert:
        try:
            wt = getattr(expert, 'websocket_thread', None)
            info["ws_thread_alive"] = wt.is_alive() if wt else False
            info["profile_attr"] = str(getattr(expert, 'profile', 'N/A'))[:200]
            store = getattr(expert, 'msg_by_action', {})
            info["msg_keys"] = list(store.keys()) if store else []
        except Exception as e:
            info["err"] = str(e)
        info["threads"] = [t.name for t in threading.enumerate()]
    return jsonify(info)

# Captura temporal de mensajes para diagnóstico
_CAPTURE = []
_CAPTURING = False

@app.route('/capture')
def capture():
    """Captura 8s de mensajes WS raw, envía profile request, muestra resultado."""
    global _CAPTURING
    _ensure()
    if not expert:
        return jsonify({"error":"no expert"})
    
    _CAPTURE.clear()
    _CAPTURING = True
    
    # Inyectar capturador temporal
    ws = getattr(expert, 'websocket_client', None)
    if not ws:
        return jsonify({"error":"no ws client"})
    
    current_handler = ws.on_message
    
    def capture_handler(ws_app, raw):
        if _CAPTURING and isinstance(raw, str) and 'candle' not in raw and 'pong' not in raw and 'tradersChoice' not in raw and 'optStatus' not in raw:
            _CAPTURE.append(raw[:500])
        if current_handler:
            try: current_handler(ws_app, raw)
            except: pass
    
    ws.on_message = capture_handler
    
    # Pedir profile
    try:
        ws.send(json.dumps({"action":"profile","ns":99}))
    except Exception as e1:
        try:
            sock = getattr(ws, 'sock', None)
            if sock: sock.send(json.dumps({"action":"profile","ns":99}))
        except Exception as e2:
            _CAPTURING = False
            ws.on_message = current_handler
            return jsonify({"error":f"send failed: {e1} / {e2}"})
    
    # Esperar 8 segundos
    time.sleep(8)
    _CAPTURING = False
    ws.on_message = current_handler
    
    return jsonify({
        "captured_count": len(_CAPTURE),
        "messages": _CAPTURE[:30],
        "bal_after": {"demo":_BAL['demo'],"real":_BAL['real']},
    })

# ═══════════════════════════════════════════════════════════════════
log(f"[BRIDGE] Cargado. {'DEMO' if IS_DEMO else 'REAL'} ${AMOUNT}. Lazy init.")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT",8080)))

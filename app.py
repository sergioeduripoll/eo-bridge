"""
app.py v9 — Puente ExpertOption DEFINITIVO
- Usa expert.Profile() para pedir balance (método nativo de la librería)
- Usa expert.send_websocket_request() para enviar mensajes
- _alive() via websocket_thread.is_alive()
- Balance se lee de expert.profile después de llamar Profile()
- Conexión LAZY dentro del worker de gunicorn
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

ASSETS = {"BTC/USD":160,"ETH/USD":162,"XRP/USD":173,
          "ADA/USD":235,"SOL/USD":464,"DOGE/USD":463,"BNB/USD":462}
TF_EXP = {'5M':300,'15M':900,'30M':1800,'1H':3600}

expert = None
_lock = threading.Lock()
_last_reconn = 0
_initialized = False
_TRD = {'trade_id': None, 'result': None}
_TRD_LOCK = threading.Lock()

# ═══════════════════════════════════════════════════════════════════
# BALANCE — Lee de expert.profile (lo llena expert.Profile())
# ═══════════════════════════════════════════════════════════════════
def _fetch_balance():
    """Llama expert.Profile() y lee el resultado de expert.profile."""
    if not expert: return None, None
    try:
        expert.Profile()
        time.sleep(2)  # Esperar respuesta del broker
        
        p = getattr(expert, 'profile', None)
        if p and isinstance(p, dict):
            prof = p.get('profile', p)
            if isinstance(prof, dict):
                return prof.get('demo_balance'), prof.get('real_balance')
        
        # Si profile es el objeto directo
        if p and hasattr(p, 'get'):
            return p.get('demo_balance'), p.get('real_balance')
    except Exception as e:
        log(f"[BAL] Error: {e}")
    return None, None

def _get_bal():
    if not expert: return None
    p = getattr(expert, 'profile', None)
    if p and isinstance(p, dict):
        prof = p.get('profile', p)
        if isinstance(prof, dict):
            return prof.get('demo_balance') if IS_DEMO else prof.get('real_balance')
    return None

def _alive():
    if not expert: return False
    try:
        wt = getattr(expert, 'websocket_thread', None)
        return wt and wt.is_alive()
    except: return False

# ═══════════════════════════════════════════════════════════════════
# INTERCEPTOR — Captura trade_id de respuestas del broker
# ═══════════════════════════════════════════════════════════════════
def _inject(exp):
    ws = getattr(exp, 'websocket_client', None)
    if not ws: return
    
    # El WS real es ws.wss (WebSocketApp), los callbacks están ahí
    wss = getattr(ws, 'wss', None)
    if not wss: return
    
    orig = wss.on_message
    
    def _on(wss_app, raw):
        # Primero dejar que la librería procese
        if orig:
            try: orig(wss_app, raw)
            except: pass
        
        # Nosotros capturamos trade_id y openTradeSuccessful
        try:
            if not isinstance(raw, str) or len(raw) < 20: return
            
            if '"trade_id"' in raw:
                d = json.loads(raw)
                tid = d.get('message',{}).get('trade_id')
                if tid:
                    with _TRD_LOCK: _TRD['trade_id'] = tid
                    log(f"[WS] 🎫 trade_id: {tid}")
            
            elif 'openTradeSuccessful' in raw:
                d = json.loads(raw)
                t = d.get('message',{}).get('trade',{})
                if t:
                    with _TRD_LOCK:
                        _TRD['result'] = {'id':t.get('id'),'open_rate':t.get('open_rate'),'is_demo':t.get('is_demo')}
                    log(f"[WS] ✅ id={t.get('id')} open={t.get('open_rate')}")
        except: pass
    
    wss.on_message = _on
    log("[WS] ✅ Interceptor en wss.on_message")

# ═══════════════════════════════════════════════════════════════════
# CONEXIÓN
# ═══════════════════════════════════════════════════════════════════
def _connect():
    global expert
    if not EoApi or not TOKEN:
        log("[CONN] ❌ Sin EoApi/TOKEN"); return False
    try:
        if expert:
            try: getattr(expert,'websocket_client',None) and expert.websocket_client.reconnect()
            except: pass
            expert = None; gc.collect()

        m = "DEMO" if IS_DEMO else "REAL"
        log(f"[CONN] 🔌 ({m}, ${AMOUNT})...")
        expert = EoApi(token=TOKEN, server_region=SERVER)
        expert.connect()

        for _ in range(50):
            if _alive(): break
            time.sleep(0.3)
        
        if not _alive():
            time.sleep(5)
        
        log(f"[CONN] WS thread alive: {_alive()}")
        
        # Inyectar interceptor en el WebSocketApp real (wss)
        _inject(expert)
        
        # Pedir profile usando el método nativo
        time.sleep(2)
        log("[CONN] 📡 Pidiendo Profile()...")
        demo, real = _fetch_balance()
        
        if demo is not None or real is not None:
            active = demo if IS_DEMO else real
            log(f"[CONN] ✅ Demo=${demo} Real=${real} Activo=${active}")
        else:
            log(f"[CONN] ⚠️ Profile() no devolvió balance. expert.profile={getattr(expert,'profile',None)}")
        
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
# TRADE
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
# WATCHDOG
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
# RUTAS
# ═══════════════════════════════════════════════════════════════════
def _auth():
    a = request.headers.get('Authorization','')
    return a.startswith('Bearer ') and a.split(' ',1)[1] == SECRET

@app.route('/health')
def health():
    _ensure()
    bal = _get_bal()
    demo, real = _fetch_balance() if bal is None else (None, None)
    if bal is None: bal = _get_bal()
    return jsonify({"status":"ok","ws_alive":_alive(),"mode":"DEMO" if IS_DEMO else "REAL",
                    "balance":_get_bal(),"trade_amount":AMOUNT})

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
    # Siempre pedir balance fresco
    demo, real = _fetch_balance()
    bal = _get_bal()
    return jsonify({"connected":_alive(),"mode":"DEMO" if IS_DEMO else "REAL",
                    "balance":bal,"demo_balance":demo,"real_balance":real,
                    "trade_amount":AMOUNT,"message":"OK" if _alive() else "Sin WS"})

@app.route('/debug')
def debug():
    _ensure()
    info = {"ws_alive":_alive(),"expert":expert is not None}
    if expert:
        # 1. Qué retorna Profile()?
        try:
            ret = expert.Profile()
            info["Profile_return"] = str(ret)[:500]
            info["Profile_return_type"] = str(type(ret).__name__)
        except Exception as e:
            info["Profile_error"] = str(e)
        
        time.sleep(3)
        info["profile_attr_after"] = str(getattr(expert, 'profile', None))[:500]
        
        # 2. Qué hay en msg_by_action ahora?
        store = getattr(expert, 'msg_by_action', {})
        info["msg_keys"] = list(store.keys()) if store else []
        # Ver si profile apareció
        for k in store.keys():
            val = store[k]
            s = str(val)
            if 'balance' in s.lower() or 'profile' in s.lower():
                info[f"store_{k}"] = s[:500]
        
        # 3. Qué retorna SetDemo?
        try:
            ret2 = expert.SetDemo()
            info["SetDemo_return"] = str(ret2)[:300]
        except Exception as e:
            info["SetDemo_error"] = str(e)
        
        # 4. Revisar msg_by_ns
        ns_store = getattr(expert, 'msg_by_ns', None)
        if ns_store:
            info["msg_by_ns_keys"] = list(ns_store.keys())[:20]
            for k in list(ns_store.keys())[:5]:
                v = ns_store[k]
                if v is not None:
                    info[f"ns_{k}"] = str(v)[:300]
        
        # 5. Revisar results
        res_store = getattr(expert, 'results', None)
        if res_store:
            info["results_keys"] = list(res_store.keys())[:20]
    return jsonify(info)

# ═══════════════════════════════════════════════════════════════════
log(f"[BRIDGE] v9. {'DEMO' if IS_DEMO else 'REAL'} ${AMOUNT}. Lazy init.")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT",8080)))

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
IS_DEMO= int(os.environ.get("EO_DEMO", "1"))             # <── 1=DEMO, 0=REAL
TRADE_PCT = int(os.environ.get("TRADE_PERCENT", "10"))    # <── 10% del saldo
MIN_AMOUNT = 1                                             # <── Mínimo $1

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
    """Llama expert.Profile() — retorna el dict directamente."""
    if not expert: return None, None
    try:
        ret = expert.Profile()
        if ret and isinstance(ret, dict):
            msg = ret.get('message', ret)
            prof = msg.get('profile', msg) if isinstance(msg, dict) else msg
            if isinstance(prof, dict):
                return prof.get('demo_balance'), prof.get('real_balance')
    except Exception as e:
        log(f"[BAL] Error: {e}")
    return None, None

def _get_bal():
    demo, real = _fetch_balance()
    return demo if IS_DEMO else real

def _calc_amount():
    """Calcula monto = TRADE_PCT% del saldo, redondeado abajo, mínimo $1."""
    bal = _get_bal()
    if bal is None or bal < MIN_AMOUNT:
        return MIN_AMOUNT
    amount = int(bal * TRADE_PCT / 100)  # int() trunca hacia abajo
    return max(amount, MIN_AMOUNT)

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
        
        # ═══════════════════════════════════════════════════════
        # CRÍTICO: Cambiar contexto a REAL si IS_DEMO=0
        # Sin esto, el broker rechaza con ERROR_EXPECT_DEMO_CONTEXT
        # SetDemo() sin args = modo demo, SetDemo() retorna True
        # Para REAL: necesitamos cambiar el contexto de la sesión
        # ═══════════════════════════════════════════════════════
        time.sleep(2)
        if IS_DEMO == 0:
            try:
                # Enviar setContext para cambiar a real (is_demo=0)
                expert.send_websocket_request(action="setContext", msg={"is_demo": 0})
                time.sleep(1)
                log("[CONN] 💵 Contexto cambiado a REAL")
            except Exception as e:
                log(f"[CONN] ⚠️ Error cambiando contexto: {e}")
        else:
            try:
                expert.SetDemo()
                log("[CONN] 🧪 Contexto DEMO")
            except: pass
        
        # Inyectar interceptor en el WebSocketApp real (wss)
        _inject(expert)
        
        # Pedir profile usando el método nativo
        time.sleep(2)
        log("[CONN] 📡 Profile()...")
        demo, real = _fetch_balance()
        
        if demo is not None or real is not None:
            active = demo if IS_DEMO else real
            log(f"[CONN] ✅ Demo=${demo} Real=${real} Activo=${active}")
        else:
            log(f"[CONN] ⚠️ Profile() sin balance")
        
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

    # Pre-check: verificar que el WS funciona intentando Profile()
    try:
        test = expert.Profile()
        if not test or not isinstance(test, dict):
            raise Exception("Profile vacío")
    except:
        log("[TRADE] ⚠️ WS muerto, reconectando antes de operar...")
        with _lock: _connect()
        if not expert:
            return {"status":"error","reason":"RECONNECT_FAIL","details":"No se pudo reconectar"}

    eo_type = "call" if direction == "BUY" else "put"
    exp_t = TF_EXP.get(tf, 300)
    aid = ASSETS.get(asset)
    if not aid:
        return {"status":"error","reason":"INVALID_ASSET","details":f"No mapeado: {asset}"}

    trade_amount = _calc_amount()
    m = "DEMO" if IS_DEMO else "REAL"
    log(f"[TRADE] 🎯 {eo_type} {asset}(ID:{aid}) ${trade_amount} [{m}] (bal=${_get_bal()} {TRADE_PCT}%)")

    with _TRD_LOCK: _TRD['trade_id'] = None; _TRD['result'] = None

    sig.signal(sig.SIGALRM, _alarm_h)
    sig.alarm(10)
    result = None
    try:
        for att in range(2):
            try:
                result = expert.Buy(amount=trade_amount, type=eo_type, assetid=aid,
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
            "asset_id":aid,"amount":trade_amount,"tf":tf,"mode":m,"trade_id":tid}
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
        time.sleep(120)  # Cada 2 min (antes 5)
        if url:
            try: urllib.request.urlopen(f"{url}/health", timeout=10)
            except: pass
        # Verificar si podemos hacer Profile() — si falla, reconectar
        if _initialized:
            try:
                ret = expert.Profile() if expert else None
                if not ret or not isinstance(ret, dict):
                    raise Exception("Profile vacío")
            except:
                log("[WD] ⚠️ Profile falló, reconectando...")
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
    demo, real = _fetch_balance()
    bal = demo if IS_DEMO else real
    return jsonify({"status":"ok","ws_alive":_alive(),"mode":"DEMO" if IS_DEMO else "REAL",
                    "balance":bal,"trade_pct":TRADE_PCT,"next_amount":_calc_amount()})

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
    demo, real = _fetch_balance()
    bal = demo if IS_DEMO else real
    return jsonify({"connected":_alive(),"mode":"DEMO" if IS_DEMO else "REAL",
                    "balance":bal,"demo_balance":demo,"real_balance":real,
                    "trade_pct":TRADE_PCT,"next_amount":_calc_amount(),
                    "message":"OK" if _alive() else "Sin WS"})

@app.route('/debug')
def debug():
    _ensure()
    demo, real = _fetch_balance()
    bal = demo if IS_DEMO else real
    return jsonify({"ws_alive":_alive(),"mode":"DEMO" if IS_DEMO else "REAL",
                    "demo_balance":demo,"real_balance":real,"active_balance":bal,
                    "trade_amount":AMOUNT})

# ═══════════════════════════════════════════════════════════════════
log(f"[BRIDGE] v10. {'DEMO' if IS_DEMO else 'REAL'} {TRADE_PCT}% del saldo. Lazy init.")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT",8080)))

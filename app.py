"""
app.py — Puente ExpertOption v5 — Cuenta REAL habilitada
- UNA sola instancia global de EoApi conectada al arrancar
- WS Interceptor con captura de trade_id y balance real/demo
- FIX: Eliminado SetReal() — el modo se controla en cada Buy() con isdemo=0/1
- FIX: Balance lee demo_balance o real_balance según IS_DEMO
- FIX: Captura trade_id de openTradeSuccessful para trazabilidad
- gc.collect() periódico optimizado (cada 20 ciclos)
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

# ═══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN DE MONTO Y MODO
# ═══════════════════════════════════════════════════════════════════
# TRADE_AMOUNT: Monto en USD por operación. Cambiar a 2, 3, 5, etc.
# IS_DEMO: 0 = cuenta REAL | 1 = cuenta DEMO
# ═══════════════════════════════════════════════════════════════════
AMOUNT = int(os.environ.get("TRADE_AMOUNT", "1"))       # <── MONTO: $1 por defecto
IS_DEMO = int(os.environ.get("EO_DEMO", "0"))           # <── MODO: 0=REAL, 1=DEMO

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
    'demo_balance': None,
    'real_balance': None,
    'assets': {},
    'profile': None,
    'last_trade_id': None,
    'last_trade_result': None,
}
WS_RAW_LOCK = threading.Lock()
MAX_ASSETS = 50  # Solo necesitamos ~7 crypto, 50 es margen de sobra

# ═══════════════════════════════════════════════════════════════════
# WS INTERCEPTOR — Solo captura lo necesario
# Candles/pong se descartan en FAST PATH sin parsear JSON completo
# ═══════════════════════════════════════════════════════════════════

def inject_ws_interceptor(expert):
    ws = getattr(expert, 'websocket_client', None)
    if ws is None:
        log("[WS-HOOK] ⚠️ websocket_client no encontrado")
        return False

    original_on_message = ws.on_message

    def intercepted_on_message(ws_app, message):
        try:
            if not isinstance(message, str):
                # Binario — pasar directo, no parsear
                if original_on_message:
                    original_on_message(ws_app, message)
                return

            # ── FAST PATH: Descartar mensajes de alta frecuencia SIN parsear JSON completo ──
            # candles llegan ~1/seg, pong cada ~30s — son el 95% del tráfico
            if '"action":"candles"' in message or '"action":"pong"' in message:
                if original_on_message:
                    original_on_message(ws_app, message)
                return
            if '"action":"tradersChoice"' in message:
                if original_on_message:
                    original_on_message(ws_app, message)
                return

            # ── SLOW PATH: Solo parsear JSON para mensajes que nos importan ──
            data = json.loads(message)
            action = data.get('action', '')

            if action == 'profile':
                _handle_profile(data.get('message', data))

            elif action == 'assets':
                _parse_raw_assets(data.get('message', data))

            elif action == 'multipleAction':
                msg = data.get('message', data.get('data', []))
                if isinstance(msg, list):
                    for sub in msg[:10]:  # Limitar a 10 sub-acciones para no explotar
                        if isinstance(sub, dict):
                            sub_action = sub.get('action', '')
                            if sub_action == 'profile':
                                _handle_profile(sub.get('message', sub))
                            elif sub_action == 'assets':
                                _parse_raw_assets(sub.get('message', sub))

            elif action == 'buyOption':
                msg = data.get('message', {})
                if isinstance(msg, dict) and 'trade_id' in msg:
                    with WS_RAW_LOCK:
                        WS_RAW_DATA['last_trade_id'] = msg['trade_id']
                    log(f"[WS-HOOK] 🎫 trade_id: {msg['trade_id']}")

            elif action == 'openTradeSuccessful':
                trade = data.get('message', {}).get('trade', {})
                if trade:
                    with WS_RAW_LOCK:
                        WS_RAW_DATA['last_trade_result'] = {
                            'id': trade.get('id'),
                            'open_rate': trade.get('open_rate'),
                            'is_demo': trade.get('is_demo'),
                            'asset_id': trade.get('asset_id'),
                            'amount': trade.get('amount'),
                        }
                    log(f"[WS-HOOK] ✅ Trade OK: id={trade.get('id')} open={trade.get('open_rate')}")

            # Todas las demás acciones: ignorar silenciosamente

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


def _handle_profile(msg):
    """Extrae SOLO los balances del mensaje profile. No guarda el objeto completo."""
    if not isinstance(msg, dict):
        return
    
    # El profile puede venir anidado: {profile: {...}} o directamente {demo_balance: ...}
    profile_data = msg.get('profile', msg)
    if not isinstance(profile_data, dict):
        return

    with WS_RAW_LOCK:
        # NO guardar el profile completo — solo los 2 números que necesitamos
        WS_RAW_DATA['profile'] = None  # No retener el objeto grande

        demo_bal = profile_data.get('demo_balance')
        real_bal = profile_data.get('real_balance')

        if demo_bal is not None:
            try:
                WS_RAW_DATA['demo_balance'] = float(demo_bal)
            except (ValueError, TypeError):
                pass

        if real_bal is not None:
            try:
                WS_RAW_DATA['real_balance'] = float(real_bal)
            except (ValueError, TypeError):
                pass

        # Balance activo según modo
        if IS_DEMO == 1:
            WS_RAW_DATA['balance'] = WS_RAW_DATA['demo_balance']
        else:
            WS_RAW_DATA['balance'] = WS_RAW_DATA['real_balance']

        log(f"[PROFILE] Demo: ${WS_RAW_DATA['demo_balance']} | Real: ${WS_RAW_DATA['real_balance']} | Activo: ${WS_RAW_DATA['balance']} ({'DEMO' if IS_DEMO else 'REAL'})")


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
        # ── LIMPIEZA AGRESIVA antes de reconectar ──
        if expert is not None:
            try:
                ws = getattr(expert, 'websocket_client', None)
                if ws:
                    # Desuscribir candles para frenar el flood de mensajes
                    try:
                        sock = getattr(ws, 'sock', None)
                        if sock and sock.connected:
                            sock.send(json.dumps({"action": "unsubscribeCandles"}))
                    except: pass
                    # Matar callback para romper chain de closures
                    try:
                        ws.on_message = None
                        ws.on_error = None
                        ws.on_close = None
                    except: pass
                    try:
                        ws.close()
                    except: pass
            except: pass
            expert = None
            # Limpiar datos stale
            with WS_RAW_LOCK:
                WS_RAW_DATA['balance'] = None
                WS_RAW_DATA['demo_balance'] = None
                WS_RAW_DATA['real_balance'] = None
                WS_RAW_DATA['profile'] = None
                WS_RAW_DATA['last_trade_id'] = None
                WS_RAW_DATA['last_trade_result'] = None
            # GC agresivo post-desconexión
            gc.collect()
            gc.collect()  # Doble para romper ciclos

        mode_str = "DEMO" if IS_DEMO else "REAL"
        log(f"[GLOBAL] 🔌 Conectando a ExpertOption (modo {mode_str}, monto ${AMOUNT})...")
        expert = EoApi(token=TOKEN, server_region=SERVER)
        
        # CRÍTICO: Inyectar interceptor ANTES de connect()
        # así capturamos los mensajes profile/assets que llegan en el handshake
        inject_ws_interceptor(expert)
        
        expert.connect()

        start = time.time()
        while time.time() - start < 15:
            if is_ws_alive(): break
            time.sleep(0.3)

        # Re-inyectar por si connect() reemplazó los callbacks
        inject_ws_interceptor(expert)

        # NO llamar a SetReal()/SetDemo() — no existe en la librería
        # El modo se controla en cada Buy() con isdemo=IS_DEMO

        # Pedir profile explícitamente (puede que el del handshake ya fue capturado)
        time.sleep(1)  # Dar tiempo al WS para estabilizarse
        for retry in range(3):
            try:
                ws_sock = getattr(expert.websocket_client, 'sock', None)
                if ws_sock and ws_sock.connected:
                    ws_sock.send(json.dumps({"action": "profile"}))
                    ws_sock.send(json.dumps({"action": "assets"}))
                    log(f"[GLOBAL] 📡 profile+assets solicitados (intento {retry+1})")
                    break
            except:
                time.sleep(1)

        # Esperar balance con timeout generoso (10s)
        poll_start = time.time()
        while time.time() - poll_start < 10:
            with WS_RAW_LOCK:
                bal = WS_RAW_DATA['balance']
                demo = WS_RAW_DATA['demo_balance']
                real = WS_RAW_DATA['real_balance']
                ac = len(WS_RAW_DATA['assets'])
            
            # Aceptar si tenemos CUALQUIER balance (demo o real)
            if demo is not None or real is not None:
                active_bal = demo if IS_DEMO else real
                WS_RAW_DATA['balance'] = active_bal
                log(f"[GLOBAL] ✅ Conectado | Modo: {mode_str} | Demo: ${demo} | Real: ${real} | Activo: ${active_bal} | Assets: {ac}")
                return True
            time.sleep(0.5)

        # Último intento: pedir profile una vez más
        try:
            ws_sock = getattr(expert.websocket_client, 'sock', None)
            if ws_sock and ws_sock.connected:
                ws_sock.send(json.dumps({"action": "profile"}))
                time.sleep(3)
                with WS_RAW_LOCK:
                    demo = WS_RAW_DATA['demo_balance']
                    real = WS_RAW_DATA['real_balance']
                if demo is not None or real is not None:
                    active_bal = demo if IS_DEMO else real
                    with WS_RAW_LOCK:
                        WS_RAW_DATA['balance'] = active_bal
                    log(f"[GLOBAL] ✅ Conectado (retry) | Activo: ${active_bal}")
                    return True
        except: pass

        log(f"[GLOBAL] ⚠️ Conectado pero balance no recibido via WS. Intentando leer de la librería...")
        
        # FALLBACK: Leer balance directamente de los atributos internos de EoApi
        try:
            # La librería puede guardar el profile internamente
            for attr in ['profile', 'user', '_profile', 'account']:
                obj = getattr(expert, attr, None)
                if obj and isinstance(obj, dict):
                    demo = obj.get('demo_balance')
                    real = obj.get('real_balance')
                    if demo is not None or real is not None:
                        with WS_RAW_LOCK:
                            if demo is not None: WS_RAW_DATA['demo_balance'] = float(demo)
                            if real is not None: WS_RAW_DATA['real_balance'] = float(real)
                            WS_RAW_DATA['balance'] = float(demo) if IS_DEMO else float(real) if real else float(demo)
                        log(f"[GLOBAL] ✅ Balance leído de expert.{attr}: Demo=${demo} Real=${real}")
                        return True
            
            # Listar atributos del expert para debug
            attrs = [a for a in dir(expert) if not a.startswith('__')]
            log(f"[GLOBAL] 📋 Atributos de expert: {attrs[:30]}")
        except Exception as ex:
            log(f"[GLOBAL] Fallback falló: {ex}")

        return True
    except Exception as e:
        log(f"[GLOBAL] ❌ Error: {e}")
        expert = None
        gc.collect()
        return False

def ensure_connection():
    global expert, LAST_RECONNECT

    with expert_lock:
        if is_ws_alive():
            return True

        if time.time() - LAST_RECONNECT < 10:
            return False

        LAST_RECONNECT = time.time()
        log("[GLOBAL] 🔄 Reconectando con cooldown...")
        return connect_global()

# ═══════════════════════════════════════════════════════════════════
# TRADE EXECUTION
# ═══════════════════════════════════════════════════════════════════

def execute_trade(asset_str, direction, tf='5M', amount_override=None):
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

    # Balance check
    with WS_RAW_LOCK:
        balance = WS_RAW_DATA['balance']
    
    wait_time = 0
    while balance is None and wait_time < 3:
        time.sleep(0.5)
        wait_time += 0.5
        with WS_RAW_LOCK:
            balance = WS_RAW_DATA['balance']

    trade_amount = amount_override if amount_override else AMOUNT

    # Verificar saldo suficiente
    if balance is not None and balance < trade_amount:
        return {
            "status": "error",
            "reason": "INSUFFICIENT_BALANCE",
            "details": f"Saldo ${balance:.2f} insuficiente para ${trade_amount}"
        }

    mode_str = "DEMO" if IS_DEMO else "REAL"
    log(f"[TRADE] 💰 Modo: {mode_str} | Saldo: ${balance if balance is not None else '?'} | Monto: ${trade_amount}")

    # Limpiar last_trade_id antes de ejecutar
    with WS_RAW_LOCK:
        WS_RAW_DATA['last_trade_id'] = None
        WS_RAW_DATA['last_trade_result'] = None

    log(f"[TRADE] 🎯 {eo_type.upper()} {asset_str} (ID:{asset_id}) ${trade_amount} [{mode_str}]")
    
    def safe_buy():
        return expert.Buy(
            amount=trade_amount,
            type=eo_type,
            assetid=asset_id,
            exptime=exp_time,
            isdemo=IS_DEMO,
            strike_time=time.time()
        )

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

    log(f"[TRADE] ✅ Respuesta Buy(): {result}")

    # Esperar trade_id del interceptor (máx 3s)
    trade_id = None
    trade_result = None
    wait_start = time.time()
    while time.time() - wait_start < 3:
        with WS_RAW_LOCK:
            trade_id = WS_RAW_DATA['last_trade_id']
            trade_result = WS_RAW_DATA['last_trade_result']
        if trade_id:
            break
        time.sleep(0.3)

    if not isinstance(result, dict) or not result:
        return {
            "status": "error",
            "reason": "ASSET_INACTIVE",
            "details": f"{asset_str} no disponible o mercado cerrado"
        }

    # GC periódico
    GC_COUNTER += 1
    if GC_COUNTER % 20 == 0:
        collected = gc.collect()
        log(f"[GC] Limpiados {collected} objetos")

    response = {
        "status": "success",
        "asset": asset_str,
        "direction": direction,
        "type": eo_type,
        "asset_id": asset_id,
        "amount": trade_amount,
        "tf": tf,
        "exp_seconds": exp_time,
        "mode": "DEMO" if IS_DEMO else "REAL",
        "trade_id": trade_id,
    }

    if trade_result:
        response["open_rate"] = trade_result.get("open_rate")
        response["broker_confirmed"] = True

    log(f"[TRADE] 📋 Resultado final: {json.dumps(response)}")
    return response

# ═══════════════════════════════════════════════════════════════════
# KEEP-ALIVE + MEMORY WATCHDOG
# ═══════════════════════════════════════════════════════════════════

def keep_alive_watchdog():
    import urllib.request
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    while True:
        time.sleep(300)  # Cada 5 min (antes era 10)

        # Self-ping para evitar sleep de Render
        if render_url:
            try:
                urllib.request.urlopen(f"{render_url}/health", timeout=10)
            except Exception: pass
        
        # Reconectar si WS murió
        if not is_ws_alive():
            with expert_lock: connect_global()
        
        # ── GC AGRESIVO cada ciclo ──
        collected = gc.collect()
        collected2 = gc.collect()  # Segunda pasada para ciclos
        
        # ── MONITOR DE MEMORIA ──
        try:
            import resource
            mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            log(f"[WATCHDOG] Mem: {mem_mb:.0f}MB | GC: {collected}+{collected2} | WS: {'✅' if is_ws_alive() else '❌'}")
            
            # Si la memoria supera 400MB, limpiar assets y forzar GC
            if mem_mb > 400:
                log(f"[WATCHDOG] ⚠️ Memoria alta ({mem_mb:.0f}MB) — limpieza de emergencia")
                with WS_RAW_LOCK:
                    # Reducir assets a solo los 7 crypto que usamos
                    crypto_ids = set(STATIC_ASSET_MAP.values())
                    WS_RAW_DATA['assets'] = {
                        k: v for k, v in WS_RAW_DATA['assets'].items() 
                        if k in crypto_ids
                    }
                gc.collect()
                gc.collect()
        except ImportError:
            log(f"[WATCHDOG] GC: {collected}+{collected2} | WS: {'✅' if is_ws_alive() else '❌'}")

threading.Thread(target=keep_alive_watchdog, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# RUTAS
# ═══════════════════════════════════════════════════════════════════

def check_auth():
    auth = request.headers.get('Authorization', '')
    return auth.startswith('Bearer ') and auth.split(' ', 1)[1] == API_SECRET

@app.route('/health', methods=['GET'])
def health():
    with WS_RAW_LOCK:
        bal = WS_RAW_DATA['balance']
        demo = WS_RAW_DATA['demo_balance']
        real = WS_RAW_DATA['real_balance']
    
    # Si no tenemos balance, pedir profile al broker
    if bal is None and is_ws_alive():
        try:
            ws_sock = getattr(expert.websocket_client, 'sock', None)
            if ws_sock and ws_sock.connected:
                ws_sock.send(json.dumps({"action": "profile"}))
        except: pass
    
    return jsonify({
        "status": "ok",
        "ws_alive": is_ws_alive(),
        "mode": "DEMO" if IS_DEMO else "REAL",
        "balance": bal,
        "demo_balance": demo,
        "real_balance": real,
        "trade_amount": AMOUNT,
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
    amount = data.get('amount')  # Override opcional desde Node.js

    if direction not in ('BUY', 'SELL'):
        return jsonify({"status": "error", "reason": "INVALID_ASSET", "details": f"Dirección inválida: {direction}"}), 400

    log(f"[TRADE] ━━━ {asset_str} {direction} ${amount or AMOUNT} {'DEMO' if IS_DEMO else 'REAL'} ━━━")
    result = execute_trade(asset_str, direction, tf, amount)
    
    code = 200 if result["status"] == "success" else 500
    return jsonify(result), code

@app.route('/broker-status', methods=['GET'])
def broker_status():
    alive = is_ws_alive()
    
    # Si conectado pero sin balance, solicitar profile
    with WS_RAW_LOCK:
        bal = WS_RAW_DATA['balance']
    if bal is None and alive:
        try:
            ws_sock = getattr(expert.websocket_client, 'sock', None)
            if ws_sock and ws_sock.connected:
                ws_sock.send(json.dumps({"action": "profile"}))
                time.sleep(2)
        except: pass

    with WS_RAW_LOCK:
        bal = WS_RAW_DATA['balance']
        demo = WS_RAW_DATA['demo_balance']
        real = WS_RAW_DATA['real_balance']
    return jsonify({
        "connected": alive,
        "mode": "DEMO" if IS_DEMO else "REAL",
        "balance": bal,
        "demo_balance": demo,
        "real_balance": real,
        "trade_amount": AMOUNT,
        "message": "Bridge operativo" if alive else "Sin conexión WS"
    }), 200

@app.route('/debug-assets', methods=['GET'])
def debug_assets():
    with WS_RAW_LOCK:
        assets_copy = dict(WS_RAW_DATA['assets'])
    
    crypto_matches = {}
    for name, terms in SYMBOL_SEARCH.items():
        for aid, info in assets_copy.items():
            for term in terms:
                if term.upper() in info.get('symbol', '').upper() or term.lower() in info.get('name', '').lower():
                    crypto_matches[name] = {'id': aid, 'name': info.get('name'), 'active': info.get('isActive')}
                    break
    
    return jsonify({
        "total_assets": len(assets_copy),
        "crypto_matches": crypto_matches,
        "assets_raw": json.dumps(list(assets_copy.values())[:50])
    }), 200

# ═══════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════

if EoApi and TOKEN:
    connect_global()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)

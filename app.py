"""
app.py — Puente ExpertOption para Render
Con debug de dependencias, reconexión, keep-alive y autenticación.
"""

import os
import sys
import time
import threading
import subprocess

# ═══════════════════════════════════════════════════════════════════
# SAFETY: Instalar dependencias faltantes ANTES de importar nada
# ExpertOptionAPI necesita pause, simplejson, websocket-client
# pero Render puede cachear un entorno incompleto entre restarts
# ═══════════════════════════════════════════════════════════════════

def ensure_deps():
    """Verifica e instala dependencias críticas si faltan."""
    deps = ['pause', 'simplejson', 'websocket']
    missing = []
    for dep in deps:
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)
    if missing:
        print(f"[SAFETY] Instalando dependencias faltantes: {missing}")
        pkg_map = {'websocket': 'websocket-client==1.7.0', 'pause': 'pause', 'simplejson': 'simplejson'}
        for m in missing:
            pkg = pkg_map.get(m, m)
            subprocess.run([sys.executable, '-m', 'pip', 'install', pkg, '-q'], check=False)
        print("[SAFETY] Dependencias instaladas")

ensure_deps()

from flask import Flask, request, jsonify

app = Flask(__name__)

# ═══════════════════════════════════════════════════════════════════
# DEBUG: Diagnosticar dependencias al arrancar
# ═══════════════════════════════════════════════════════════════════

print("=" * 60)
print("[DEBUG] Python version:", sys.version)
print("[DEBUG] pip list (relevante):")
try:
    result = subprocess.run([sys.executable, '-m', 'pip', 'list'], capture_output=True, text=True)
    for line in result.stdout.split('\n'):
        low = line.lower()
        if any(k in low for k in ['expert', 'websocket', 'flask', 'gunicorn']):
            print(f"  {line}")
except Exception as e:
    print(f"  pip list falló: {e}")

# Debug: verificar websocket
print("[DEBUG] Verificando websocket...")
try:
    import websocket
    print(f"  websocket.__file__: {websocket.__file__}")
    print(f"  WebSocketApp existe: {hasattr(websocket, 'WebSocketApp')}")
    if not hasattr(websocket, 'WebSocketApp'):
        print("  ⚠️ FALTA WebSocketApp — probablemente paquete 'websocket' sin '-client'")
except ImportError as e:
    print(f"  ❌ websocket no instalado: {e}")

# Debug: buscar el módulo expert
print("[DEBUG] Buscando módulo ExpertOption...")
EoApi = None

# Intento 1: import estándar documentado
try:
    from expert import EoApi as _EoApi
    EoApi = _EoApi
    print("  ✅ from expert import EoApi → OK")
except ImportError as e:
    print(f"  ❌ from expert import EoApi → {e}")

# Intento 2: ruta alternativa PyPI
if EoApi is None:
    try:
        from ExpertOptionAPI.expert import EoApi as _EoApi
        EoApi = _EoApi
        print("  ✅ from ExpertOptionAPI.expert import EoApi → OK")
    except ImportError as e:
        print(f"  ❌ from ExpertOptionAPI.expert import EoApi → {e}")

# Intento 3: buscar en site-packages directamente
if EoApi is None:
    try:
        import importlib
        import site
        sp = site.getsitepackages()
        print(f"  site-packages: {sp}")
        for p in sp:
            expert_path = os.path.join(p, 'expert')
            if os.path.isdir(expert_path):
                print(f"  Encontrado: {expert_path}")
                contents = os.listdir(expert_path)
                print(f"  Contenido: {contents}")
        # Intento forzado
        from expert.api import EoApi as _EoApi
        EoApi = _EoApi
        print("  ✅ from expert.api import EoApi → OK")
    except Exception as e:
        print(f"  ❌ Búsqueda manual falló: {e}")

# Intento 4: último recurso - importlib
if EoApi is None:
    try:
        import importlib
        mod = importlib.import_module('expert')
        print(f"  Módulo expert: {dir(mod)}")
        if hasattr(mod, 'EoApi'):
            EoApi = mod.EoApi
            print("  ✅ expert.EoApi via importlib → OK")
    except Exception as e:
        print(f"  ❌ importlib falló: {e}")

if EoApi is None:
    print("  🔴 NO SE PUDO IMPORTAR EoApi — el bridge arrancará sin conexión")
else:
    print(f"  🟢 EoApi cargado correctamente: {EoApi}")

print("=" * 60)

# ═══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════

TOKEN = os.environ.get("EO_TOKEN", "")
SERVER = os.environ.get("EO_SERVER", "wss://fr24g1eu.expertoption.com/")
API_SECRET = os.environ.get("BRIDGE_SECRET", "cambiar_esto")
AMOUNT = int(os.environ.get("TRADE_AMOUNT", "10"))
EXP_TIME = int(os.environ.get("EXP_TIME", "60"))

ASSET_MAP = {
    "BTC/USD":  160,
    "ETH/USD":  162,
    "XRP/USD":  173,
    "ADA/USD":  235,
    "SOL/USD":  464,
    "DOGE/USD": 463,
    "BNB/USD":  462
}

# ═══════════════════════════════════════════════════════════════════
# CAPTURA DE MENSAJES WS (para descubrir asset IDs)
# ═══════════════════════════════════════════════════════════════════

WS_CAPTURED = {}  # Almacena mensajes WS interceptados por action

def ws_message_handler(message):
    """Intercepta todos los mensajes WS para capturar assets, profile, etc."""
    try:
        import json
        if isinstance(message, str):
            data = json.loads(message)
        elif isinstance(message, dict):
            data = message
        else:
            return

        action = data.get('action', 'unknown')

        # Capturar acciones que nos interesan
        if action in ('assets', 'profile', 'environment', 'getCurrency', 'userGroup'):
            WS_CAPTURED[action] = data
            print(f"[WS-CAPTURE] ✅ Capturado '{action}': {str(data)[:500]}")

        # Buscar assets dentro de multipleAction
        if action == 'multipleAction':
            actions = data.get('message', data.get('data', []))
            if isinstance(actions, list):
                for sub in actions:
                    if isinstance(sub, dict):
                        sub_action = sub.get('action', sub.get('a', ''))
                        if sub_action in ('assets', 'profile'):
                            WS_CAPTURED[sub_action] = sub
                            print(f"[WS-CAPTURE] ✅ Capturado '{sub_action}' (dentro de multipleAction): {str(sub)[:500]}")
    except Exception as e:
        pass  # No romper el WS por un error de captura

# ═══════════════════════════════════════════════════════════════════
# CONEXIÓN A EXPERTOPTION
# ═══════════════════════════════════════════════════════════════════

expert = None

def connect_expert():
    """Conecta a ExpertOption en modo DEMO."""
    global expert
    if EoApi is None:
        print("[BRIDGE] ❌ EoApi no disponible — no se puede conectar")
        return False
    if not TOKEN:
        print("[BRIDGE] ⚠️ EO_TOKEN no configurado")
        return False
    try:
        print("[BRIDGE] Conectando a ExpertOption...")
        expert = EoApi(token=TOKEN, server_region=SERVER)

        # Registrar callback custom para capturar mensajes WS
        if hasattr(expert, 'message_callback'):
            original_cb = expert.message_callback
            def combined_cb(msg):
                ws_message_handler(msg)
                if original_cb:
                    return original_cb(msg)
            expert.message_callback = combined_cb
            print("[BRIDGE] Callback WS registrado")

        expert.connect()
        time.sleep(3)  # Más tiempo para recibir assets
        expert.SetDemo()

        # Imprimir lo que capturamos
        if WS_CAPTURED:
            print(f"[BRIDGE] Mensajes WS capturados: {list(WS_CAPTURED.keys())}")
            for k, v in WS_CAPTURED.items():
                print(f"[BRIDGE] {k}: {str(v)[:300]}")
        else:
            print("[BRIDGE] ⚠️ No se capturaron mensajes WS (callback puede no funcionar con esta librería)")

        print("[BRIDGE] ✅ Conectado a ExpertOption DEMO")
        return True
    except Exception as e:
        print(f"[BRIDGE] ❌ Error de conexión: {e}")
        expert = None
        return False

def ensure_connection():
    """Reconecta si es necesario."""
    global expert
    if expert is not None:
        return True
    return connect_expert()

# ═══════════════════════════════════════════════════════════════════
# KEEP-ALIVE: Evita que Render duerma (ping cada 10 min)
# ═══════════════════════════════════════════════════════════════════

def keep_alive():
    import urllib.request
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    while True:
        time.sleep(600)
        if render_url:
            try:
                urllib.request.urlopen(f"{render_url}/health", timeout=10)
                print("[KEEP-ALIVE] Ping OK")
            except Exception:
                pass
        # Reconectar si se cayó
        ensure_connection()

keepalive_thread = threading.Thread(target=keep_alive, daemon=True)
keepalive_thread.start()

# ═══════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════

def check_auth():
    auth = request.headers.get('Authorization', '')
    if not auth.startswith('Bearer ') or auth.split(' ', 1)[1] != API_SECRET:
        return False
    return True

# ═══════════════════════════════════════════════════════════════════
# RUTAS
# ═══════════════════════════════════════════════════════════════════

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "connected": expert is not None,
        "eoapi_loaded": EoApi is not None,
        "mode": "DEMO",
        "python": sys.version.split()[0]
    }), 200

@app.route('/debug-assets', methods=['GET'])
def debug_assets():
    """Descubre los IDs reales de activos en ExpertOption."""
    if not check_auth():
        return jsonify({"status": "error", "message": "No autorizado"}), 401

    if expert is None:
        return jsonify({"status": "error", "message": "No conectado"}), 503

    debug = {}

    # 1. Buscar en msg_by_action (respuestas cacheadas del WS)
    try:
        if hasattr(expert, 'msg_by_action') and expert.msg_by_action:
            mba = expert.msg_by_action
            debug['msg_by_action_keys'] = list(mba.keys()) if isinstance(mba, dict) else str(type(mba))
            if isinstance(mba, dict) and 'assets' in mba:
                assets_data = mba['assets']
                debug['assets_raw'] = str(assets_data)[:2000]
            if isinstance(mba, dict) and 'profile' in mba:
                debug['profile_raw'] = str(mba['profile'])[:1000]
        else:
            debug['msg_by_action'] = 'NO_ATTR o vacío'
    except Exception as e:
        debug['msg_by_action_error'] = str(e)

    # 2. Buscar en msg_by_ns
    try:
        if hasattr(expert, 'msg_by_ns') and expert.msg_by_ns:
            mbn = expert.msg_by_ns
            debug['msg_by_ns_keys'] = list(mbn.keys()) if isinstance(mbn, dict) else str(type(mbn))
            for k, v in (mbn.items() if isinstance(mbn, dict) else []):
                debug[f'ns_{k}'] = str(v)[:500]
        else:
            debug['msg_by_ns'] = 'NO_ATTR o vacío'
    except Exception as e:
        debug['msg_by_ns_error'] = str(e)

    # 3. Buscar en results
    try:
        if hasattr(expert, 'results') and expert.results:
            debug['results'] = str(expert.results)[:2000]
        else:
            debug['results'] = 'NO_ATTR o vacío'
    except Exception as e:
        debug['results_error'] = str(e)

    # 4. Intentar GetCandles con distintos IDs para detectar cuáles son cripto
    # Los IDs típicos de ExpertOption van del 1 al 300+
    crypto_names = ['bitcoin', 'btc', 'ethereum', 'eth', 'cardano', 'ada',
                    'solana', 'sol', 'ripple', 'xrp', 'doge', 'bnb', 'crypto']

    found_assets = {}
    try:
        if isinstance(debug.get('msg_by_action_keys'), list):
            for key in debug['msg_by_action_keys']:
                val = expert.msg_by_action[key]
                val_str = str(val).lower()[:500]
                for cn in crypto_names:
                    if cn in val_str:
                        found_assets[key] = str(val)[:300]
                        break
    except Exception:
        pass

    debug['crypto_matches'] = found_assets if found_assets else 'Ninguno encontrado en msg_by_action'

    # 5. Datos capturados por nuestro callback WS
    if WS_CAPTURED:
        debug['ws_captured_keys'] = list(WS_CAPTURED.keys())
        for k, v in WS_CAPTURED.items():
            debug[f'ws_{k}'] = str(v)[:2000]
    else:
        debug['ws_captured'] = 'Vacío — callback puede no funcionar con esta librería'

    # 6. Buscar en nested_dict si existe
    try:
        if hasattr(expert, 'nested_dict') and expert.nested_dict:
            nd = expert.nested_dict
            debug['nested_dict_type'] = str(type(nd))
            if isinstance(nd, dict):
                debug['nested_dict_keys'] = list(nd.keys())[:50]
                # Buscar crypto keywords en las keys
                for k in list(nd.keys())[:100]:
                    k_lower = str(k).lower()
                    for cn in crypto_names:
                        if cn in k_lower:
                            debug[f'nd_match_{k}'] = str(nd[k])[:300]
            debug['nested_dict_sample'] = str(nd)[:2000]
    except Exception as e:
        debug['nested_dict_error'] = str(e)

    # Log todo
    for k, v in debug.items():
        print(f"[DEBUG-ASSETS] {k}: {v}")

    return jsonify(debug), 200

@app.route('/broker-status', methods=['GET'])
def broker_status():
    """Health check del broker — verifica conexión y retorna saldo DEMO."""
    if not check_auth():
        return jsonify({"status": "error", "message": "No autorizado"}), 401

    if expert is None:
        return jsonify({
            "status": "disconnected",
            "connected": False,
            "balance": None,
            "mode": "DEMO"
        }), 200

    balance = None
    debug_info = {}

    # Intentar TODOS los métodos posibles para obtener saldo
    methods_to_try = [
        ('Profile', lambda: expert.Profile()),
        ('GetBalance', lambda: expert.GetBalance()),
        ('GetProfile', lambda: expert.GetProfile()),
        ('balance attr', lambda: expert.balance if hasattr(expert, 'balance') else 'NO_ATTR'),
        ('demo_balance attr', lambda: expert.demo_balance if hasattr(expert, 'demo_balance') else 'NO_ATTR'),
        ('account attr', lambda: expert.account if hasattr(expert, 'account') else 'NO_ATTR'),
    ]

    for name, fn in methods_to_try:
        try:
            result = fn()
            debug_info[name] = str(result)[:500] if result is not None else 'None'
            print(f"[BROKER-STATUS] {name} → {debug_info[name]}")

            # Intentar extraer balance del resultado
            if balance is None and result is not None:
                if isinstance(result, (int, float)):
                    balance = result
                elif isinstance(result, dict):
                    for key in ['demo_balance', 'balance', 'amount', 'demo', 'd']:
                        if key in result and result[key] is not None:
                            balance = result[key]
                            break
                elif isinstance(result, str) and result not in ('None', 'NO_ATTR', ''):
                    try:
                        balance = float(result)
                    except ValueError:
                        pass
        except Exception as e:
            debug_info[name] = f"ERROR: {e}"
            print(f"[BROKER-STATUS] {name} → ERROR: {e}")

    # Debug: listar todos los atributos/métodos del objeto expert
    try:
        attrs = [a for a in dir(expert) if not a.startswith('_')]
        debug_info['available_methods'] = ', '.join(attrs)
        print(f"[BROKER-STATUS] Métodos disponibles: {', '.join(attrs)}")
    except Exception:
        pass

    return jsonify({
        "status": "connected",
        "connected": True,
        "balance": balance,
        "mode": "DEMO",
        "debug": debug_info
    }), 200

@app.route('/trade', methods=['POST'])
def trade():
    global expert
    if not check_auth():
        return jsonify({"status": "error", "message": "No autorizado"}), 401

    data = request.json
    if not data:
        return jsonify({"status": "error", "message": "Body vacío"}), 400

    asset_str = data.get('asset', '')
    direction = data.get('direction', '').upper()

    print(f"[TRADE] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"[TRADE] 📥 Petición POST /trade recibida para {asset_str} {direction}")

    if direction not in ('BUY', 'SELL'):
        print(f"[TRADE] ❌ Dirección inválida: {direction}")
        return jsonify({"status": "error", "message": f"Dirección inválida: {direction}"}), 400

    if EoApi is None:
        print(f"[TRADE] ❌ EoApi no cargado")
        return jsonify({"status": "error", "message": "EoApi no cargado"}), 503

    eo_type = "call" if direction == "BUY" else "put"
    asset_id = ASSET_MAP.get(asset_str)
    if asset_id is None:
        print(f"[TRADE] ⚠️ Activo no mapeado: {asset_str} — operación cancelada")
        return jsonify({"status": "error", "message": f"Activo no disponible en ExpertOption: {asset_str}"}), 400

    if not ensure_connection():
        print(f"[TRADE] ❌ Sin conexión a ExpertOption")
        return jsonify({"status": "error", "message": "Sin conexión a ExpertOption"}), 503

    try:
        # Forzar cuenta DEMO antes de cada operación
        print(f"[TRADE] 🔄 Seleccionando cuenta DEMO...")
        expert.SetDemo()
        time.sleep(0.5)

        # Obtener saldo para calcular 10%
        trade_amount = AMOUNT  # Fallback al monto fijo
        try:
            # Intentar obtener balance de múltiples fuentes
            balance = None

            # 1. Atributo profile (dict cacheado por la librería)
            if hasattr(expert, 'profile') and expert.profile:
                prof = expert.profile
                print(f"[TRADE] 📊 expert.profile: {str(prof)[:300]}")
                if isinstance(prof, dict):
                    balance = prof.get('demo_balance', prof.get('balance', prof.get('d', None)))

            # 2. Atributo msg_by_action (respuestas cacheadas del WS)
            if balance is None and hasattr(expert, 'msg_by_action') and expert.msg_by_action:
                mba = expert.msg_by_action
                if isinstance(mba, dict) and 'profile' in mba:
                    prof_data = mba['profile']
                    print(f"[TRADE] 📊 msg_by_action.profile: {str(prof_data)[:300]}")
                    if isinstance(prof_data, dict):
                        balance = prof_data.get('demo_balance', prof_data.get('balance', None))

            # 3. Método Profile()
            if balance is None:
                prof_result = expert.Profile()
                print(f"[TRADE] 📊 Profile(): {str(prof_result)[:300]}")
                if prof_result and isinstance(prof_result, dict):
                    balance = prof_result.get('demo_balance', prof_result.get('balance', None))

            # Calcular 10% del saldo (entero, mínimo 1)
            if balance is not None:
                balance = float(balance)
                trade_amount = max(1, int(balance * 0.10))
                print(f"[TRADE] 💰 Saldo: ${balance} → 10% = ${trade_amount}")
            else:
                print(f"[TRADE] ⚠️ Saldo no disponible, usando monto fijo: ${trade_amount}")

        except Exception as be:
            print(f"[TRADE] ⚠️ Error obteniendo saldo: {be} — usando monto fijo: ${trade_amount}")

        print(f"[TRADE] 📊 Mapeo: {asset_str} → ID:{asset_id} | Tipo: {eo_type} | Monto: ${trade_amount} | Exp: {EXP_TIME}s")
        print(f"[TRADE] 🎯 Ejecutando {eo_type.upper()} en ExpertOption...")

        strike = time.time()
        result = expert.Buy(
            amount=trade_amount,
            type=eo_type,
            assetid=asset_id,
            exptime=EXP_TIME,
            isdemo=1,
            strike_time=strike
        )
        print(f"[TRADE] ✅ Respuesta cruda del Broker: {result}")
        print(f"[TRADE] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        return jsonify({
            "status": "success",
            "asset": asset_str,
            "direction": direction,
            "type": eo_type,
            "asset_id": asset_id,
            "amount": trade_amount,
            "broker_response": str(result)
        }), 200

    except Exception as e:
        print(f"[TRADE] ❌ Error de ejecución: {e}")
        print(f"[TRADE] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        expert = None
        return jsonify({"status": "error", "message": str(e)}), 500

# ═══════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════

if TOKEN and EoApi:
    connect_expert()
elif not TOKEN:
    print("[BRIDGE] ⚠️ Arrancando sin EO_TOKEN — configurar en Environment Variables")
elif not EoApi:
    print("[BRIDGE] ⚠️ Arrancando sin EoApi — revisar logs de debug arriba")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    print(f"[BRIDGE] Escuchando en :{port} | Modo: DEMO | Monto: ${AMOUNT}")
    app.run(host='0.0.0.0', port=port, debug=False)

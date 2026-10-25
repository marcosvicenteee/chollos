"""Mantiene despierto un despliegue (Render u otro) haciendo ping a su propia URL.

Render suspende los servicios gratuitos tras ~15 min sin tráfico entrante. Un hilo
propio hace GET a SELF_URL cada 10 min, generando tráfico entrante y evitando la
suspensión. Sólo actúa si SELF_URL está definido.
"""
import os
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

_INTERVAL = 600  # 10 minutos, por debajo del umbral de suspensión de Render
_THREAD = None
_GUARD = threading.Lock()


def self_url():
    return os.environ.get('SELF_URL', '').strip().rstrip('/')


def enabled():
    url = self_url()
    return bool(url) and url.startswith('https://')


def start():
    global _THREAD
    if not enabled():
        return False
    with _GUARD:
        if _THREAD and _THREAD.is_alive():
            return True
        _THREAD = threading.Thread(target=_loop, daemon=True, name='cero-keepalive')
        _THREAD.start()
    return True


def _ping():
    url = self_url()
    if not url:
        return
    target = url + '/health'
    try:
        with urlopen(Request(target, headers={'User-Agent': 'CERO-keepalive'}), timeout=20) as r:
            return r.status
    except URLError:
        return None
    except Exception:
        return None


def _loop():
    # primer ping tras arrancar, para registrar actividad inmediata
    time.sleep(30)
    while True:
        _ping()
        time.sleep(_INTERVAL)

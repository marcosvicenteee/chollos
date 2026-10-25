"""Avisos de CERO a Discord: chollos por webhook y pulso periódico por otro.

- DISCORD_WEBHOOK_URL recibe cada chollo confirmado (con mención opcional).
- DISCORD_STATUS_WEBHOOK_URL recibe cada X minutos un mensaje de que el radar sigue trabajando.
La cola vive en la base de datos (notifications, canal «discord»), así que un reinicio
no pierde avisos. Un hilo propio envía en segundo plano para no frenar el rastreo y
respeta el límite de ritmo del webhook.
"""
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from cero.db import connect, unpack
from cero.inbox import discard, eligible, enqueue as _enqueue, fresh, mark_sent, pending

CHANNEL = 'discord'
MAX_BYTES = 8_000_000
MIN_INTERVAL = 2.5
COLOR_FREE = 0x2ECC71
COLOR_DEAL = 0xE67E22
COLOR_STATUS = 0x3498DB

_WAKE = threading.Event()
_GUARD = threading.Lock()
_THREAD = None
_HEARTBEAT_THREAD = None
_FATAL = {'detail': ''}
_STATUS_FATAL = {'detail': ''}
_LAST = {'ok': None, 'at': None, 'detail': ''}

IMAGE_PATH = re.compile(r'/static/cache/[a-f0-9]{64}\.webp')
CACHE = Path(__file__).resolve().parent / 'static' / 'cache'


def _env(name, default=''):
    return os.environ.get(name, default).strip()


def deal_webhook():
    return _env('DISCORD_WEBHOOK_URL')


def status_webhook():
    return _env('DISCORD_STATUS_WEBHOOK_URL')


def mention_user():
    return _env('DISCORD_MENTION_USER_ID')


def heartbeat_minutes():
    try:
        return max(5, int(_env('DISCORD_HEARTBEAT_MINUTES', '30') or '30'))
    except ValueError:
        return 30


def armed():
    url = deal_webhook()
    if not url.startswith('https://') or '/api/webhooks/' not in url:
        return False
    return _env('DISCORD_ENABLED', 'true').lower() != 'false'


def status_armed():
    url = status_webhook()
    if not url.startswith('https://') or '/api/webhooks/' not in url:
        return False
    return _env('DISCORD_STATUS_ENABLED', 'true').lower() != 'false'


def status():
    def info(url, fatal):
        host = urlparse(url).netloc if url else ''
        return {'armed': bool(url.startswith('https://') and '/api/webhooks/' in url), 'host': host,
                'fatal': fatal['detail']}
    c = connect()
    try:
        waiting = c.execute('SELECT COUNT(*) FROM notifications WHERE channel=? AND sent_at IS NULL', (CHANNEL,)).fetchone()[0]
    finally:
        c.close()
    return {'deal': {**info(deal_webhook(), _FATAL), 'enabled': _env('DISCORD_ENABLED', 'true'),
                    'pending': waiting, 'worker': bool(_THREAD and _THREAD.is_alive()),
                    'last': dict(_LAST), 'mention_user': mention_user()},
            'status': {**info(status_webhook(), _STATUS_FATAL), 'enabled': _env('DISCORD_STATUS_ENABLED', 'true'),
                      'heartbeat_minutes': heartbeat_minutes(),
                      'worker': bool(_HEARTBEAT_THREAD and _HEARTBEAT_THREAD.is_alive())}}


def enqueue(conn, deal_id, deal):
    saved = _enqueue(conn, deal_id, deal, CHANNEL)
    if saved:
        notify_new()
    return saved


def notify_new():
    if armed() and not _FATAL['detail']:
        _WAKE.set()


def build(deal):
    from cero.policy import affiliate_url
    currency = deal.get('currency') or 'EUR'
    price = deal.get('price')
    free = price == 0
    title = (deal.get('title') or 'Sin título').strip()[:230]
    description = ' · '.join(part for part in (deal.get('store'), deal.get('category'), deal.get('variant')) if part)[:300]
    fields = []
    if price is None:
        fields.append({'name': 'Precio', 'value': 'por confirmar', 'inline': True})
    else:
        fields.append({'name': 'Precio', 'value': ('0,00 ' + currency) if free else f'{price:.2f} {currency}', 'inline': True})
    if deal.get('discount_pct'):
        fields.append({'name': 'Descuento', 'value': f"-{deal['discount_pct']:.0f}%", 'inline': True})
    if deal.get('shipping') is not None:
        fields.append({'name': 'Envío', 'value': f"{deal['shipping']:.2f} {currency}", 'inline': True})
    else:
        fields.append({'name': 'Envío', 'value': 'por confirmar', 'inline': True})
    if deal.get('score') is not None:
        fields.append({'name': 'Interés', 'value': f"{deal['score']:.0f}/100", 'inline': True})
    if deal.get('availability') in ('in_stock', 'preorder'):
        fields.append({'name': 'Stock', 'value': 'disponible', 'inline': True})
    embed = {'title': title, 'url': affiliate_url(deal.get('url') or ''),
             'description': description or 'Hallazgo del radar CERO',
             'color': COLOR_FREE if free else COLOR_DEAL, 'fields': fields,
             'footer': {'text': 'CERO · radar de chollos · verifica la cesta antes de comprar'}}
    if deal.get('checked_at'):
        embed['timestamp'] = deal['checked_at']
    image = deal.get('image') or ''
    if not IMAGE_PATH.fullmatch(image):
        return embed, None, ''
    path = CACHE / Path(image).name
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        return embed, None, ''
    embed['image'] = {'url': 'attachment://' + path.name}
    return embed, path.read_bytes(), path.name


def _post(embed, image=None, filename='', content='', allowed_mentions=None, url=None, fatal_box=None):
    """Envía un embed (y opcionalmente una imagen) a un webhook. Devuelve (ok, espera)."""
    from curl_cffi import CurlMime, requests
    target = url or deal_webhook()
    if fatal_box is None:
        fatal_box = _FATAL
    body = {'embeds': [embed], 'allowed_mentions': allowed_mentions if allowed_mentions is not None else {'parse': []}}
    if content:
        body['content'] = content
    payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
    mime = None
    if image:
        mime = CurlMime()
        mime.addpart(name='payload_json', data=payload, content_type='application/json')
        mime.addpart(name='files[0]', data=image, filename=filename, content_type='image/webp')
        kwargs = {'multipart': mime}
    else:
        kwargs = {'data': payload, 'headers': {'Content-Type': 'application/json'}}
    try:
        response = requests.post(target, timeout=25, **kwargs)
    except Exception as exc:
        _LAST.update(ok=False, at=time.time(), detail=type(exc).__name__)
        return False, 0
    finally:
        if mime is not None:
            mime.close()
    if response.status_code in (401, 403, 404):
        fatal_box['detail'] = f'Webhook rechazado ({response.status_code}). Revisa la URL.'
        _LAST.update(ok=False, at=time.time(), detail=fatal_box['detail'])
        return False, 0
    if response.status_code == 429:
        try:
            wait = float(response.json().get('retry_after', 5))
        except Exception:
            wait = 5.0
        _LAST.update(ok=False, at=time.time(), detail='límite de ritmo de Discord')
        return False, max(wait, 1.0)
    ok = 200 <= response.status_code < 300
    _LAST.update(ok=ok, at=time.time(), detail='' if ok else f'HTTP {response.status_code}')
    return ok, 0


def send_deal(deal):
    if not armed() or _FATAL['detail'] or not eligible(deal) or not fresh(deal):
        return False
    embed, image, filename = build(deal)
    user = mention_user() if deal.get('price') == 0 else ''
    content = f'<@{user}>' if user else ''
    allowed = {'users': [user]} if user else {'parse': []}
    ok, _ = _post(embed, image, filename, content=content, allowed_mentions=allowed)
    return ok


def flush(limit=5):
    if not armed() or _FATAL['detail']:
        return 0
    c = connect()
    sent = 0
    try:
        for row in pending(c, CHANNEL, limit):
            deal = unpack(row)
            if not eligible(deal) or not fresh(deal):
                discard(c, row['notification_id'])
                continue
            embed, image, filename = build(deal)
            user = mention_user() if deal.get('price') == 0 else ''
            content = f'<@{user}>' if user else ''
            allowed = {'users': [user]} if user else {'parse': []}
            ok, wait = _post(embed, image, filename, content=content, allowed_mentions=allowed)
            if ok:
                mark_sent(c, row['notification_id'])
                sent += 1
            elif wait:
                _WAKE.set()
                time.sleep(min(wait, 30))
            if _FATAL['detail']:
                break
            time.sleep(MIN_INTERVAL)
    finally:
        c.close()
    return sent


def send_test():
    embed = {'title': 'CERO conectado a Discord', 'description': 'Los avisos de chollos llegarán a este canal.',
             'color': COLOR_DEAL, 'fields': [{'name': 'Canal', 'value': 'avisos', 'inline': True}],
             'footer': {'text': 'CERO · radar de chollos'}}
    user = mention_user()
    content = f'<@{user}> CERO conectado y listo para avisar.' if user else 'CERO conectado y listo para avisar.'
    allowed = {'users': [user]} if user else {'parse': []}
    ok, _ = _post(embed, None, '', content=content, allowed_mentions=allowed)
    return ok, _LAST['detail']


def send_heartbeat():
    """Mensaje periódico al webhook de estado: el radar sigue trabajando."""
    if not status_armed() or _STATUS_FATAL['detail']:
        return False
    c = connect()
    try:
        total = c.execute("SELECT COUNT(*) FROM deals WHERE status='verified'").fetchone()[0]
        last = c.execute('SELECT profile, checked, found, finished_at FROM runs ORDER BY id DESC LIMIT 1').fetchone()
    finally:
        c.close()
    msg = '🔍 CERO sigue buscando ofertas.'
    if last:
        msg += (f" Última pasada ({last['profile']}): {last['checked']} fichas revisadas,"
                f" {last['found']} ofertas. Total verificadas: {total}.")
    msg += f" Próximo pulso en {heartbeat_minutes()} min."
    embed = {'title': 'CERO sigue rastreando', 'description': msg, 'color': COLOR_STATUS,
             'footer': {'text': 'CERO · radar de chollos'}, 'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    ok, _ = _post(embed, None, '', url=status_webhook(), fatal_box=_STATUS_FATAL)
    return ok


def start_worker():
    global _THREAD, _HEARTBEAT_THREAD
    started = False
    if armed():
        with _GUARD:
            if not (_THREAD and _THREAD.is_alive()):
                _THREAD = threading.Thread(target=_loop, daemon=True, name='cero-discord')
                _THREAD.start()
                _WAKE.set()
            started = True
    if status_armed():
        with _GUARD:
            if not (_HEARTBEAT_THREAD and _HEARTBEAT_THREAD.is_alive()):
                _HEARTBEAT_THREAD = threading.Thread(target=_heartbeat_loop, daemon=True, name='cero-discord-heartbeat')
                _HEARTBEAT_THREAD.start()
        # primer pulso tras arrancar, sin bloquear
        def _first_pulse():
            time.sleep(5)
            try:
                send_heartbeat()
            except Exception:
                pass
        threading.Thread(target=_first_pulse, daemon=True).start()
    return started


def stop_worker():
    _WAKE.set()


def _loop():
    while True:
        _WAKE.wait(timeout=20)
        _WAKE.clear()
        if _FATAL['detail']:
            return
        try:
            flush()
        except Exception:
            time.sleep(5)


def _heartbeat_loop():
    while True:
        time.sleep(heartbeat_minutes() * 60)
        if _STATUS_FATAL['detail']:
            return
        try:
            send_heartbeat()
        except Exception:
            time.sleep(10)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Avisos de CERO a Discord.')
    parser.add_argument('--test', action='store_true', help='Envía un mensaje de prueba al webhook de chollos')
    parser.add_argument('--status-ping', action='store_true', help='Envía un pulso de prueba al webhook de estado')
    parser.add_argument('--send', action='store_true', help='Envía ahora los avisos pendientes')
    parser.add_argument('--heartbeat', action='store_true', help='Envía ahora un pulso de estado')
    args = parser.parse_args()
    if args.status_ping or args.heartbeat:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    if args.test or args.status_ping or args.send or args.heartbeat:
        if (args.test or args.send) and not armed():
            raise SystemExit('Discord (chollos) no está activo: define DISCORD_WEBHOOK_URL en .env.')
        if (args.status_ping or args.heartbeat) and not status_armed():
            raise SystemExit('Discord (estado) no está activo: define DISCORD_STATUS_WEBHOOK_URL en .env.')
    if args.test:
        ok, detail = send_test()
        raise SystemExit('Mensaje de prueba enviado.' if ok else 'Discord rechazó el mensaje: ' + (detail or 'error desconocido'))
    if args.heartbeat or args.status_ping:
        ok = send_heartbeat()
        raise SystemExit('Pulso enviado.' if ok else 'Discord rechazó el pulso: ' + (_STATUS_FATAL['detail'] or _LAST['detail'] or 'error desconocido'))
    if args.send:
        print(f'Entregados: {flush(limit=20)}')
        return
    print(json.dumps(status(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

"""Bandeja de avisos común a todos los canales: una fila por oferta y destino.

El filtro es el mismo que ya usaba Telegram: sólo se avisa de un hallazgo verificado,
con foto propia, cero aceptado por revisión o rebaja grande con score suficiente.
"""
import json
from cero.db import now

MIN_DISCOUNT = 80
MIN_SCORE = 85
MAX_AGE_SECONDS = 7200


def eligible(deal):
    if deal.get('status') != 'verified' or deal.get('is_sample') or deal.get('image_status') != 'verified':
        return False
    if deal.get('price') == 0:
        return deal.get('free_review') == 'accepted'
    if (deal.get('discount_pct') or 0) < MIN_DISCOUNT:
        return False
    return (deal.get('score') or 0) >= MIN_SCORE


def fresh(deal):
    from datetime import datetime, timezone
    checked = deal.get('checked_at')
    if not checked:
        return False
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(checked)).total_seconds()
    except ValueError:
        return False
    return age <= MAX_AGE_SECONDS


def payload(deal):
    return json.dumps({key: deal.get(key) for key in (
        'title', 'url', 'price', 'currency', 'store', 'variant', 'score', 'discount_pct',
        'shipping', 'image', 'checked_at', 'category', 'availability', 'free_review')},
        ensure_ascii=False)


def enqueue(conn, deal_id, deal, channel):
    """Guarda el aviso y lo reabre si vuelve a estar agotado o baja de precio."""
    if not eligible(deal):
        return False
    previous = conn.execute('SELECT status FROM observations WHERE deal_id=? ORDER BY id DESC LIMIT 1 OFFSET 1', (deal_id,)).fetchone()
    restocked = bool(previous and previous['status'] == 'expired')
    conn.execute('''INSERT INTO notifications(deal_id,channel,payload,created_at) VALUES(?,?,?,?)
      ON CONFLICT(deal_id,channel) DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at,
      sent_at=CASE WHEN ? OR json_extract(excluded.payload,'$.price')<COALESCE(json_extract(notifications.payload,'$.price'),1e12)
      THEN NULL ELSE notifications.sent_at END''',
      (deal_id, channel, payload(deal), now(), int(restocked)))
    return True


def pending(conn, channel, limit=20):
    return conn.execute('''SELECT n.id notification_id,d.* FROM notifications n JOIN deals d ON d.id=n.deal_id
      WHERE n.channel=? AND n.sent_at IS NULL ORDER BY n.id LIMIT ?''', (channel, limit)).fetchall()


def mark_sent(conn, notification_id):
    conn.execute('UPDATE notifications SET sent_at=? WHERE id=?', (now(), notification_id))
    conn.commit()


def discard(conn, notification_id):
    """Un aviso demasiado antiguo no se envía: se cierra para no repetirlo en cada pasada."""
    conn.execute('UPDATE notifications SET sent_at=? WHERE id=?', (now(), notification_id))
    conn.commit()


def count_pending(conn, channel):
    return conn.execute('SELECT COUNT(*) FROM notifications WHERE channel=? AND sent_at IS NULL', (channel,)).fetchone()[0]

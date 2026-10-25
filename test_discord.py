"""Comprueba el canal Discord sin red: bandeja, filtros, embed y deduplicación."""
import json
import os
import tempfile
import unittest
from pathlib import Path


class DiscordChannel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['CERO_DATA_DIR'] = self.tmp.name
        os.environ['DISCORD_ENABLED'] = 'true'
        os.environ['DISCORD_WEBHOOK_URL'] = 'https://discord.com/api/webhooks/1/abc'
        import cero.db as db
        db.DB_PATH = Path(self.tmp.name) / 'cero.db'
        db._migrated.clear()
        self.db = db
        self.c = db.connect()

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def deal(self, **extra):
        row = {'id': 'x1', 'title': 'Cala gratuita', 'url': 'https://tienda.example/p/1', 'price': 0.0,
               'currency': 'EUR', 'store': 'Tienda', 'discount_pct': None, 'score': 96,
               'image': '/static/cache/' + 'a' * 64 + '.webp', 'image_status': 'verified',
               'status': 'verified', 'checked_at': self.db.now(), 'free_review': 'accepted',
               'is_sample': 0, 'availability': 'in_stock', 'category': 'Deporte', 'shipping': None}
        row.update(extra)
        return row

    def test_armed_requires_webhook(self):
        from cero import discord
        self.assertTrue(discord.armed())
        os.environ['DISCORD_WEBHOOK_URL'] = ''
        self.assertFalse(discord.armed())
        os.environ['DISCORD_WEBHOOK_URL'] = 'https://example.com/api/webhooks/1/abc'
        os.environ['DISCORD_WEBHOOK_URL'] = 'https://discord.com/api/webhooks/1/abc'

    def test_enqueue_respects_rules(self):
        from cero import discord
        self.assertFalse(discord.enqueue(self.c, 'x1', self.deal(free_review='pending')))
        self.assertFalse(discord.enqueue(self.c, 'x1', self.deal(price=20.0, discount_pct=50, score=99)))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 0)
        self.assertTrue(discord.enqueue(self.c, 'x1', self.deal()))
        self.c.commit()

    def test_dedupe_and_price_drop(self):
        from cero import discord
        deal = self.deal(price=9.99, discount_pct=90, score=90, free_review='pending', url='https://tienda.example/p/2')
        self.assertTrue(discord.enqueue(self.c, 'x2', deal))
        self.c.execute("UPDATE notifications SET sent_at='2026-01-01T00:00:00+00:00' WHERE deal_id='x2'")
        discord.enqueue(self.c, 'x2', deal)
        self.assertIsNotNone(self.c.execute("SELECT sent_at FROM notifications WHERE deal_id='x2'").fetchone()[0])
        discord.enqueue(self.c, 'x2', self.deal(price=4.99, discount_pct=95, score=90, free_review='pending', url='https://tienda.example/p/2'))
        self.assertIsNone(self.c.execute("SELECT sent_at FROM notifications WHERE deal_id='x2'").fetchone()[0])

    def test_build_embed(self):
        from cero import discord
        embed, image, name = discord.build(self.deal())
        self.assertEqual(embed['color'], discord.COLOR_FREE)
        self.assertIn('0,00 EUR', json.dumps(embed, ensure_ascii=False))
        self.assertIn('tienda.example', embed['url'])
        self.assertIsNone(image)
        self.assertEqual(name, '')
        paid = discord.build(self.deal(price=9.99, discount_pct=90, free_review='pending'))
        self.assertEqual(paid[0]['color'], discord.COLOR_DEAL)
        self.assertTrue(any('-90%' == f['value'] for f in paid[0]['fields']))

    def test_stale_is_not_sent(self):
        from cero import discord
        from cero.inbox import fresh
        self.assertFalse(fresh(self.deal(checked_at='2020-01-01T00:00:00+00:00')))


class DiscordWire(unittest.TestCase):
    """Comprueba el formato real del POST contra un servidor local (sin tocar Discord)."""

    def server(self):
        import http.server
        import threading
        captured = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get('content-length', 0))
                captured['body'] = self.rfile.read(size)
                captured['type'] = self.headers.get('content-type', '')
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd.server_address[1], captured

    def test_json_and_multipart(self):
        from cero import discord
        port, captured = self.server()
        original_armed, original_webhook = discord.armed, discord.deal_webhook
        self.addCleanup(setattr, discord, 'armed', original_armed)
        self.addCleanup(setattr, discord, 'deal_webhook', original_webhook)
        discord.armed = lambda: True
        discord.deal_webhook = lambda: f'http://127.0.0.1:{port}/api/webhooks/1/abc'
        embed = {'title': 'prueba de envío', 'description': 'hola'}
        self.assertTrue(discord._post(embed, b'RIFFxxxx', 'b' * 64 + '.webp')[0])
        self.assertIn('multipart/form-data', captured['type'])
        body = captured['body'].decode('utf-8', 'ignore')
        self.assertIn('payload_json', body)
        self.assertIn('files[0]', body)
        self.assertIn('prueba de envío', body)
        self.assertTrue(discord._post(embed, None, '')[0])
        self.assertIn('application/json', captured['type'])
        self.assertIn('prueba de envío', captured['body'].decode('utf-8'))


if __name__ == '__main__':
    unittest.main()

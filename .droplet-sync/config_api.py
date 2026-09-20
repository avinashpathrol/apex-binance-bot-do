#!/usr/bin/env python3
"""Config API."""
import datetime, json, os, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

CONFIG_PATH = '/var/www/apex/bot_config.json'
HERMES_CHAT_URL = 'http://10.122.0.3:8787/dashboard_chat'
HERMES_CHAT_TIMEOUT_SECONDS = 25

# Real command power from dashboard chat, added 2026-09-18 -- deliberately
# scoped to the EXACT same bot_config.json flags the dashboard's own buttons
# already write (see dashboard/command.html's closeFutures/forceTrailFutures/
# closeMu). Hermes already validates type/symbol against the same allowlist
# before returning an action (see hermes_veto_service.py's
# CHAT_ACTION_TYPES/CHAT_ACTION_SYMBOLS) -- this is defense in depth, never
# trusting a remote response blindly even though it's already been checked
# once. Any action that doesn't pass validation here is silently dropped
# (reply still gets shown, nothing gets applied) -- never half-applied.
FUTURES_SYMBOLS = ('NBISUSDT', 'AMDUSDT', 'APPUSDT', 'SOXLUSDT', 'CRCLUSDT', 'ASTSUSDT', 'TSLAUSDT')

def _apply_action(action):
    if not isinstance(action, dict):
        return None
    a_type, symbol = action.get('type'), action.get('symbol')
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    patch, label = None, None
    if a_type == 'close_position' and symbol in FUTURES_SYMBOLS:
        patch = {'futures_close_requested': True, 'futures_close_requested_at': now_iso,
                 'futures_close_symbol': symbol}
        label = f'Close requested — {symbol}'
    elif a_type == 'close_position' and symbol == 'MUUSDT':
        patch = {'futures_mu_close_requested': True, 'futures_mu_close_requested_at': now_iso}
        label = 'Close requested — MU overnight'
    elif a_type == 'force_trail' and symbol in FUTURES_SYMBOLS:
        patch = {'futures_force_trail': True, 'futures_force_trail_at': now_iso,
                 'futures_force_trail_symbol': symbol}
        label = f'Trailing stop locked — {symbol}'
    elif a_type == 'pause_bot':
        patch = {'futures_bot_paused': True}
        label = 'Bot paused'
    elif a_type == 'resume_bot':
        patch = {'futures_bot_paused': False}
        label = 'Bot resumed'
    if not patch:
        return None
    try:
        cfg = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        cfg.update(patch)
        cfg['updated_at'] = now_iso
        with open(CONFIG_PATH, 'w') as f:
            json.dump(cfg, f, indent=2)
        return label
    except Exception:
        return None

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # silence access log

    def do_GET(self):
        if self.path.split('?')[0] != '/bot_config.json':
            self.send_error(404); return
        data = b'{}'
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'rb') as f: data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        if self.path.split('?')[0] != '/bot_config.json':
            self.send_error(404); return
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)
        try:
            json.loads(body)  # validate JSON
            with open(CONFIG_PATH, 'wb') as f: f.write(body)
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'ok')
        except Exception as e:
            self.send_error(400, str(e))

    def do_POST(self):
        if self.path.split('?')[0] != '/hermes_chat':
            self.send_error(404); return
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(body)
        except Exception:
            self.send_error(400, 'invalid JSON'); return
        message = str(payload.get('message', '')).strip()[:2000]
        reply = {'reply': "Hermes didn't respond in time — try again in a moment.", 'ok': False}
        if message:
            try:
                req = urllib.request.Request(
                    HERMES_CHAT_URL,
                    data=json.dumps({'message': message}).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with urllib.request.urlopen(req, timeout=HERMES_CHAT_TIMEOUT_SECONDS) as resp:
                    reply = json.loads(resp.read())
                    reply['ok'] = True
                    applied = _apply_action(reply.get('action'))
                    if applied:
                        reply['action_applied'] = applied
            except Exception as e:
                reply = {'reply': f'Hermes is unreachable right now ({type(e).__name__}). Nothing was changed.', 'ok': False}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(reply).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, PUT, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

if __name__ == '__main__':
    HTTPServer(('127.0.0.1', 8080), Handler).serve_forever()

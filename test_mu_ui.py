#!/usr/bin/env python3
"""Browser test of the MU exit controls on BOTH real dashboards (command.html desktop, index.html mobile).
Headless Chrome loads the actual pages against a tiny mock server that serves a fake MU position and records every
PUT to /bot_config.json. A script injected into a copy of each page types into the inputs, clicks the buttons and
reports what it saw; this file then checks the requests the buttons produced.
Needs Google Chrome (macOS path below).  Run: python3 test_mu_ui.py"""
import copy
import html
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(HERE, 'dashboard')
OPENED = '2026-09-18T19:55:06.751650+00:00'

OPEN = {
    'generated_at': '2099-01-01T00:00:00Z', 'position': 'LONG', 'entry_price': 1015.17, 'sl_price': 979.639, 'sl_default': 979.639,
    'sl_custom': False, 'target_price': None, 'qty': 1.58, 'entry_fee': 0.7969, 'fee_rate': 0.0005, 'opened_at': OPENED,
    'current_price': 1023.13, 'unrealized_pnl': 12.58, 'amount': 40.0, 'leverage': 40, 'levels_status': None,
    'performance': {'total': 0, 'wins': 0, 'losses': 0, 'win_rate': 0, 'win_pnl': 0, 'loss_pnl': 0, 'total_fees': 0, 'net_pnl': 0}, 'trades': [],
}
CUSTOM = dict(OPEN, sl_price=1015.0, sl_custom=True, target_price=1040.0,
              levels_status={'at': '2099-01-01T00:00:00Z', 'ok': True, 'msg': 'stop set to $1,015.00'})
FLAT = dict(OPEN, position=None, qty=None, entry_price=None, sl_price=None, current_price=None, unrealized_pnl=None)
BASE_CONFIG = {'futures_bot_paused': False, 'futures_trade_amount_usdt': 40, 'some_other_setting': 'must survive'}


class Server:
    def __init__(self, root, fixture):
        self.puts, self.config, self.fixture = [], dict(BASE_CONFIG), fixture
        outer = self

        class H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=root, **k)

            def log_message(self, *a):
                pass

            def _json(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

            def do_GET(self):
                p = self.path.split('?')[0]
                if p == '/bot_config.json':
                    return self._json(outer.config)
                if p == '/data_overnight_mu.json':
                    return self._json(outer.fixture)
                if p.endswith('.json'):
                    self.send_error(404); return
                return super().do_GET()

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                outer.puts.append(body); outer.config = body
                self.send_response(200); self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'ok')

        socketserver.TCPServer.allow_reuse_address = True
        self.httpd = socketserver.ThreadingTCPServer(('127.0.0.1', 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown(); self.httpd.server_close()


STUBS = "<script>window.confirm=function(){return true};window.prompt=function(){return null};window.alert=function(){};</script>"


def scenario(root_sel, kind):
    reload = 'await (window.loadAll ? loadAll() : fetchMU());'
    if kind == 'open':
        steps = """
  type(q('stopPrice'), 1021.35);
  R.tightHelp = q('stopHelp').textContent; R.enabledTight = !q('setStop').disabled; R.profitFromPrice = q('stopProfit').value;
  type(q('stopProfit'), 8); R.priceFromProfit = q('stopPrice').value;
  q('setStop').click(); await sleep(500);
  type(q('stopPrice'), 1030); R.aboveHelp = q('stopHelp').textContent; R.aboveEnabled = !q('setStop').disabled;
  type(q('stopPrice'), 970);  R.belowDefaultHelp = q('stopHelp').textContent; R.belowDefaultEnabled = !q('setStop').disabled;
  type(q('stopPrice'), 1000); R.widerHelp = q('stopHelp').textContent;
  type(q('tgtPrice'), 1020);  R.tgtBadEnabled = !q('setTgt').disabled;
  type(q('tgtPrice'), 1040);  R.tgtHelp = q('tgtHelp').textContent; q('setTgt').click(); await sleep(500);
  R.chips = [].map.call(q('chips').children, function(b){return b.textContent});
  R.activeDefault = q('active').textContent;
  q('closeNow').click(); await sleep(500);"""
    elif kind == 'custom':
        steps = """
  R.active = q('active').textContent; R.status = q('status').textContent;
  root.querySelector('[data-act="reset"]').click(); await sleep(500);
  root.querySelector('[data-act="clear"]').click(); await sleep(500);"""
    else:
        steps = "  R.hidden = root.style.display === 'none';"
    return STUBS.replace('<script>', '<script>').replace('</script>', '</script>'), """<script>
(function(){ setTimeout(async function(){
  var R = {}; var sleep = function(ms){return new Promise(function(r){setTimeout(r, ms)})};
  try {
    %s
    await sleep(300);
    var root = document.querySelector('%s');
    var q = function(k){return root.querySelector('[data-r="'+k+'"]')};
    var type = function(el, v){ el.value = String(v); el.dispatchEvent(new Event('input', {bubbles:true})); };
    R.rootDisplay = root.style.display;
    %s
  } catch(e) { R.error = String(e && e.stack || e); }
  document.body.insertAdjacentHTML('beforeend', '<pre id="__' + 't">' + JSON.stringify(R) + '</pre>');
}, 2500); })();
</script>""" % (reload, root_sel, steps)


def run_case(page, root_sel, kind, fixture):
    tmp = tempfile.mkdtemp()
    for f in (page, 'mu_levels.js'):
        shutil.copy(os.path.join(DASH, f), tmp)
    src = open(os.path.join(tmp, page)).read()
    stubs, scen = scenario(root_sel, kind)
    src = src.replace('<head>', '<head>' + stubs, 1).replace('</body>', scen + '</body>', 1)
    open(os.path.join(tmp, 'test_' + page), 'w').write(src)
    srv = Server(tmp, fixture)
    try:
        proc = subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--no-sandbox', '--enable-logging=stderr', '--v=0',
                               '--window-size=%s' % ('1440,1000' if page == 'command.html' else '420,900'), '--virtual-time-budget=20000', '--dump-dom',
                               'http://127.0.0.1:%d/test_%s?view=%s' % (srv.port, page, 'desktop' if page == 'command.html' else 'mobile')],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        m = re.search(r'<pre id="__t">([^<]*)</pre>', proc.stdout)
        if m:
            res = json.loads(html.unescape(m.group(1)))
        else:
            errs = [l for l in proc.stderr.splitlines() if 'CONSOLE' in l or 'Uncaught' in l][:6]
            res = {'error': 'no result element (page did not finish)', 'console': errs}
        return res, srv.puts
    finally:
        srv.stop(); shutil.rmtree(tmp, ignore_errors=True)


FAILS = []


def check(name, cond, detail=''):
    print(('  ok   ' if cond else '  FAIL ') + name + ('' if cond else '   -> ' + str(detail)))
    if not cond:
        FAILS.append(name)


def price_for(net, d=OPEN):
    return (net + d['entry_fee'] + d['entry_price'] * d['qty']) / (d['qty'] * (1 - d['fee_rate']))


def main():
    for label, page, sel in (('DESKTOP command.html', 'command.html', '#mu-exit'), ('MOBILE index.html', 'index.html', '#mu-exit-card')):
        print('\n== %s ==' % label)
        r, puts = run_case(page, sel, 'open', copy.deepcopy(OPEN))
        check('page ran without a script error', 'error' not in r, r.get('error'))
        check('controls are shown for an open position', r.get('rootDisplay') != 'none', r.get('rootDisplay'))
        check('tight stop: warning + profit shown', 'Very tight' in r.get('tightHelp', '') and 'keep about' in r.get('tightHelp', ''), r.get('tightHelp'))
        check('tight-but-valid stop enables the button', r.get('enabledTight') is True)
        exp = price_for(8)
        check('typing a profit fills the price (profit 8 -> $%.2f)' % exp, abs(float(r.get('priceFromProfit') or 0) - exp) < 0.011, r.get('priceFromProfit'))
        check('stop at/above price is blocked with a clear message', r.get('aboveEnabled') is False and 'immediately' in r.get('aboveHelp', ''), r.get('aboveHelp'))
        check('stop below the default stop is blocked', r.get('belowDefaultEnabled') is False and 'default stop' in r.get('belowDefaultHelp', ''), r.get('belowDefaultHelp'))
        check('a wide stop shows the reference (not the tight warning)', 'Very tight' not in r.get('widerHelp', '') and 'For reference' in r.get('widerHelp', ''), r.get('widerHelp'))
        check('target at/below price is blocked', r.get('tgtBadEnabled') is False)
        check('quick-pick chips offered while in profit', r.get('chips') == ['Breakeven', 'Keep 75%', 'Keep 50%'], r.get('chips'))
        check('three requests were sent (stop, target, close)', len(puts) == 3, len(puts))
        if len(puts) == 3:
            s, t, c = puts
            rq = s.get('futures_mu_levels_request', {})
            check('stop request carries the price, the position id and a timestamp',
                  abs(rq.get('stop', 0) - exp) < 0.011 and rq.get('for_opened_at') == OPENED and rq.get('requested_at'), rq)
            check('the rest of the config survived the write', s.get('some_other_setting') == 'must survive' and s.get('futures_trade_amount_usdt') == 40, list(s))
            check('target request sent', t.get('futures_mu_levels_request', {}).get('target') == 1040.0, t.get('futures_mu_levels_request'))
            check('close-now sets the same flags the old button did', c.get('futures_mu_close_requested') is True and c.get('futures_mu_close_requested_at'), list(c))

        r, puts = run_case(page, sel, 'custom', copy.deepcopy(CUSTOM))
        check('custom levels are displayed', 'Custom stop' in r.get('active', '') and 'Take profit' in r.get('active', '') and '1015.00' in r.get('active', ''), r.get('active'))
        check("bot's confirmation is displayed", 'stop set' in r.get('status', ''), r.get('status'))
        check('Reset and Clear send their requests', len(puts) == 2 and puts[0]['futures_mu_levels_request'].get('reset_stop') is True
              and puts[1]['futures_mu_levels_request'].get('clear_target') is True, puts)

        r, puts = run_case(page, sel, 'flat', copy.deepcopy(FLAT))
        check('controls hidden when there is no position', r.get('hidden') is True or r.get('rootDisplay') == 'none', r)
        check('nothing sent when flat', puts == [])

    print('\n%s' % ('ALL BROWSER TESTS PASSED' if not FAILS else 'FAILED: %d -> %s' % (len(FAILS), FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == '__main__':
    main()

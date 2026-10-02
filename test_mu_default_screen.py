#!/usr/bin/env python3
"""Headless-browser test of the 2026-10-01 change: index.html (mobile) should land on the MU screen by
default, UNLESS a real futures position is open somewhere, in which case that takes priority.
Reuses the mock server from test_mu_ui.py. Run: python3 test_mu_default_screen.py"""
import copy
import html
import json
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, '.')
import test_mu_ui as T

FUT_FLAT = {'position': None, 'price': 100.0, 'indicators': {}, 'trail': {}, 'performance': {}}
FUT_OPEN = dict(FUT_FLAT, position='LONG', entry_price=95.0)
MU_FLAT = dict(T.FLAT)

SCEN = """<script>
(function(){ setTimeout(async function(){
  var R = {};
  try {
    R.tabHomeActive = document.getElementById('tab-home').classList.contains('active');
    R.tabMuActive = document.getElementById('tab-mu').classList.contains('active');
    R.statusBar = document.getElementById('status-bar').textContent;
    R.activeSymClass = document.getElementById('sym-mu') ? document.getElementById('sym-mu').className : null;
    R.nbisClass = document.getElementById('sym-nbis') ? document.getElementById('sym-nbis').className : null;
  } catch (e) { R.error = String(e && e.stack || e); }
  document.body.insertAdjacentHTML('beforeend', '<pre id="__' + 't">' + JSON.stringify(R) + '</pre>');
}, 3000); })();
</script>"""


def run(fut_fixture_for, mu_fixture):
    tmp = tempfile.mkdtemp()
    import shutil, os
    shutil.copy('dashboard/index.html', tmp)
    shutil.copy('dashboard/mu_levels.js', tmp)
    src = open(tmp + '/index.html').read()
    src = src.replace('<head>', '<head>' + T.STUBS, 1).replace('</body>', SCEN + '</body>', 1)
    open(tmp + '/test_index.html', 'w').write(src)

    class Srv(T.Server):
        def __init__(self, root):
            self.fut_fixture_for = fut_fixture_for
            super().__init__(root, mu_fixture)

    srv = Srv(tmp)
    orig_do_get = srv.httpd.RequestHandlerClass.do_GET

    def do_GET(self):
        p = self.path.split('?')[0]
        m = re.match(r'/data_futures_(\w+)\.json', p)
        if m:
            fx = fut_fixture_for(m.group(1))
            return self._json(fx)
        return orig_do_get(self)
    srv.httpd.RequestHandlerClass.do_GET = do_GET

    try:
        proc = subprocess.run([T.CHROME, '--headless=new', '--disable-gpu', '--no-sandbox', '--window-size=420,900',
                               '--virtual-time-budget=20000', '--dump-dom', 'http://127.0.0.1:%d/test_index.html?view=mobile' % srv.port],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        m = re.search(r'<pre id="__t">([^<]*)</pre>', proc.stdout)
        return json.loads(html.unescape(m.group(1))) if m else {'error': 'no result (page did not finish)'}
    finally:
        srv.stop()
        import shutil as sh
        sh.rmtree(tmp, ignore_errors=True)


FAILS = []


def check(name, cond, detail=''):
    print(('  ok   ' if cond else '  FAIL ') + name + ('' if cond else '   -> ' + str(detail)))
    if not cond:
        FAILS.append(name)


print('== scenario 1: everything flat -> should land on MU ==')
r = run(lambda base: FUT_FLAT, MU_FLAT)
check('no script error', 'error' not in r, r.get('error'))
check('tab-home NOT active', r.get('tabHomeActive') is False, r)
check('tab-mu IS active', r.get('tabMuActive') is True, r)
check('status bar shows MU', 'MU overnight' in r.get('statusBar', ''), r.get('statusBar'))
check('MU sym-tab marked active', 'active-mu' in (r.get('activeSymClass') or ''), r.get('activeSymClass'))

print('\n== scenario 2: NBIS has an open LONG -> should override to show NBIS, not MU ==')
r = run(lambda base: FUT_OPEN if base == 'nbis' else FUT_FLAT, MU_FLAT)
check('no script error', 'error' not in r, r.get('error'))
check('tab-home IS active (futures view)', r.get('tabHomeActive') is True, r)
check('tab-mu NOT active', r.get('tabMuActive') is False, r)
check('NBIS sym-tab marked active', 'active-nbis' in (r.get('nbisClass') or ''), r.get('nbisClass'))

print('\n%s' % ('ALL PASSED' if not FAILS else 'FAILED: %d -> %s' % (len(FAILS), FAILS)))
sys.exit(1 if FAILS else 0)

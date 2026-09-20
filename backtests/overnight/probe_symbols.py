#!/usr/bin/env python3
"""Read-only probe: real max leverage (leverageBracket), lot filters, spread, status for the Sentinel paper symbols.
Only GET requests. Prints no secrets."""
import hashlib, hmac, json, os, time, urllib.parse, urllib.request

BASE = 'https://fapi.binance.com'
SYMS = ['SNDKUSDT', 'WDCUSDT', 'LITEUSDT', 'MUUSDT']
KEY, SEC = os.environ.get('BINANCE_API_KEY', ''), os.environ.get('BINANCE_API_SECRET', '')


def get(path, params=None, signed=False):
    p = dict(params or {})
    if signed:
        p['timestamp'] = int(time.time() * 1000)
        p['recvWindow'] = 10000
        q = urllib.parse.urlencode(p)
        p_sig = hmac.new(SEC.encode(), q.encode(), hashlib.sha256).hexdigest()
        url = '%s%s?%s&signature=%s' % (BASE, path, q, p_sig)
    else:
        url = '%s%s?%s' % (BASE, path, urllib.parse.urlencode(p))
    req = urllib.request.Request(url, headers={'X-MBX-APIKEY': KEY} if signed else {})
    return json.loads(urllib.request.urlopen(req, timeout=20).read().decode())


info = {s['symbol']: s for s in get('/fapi/v1/exchangeInfo')['symbols']}
for sym in SYMS:
    s = info.get(sym)
    print('=' * 100)
    if not s:
        print(sym, 'NOT LISTED'); continue
    f = {x['filterType']: x for x in s['filters']}
    print('%s  status=%s contractType=%s underlyingType=%s onboard=%s' % (
        sym, s['status'], s.get('contractType'), s.get('underlyingType'),
        time.strftime('%Y-%m-%d', time.gmtime(s['onboardDate'] / 1000))))
    print('  LOT_SIZE step=%s min=%s | MARKET_LOT_SIZE step=%s max=%s | PRICE tick=%s | MIN_NOTIONAL=%s' % (
        f['LOT_SIZE']['stepSize'], f['LOT_SIZE']['minQty'], f['MARKET_LOT_SIZE']['stepSize'], f['MARKET_LOT_SIZE']['maxQty'],
        f['PRICE_FILTER']['tickSize'], f.get('MIN_NOTIONAL', {}).get('notional')))
    bt = get('/fapi/v1/ticker/bookTicker', {'symbol': sym})
    bid, ask = float(bt['bidPrice']), float(bt['askPrice'])
    print('  book: bid %s x %s | ask %s x %s | spread %.4f%%' % (bt['bidPrice'], bt['bidQty'], bt['askPrice'], bt['askQty'], (ask - bid) / bid * 100))
    br = get('/fapi/v1/leverageBracket', {'symbol': sym}, signed=True)
    br = br[0]['brackets'] if isinstance(br, list) else br['brackets']
    for b in br:
        print('  bracket %d: notional %.0f -> %.0f  maxLeverage %dx  maintMarginRatio %s' % (
            b['bracket'], b['notionalFloor'], b['notionalCap'], b['initialLeverage'], b['maintMarginRatio']))
    # highest L such that the bracket holding notional=100*L allows L
    best = None
    for L in range(125, 0, -1):
        n = 100.0 * L
        bk = [b for b in br if b['notionalFloor'] <= n < b['notionalCap']]
        if bk and bk[0]['initialLeverage'] >= L:
            best = (L, n); break
    print('  => max usable leverage at $100 margin: %sx (notional $%s)' % (best[0], '{:,.0f}'.format(best[1])))

#!/usr/bin/env python3
"""Pull every 1-minute kline + full funding-rate history Binance has for the
given TradFi perps (read-only public endpoints). Throttled, retrying, and
RESUMABLE: per-symbol CSV is appended to, restarts continue from the last
bar on disk. Writes DONE marker when finished."""
import json, os, sys, time
import requests

OUT = os.path.dirname(os.path.abspath(__file__))
BASE = 'https://fapi.binance.com'
SYMS = ['NVDAUSDT', 'SPYUSDT', 'MUUSDT']


def get(path, params):
    last = None
    for attempt in range(12):
        try:
            r = requests.get(BASE + path, params=params, timeout=40)
            used = int(r.headers.get('X-MBX-USED-WEIGHT-1M', '0') or 0)
            if r.status_code in (418, 429):
                wait = int(r.headers.get('Retry-After', '30'))
                print('rate limited; sleeping', wait, flush=True)
                time.sleep(wait + 1)
                continue
            if r.status_code >= 500 or r.status_code == 408:
                raise RuntimeError('HTTP %d' % r.status_code)
            r.raise_for_status()
            if used > 1400:
                time.sleep(15)
            return r.json()
        except Exception as e:                      # timeouts, resets, 408/5xx: back off and retry
            last = e
            wait = min(60, 2 ** attempt)
            print('retry %d after error (%s); sleeping %ds' % (attempt + 1, e, wait), flush=True)
            time.sleep(wait)
    raise RuntimeError('retries exhausted: %s' % last)


def last_ts(path):
    if not os.path.exists(path):
        return None
    last = None
    with open(path) as f:
        for line in f:
            last = line
    if not last or last.startswith('t,'):
        return None
    return int(last.split(',')[0])


def main():
    end_ms = (int(time.time()) // 60) * 60 * 1000 - 60000   # last fully-closed minute
    for sym in SYMS:
        path = os.path.join(OUT, sym + '_1m.csv')
        resume = last_ts(path)
        n = 0
        mode = 'a' if resume else 'w'
        s = (resume + 60000) if resume else 0
        with open(path, mode) as f:
            if not resume:
                f.write('t,o,h,l,c,v\n')
            while s < end_ms:
                data = get('/fapi/v1/klines', {'symbol': sym, 'interval': '1m', 'startTime': s,
                                               'endTime': end_ms, 'limit': 1500})
                if not data:
                    break
                for k in data:
                    f.write('%d,%s,%s,%s,%s,%s\n' % (k[0], k[1], k[2], k[3], k[4], k[5]))
                f.flush()
                n += len(data)
                s = data[-1][0] + 60000
                print(sym, '+%d bars, now at' % n, time.strftime('%Y-%m-%d %H:%M', time.gmtime(s / 1000)), flush=True)
                time.sleep(0.5)
        print(sym, 'DONE (this run added %d bars)' % n, flush=True)

        fpath = os.path.join(OUT, sym + '_funding.json')
        if not os.path.exists(fpath):
            fund, s = [], 0
            while True:
                data = get('/fapi/v1/fundingRate', {'symbol': sym, 'startTime': s, 'limit': 1000})
                if not data:
                    break
                fund += data
                s = data[-1]['fundingTime'] + 1
                if len(data) < 1000:
                    break
                time.sleep(0.5)
            json.dump(fund, open(fpath, 'w'))
            print(sym, 'funding records', len(fund), flush=True)
    open(os.path.join(OUT, 'DONE'), 'w').write('ok')
    print('ALL DONE', flush=True)


if __name__ == '__main__':
    main()

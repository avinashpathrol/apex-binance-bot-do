#!/usr/bin/env python3
"""Randomized-order, resumable Dukascopy 1-min BID candle downloader (Nasdaq-100 index CFD, weekdays 2019-01-02..2026-09-18).
Random order => any prefix of the downloaded days is a random sample of the whole period.
Keeps 06:00-21:15 UTC (EMA warm-up + the US session in both DST regimes).  Run with /usr/bin/python3 (has lzma)."""
import lzma, os, random, struct, sys, threading, time, urllib.request, urllib.error
from datetime import date, timedelta, datetime, timezone

INST = 'USATECHIDXUSD'
START, END = date(2019, 1, 2), date(2026, 9, 18)
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'duka', INST)
UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
KEEP_FROM, KEEP_TO = 6 * 3600, 21 * 3600 + 15 * 60
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 4
lock, penalty, last_req, done = threading.Lock(), [0.0], [0.0], [0]


def pace():
    with lock:
        wait = max(0.25 - (time.time() - last_req[0]), penalty[0] - time.time())
        if wait > 0:
            time.sleep(wait)
        last_req[0] = time.time()


def fetch(d):
    path = os.path.join(OUT, d.isoformat() + '.csv')
    if os.path.exists(path):
        return
    url = 'https://datafeed.dukascopy.com/datafeed/%s/%04d/%02d/%02d/BID_candles_min_1.bi5' % (INST, d.year, d.month - 1, d.day)
    raw = None
    for attempt in range(10):
        pace()
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raw = b''
                break
            penalty[0] = time.time() + 20 + 10 * attempt
        except Exception:
            penalty[0] = time.time() + 20 + 10 * attempt
    if raw is None:
        return
    rows = []
    if raw:
        data = lzma.decompress(raw)
        base = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        for i in range(len(data) // 24):
            t, o, c, l, h, v = struct.unpack('>IIIIIf', data[i * 24:(i + 1) * 24])
            if KEEP_FROM <= t <= KEEP_TO:
                rows.append('%d,%.3f,%.3f,%.3f,%.3f,%.4f' % (base + t, o / 1000.0, h / 1000.0, l / 1000.0, c / 1000.0, v))
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write('\n'.join(rows))
    os.replace(tmp, path)
    done[0] += 1
    if done[0] % 25 == 0:
        print('%s  %d files this run' % (datetime.now().strftime('%H:%M:%S'), done[0]), flush=True)


def worker(jobs):
    while True:
        with lock:
            if not jobs:
                return
            d = jobs.pop()
        try:
            fetch(d)
        except Exception as e:
            print('ERR', d, type(e).__name__, e, flush=True)


if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    days, d = [], START
    while d <= END:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    random.Random(2026).shuffle(days)
    print('%d weekdays to fetch (random order), %d workers' % (len(days), WORKERS), flush=True)
    ts = [threading.Thread(target=worker, args=(days,), daemon=True) for _ in range(WORKERS)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    print('ALL DONE', flush=True)

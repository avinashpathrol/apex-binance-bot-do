// Unit tests for dashboard/mu_levels.js pure helpers.  Run: node test_mu_levels.js
const assert = require('assert');
const L = require('./dashboard/mu_levels.js');
let n = 0; const t = (name, fn) => { fn(); n++; console.log('ok  ' + name); };
const d = { qty: 1.5, entry_price: 1000, entry_fee: 0.75, fee_rate: 0.0005, current_price: 1025, sl_default: 965 };

t('netAt matches the bot formula (same numbers as test_mu_levels.py)', () => {
  assert.ok(Math.abs(L.netAt(d, 1030) - ((30 * 1.5) - 0.75 - 1.5 * 1030 * 0.0005)) < 1e-9);
  assert.ok(Math.abs(L.netAt(d, 1000) - (-0.75 - 1.5 * 1000 * 0.0005)) < 1e-9);
});
t('priceForNet is the exact inverse of netAt', () => {
  for (const px of [960, 1000, 1017.35, 1023.13, 1060.5]) assert.ok(Math.abs(L.priceForNet(d, L.netAt(d, px)) - px) < 1e-9, px);
});
t('breakeven price sits just above entry (covers both fees)', () => {
  const be = L.priceForNet(d, 0); assert.ok(be > 1000 && be < 1002, be); assert.ok(Math.abs(L.netAt(d, be)) < 1e-9);
});
t('the user example: +$20 open, keep +$18', () => {
  const px = L.priceForNet(d, 18); assert.ok(Math.abs(L.netAt(d, px) - 18) < 1e-9);
});
t('default fee applied when fee_rate is missing', () => {
  const e = Object.assign({}, d); delete e.fee_rate; assert.ok(Math.abs(L.netAt(e, 1030) - L.netAt(d, 1030)) < 1e-12);
});
t('noiseHint hits the measured table values at the knots', () => {
  const k = { 0.12: [65, 90], 0.5: [25, 68], 1: [10, 51], 2: [2, 32], 3.5: [0, 15] };
  for (const [x, [a, b]] of Object.entries(k)) { const h = L.noiseHint(+x); assert.strictEqual([h.m30, h.h8].join(), [a, b].join(), x); }
});
t('noiseHint falls as the stop gets further away, and is clamped at the ends', () => {
  let prev = 101; for (const x of [0.05, 0.12, 0.2, 0.4, 0.8, 1.5, 2.5, 3.5, 10]) { const h = L.noiseHint(x); assert.ok(h.m30 <= prev, x); prev = h.m30; }
  assert.strictEqual(L.noiseHint(0.01).m30, 65); assert.strictEqual(L.noiseHint(50).h8, 15);
});
t('"tight" warning is on at 0.5% and below, off beyond', () => {
  assert.ok(L.noiseHint(0.17).tight); assert.ok(L.noiseHint(0.5).tight); assert.ok(!L.noiseHint(0.6).tight); assert.ok(!L.noiseHint(2).tight);
});
t('validateStop mirrors the bot rules', () => {
  assert.ok(!L.validateStop(d, NaN).ok); assert.ok(!L.validateStop(d, 0).ok);
  assert.ok(!L.validateStop(d, 965).ok);                 // must be ABOVE the default stop
  assert.ok(!L.validateStop(d, 900).ok);
  assert.ok(!L.validateStop(d, 1025).ok);                // at the price
  assert.ok(!L.validateStop(d, 1024.6).ok);              // inside the 0.05% gap (bot rejects this too)
  assert.ok(!L.validateStop(d, 1100).ok);
  assert.ok(L.validateStop(d, 1024.4).ok); assert.ok(L.validateStop(d, 990).ok); assert.ok(L.validateStop(d, 965.01).ok);
});
t('validateTarget mirrors the bot rules', () => {
  assert.ok(!L.validateTarget(d, NaN).ok); assert.ok(!L.validateTarget(d, 1000).ok); assert.ok(!L.validateTarget(d, 1025).ok); assert.ok(!L.validateTarget(d, 1025.5).ok);
  assert.ok(L.validateTarget(d, 1026).ok); assert.ok(L.validateTarget(d, 1100).ok);
});
t('MIN_GAP equals the bot constant', () => {
  const py = require('fs').readFileSync('trading_bot_futures.py', 'utf8').match(/MU_LEVEL_MIN_GAP\s*=\s*([0-9.]+)/)[1];
  assert.strictEqual(L.MIN_GAP, parseFloat(py));
});
console.log('\n' + n + ' JS tests passed');

/* MU exit-level controls -- shared by command.html (desktop) and index.html (mobile).
 *
 * Lets the user (a) set a STOP price ("sell if MU falls to $X" -- e.g. lock in profit), (b) set a TAKE-PROFIT
 * price, (c) reset / clear them, and (d) close at market. The dashboard only writes ONE request into
 * bot_config.json (futures_mu_levels_request); the bot validates it, applies it to the open position, and
 * enforces it in software (checked once per bot cycle, ~40 s -- a fast drop can gap past a stop).
 *
 * Pure helpers are exported for unit tests (test_mu_levels.js); mount() builds the UI.
 * Keep MIN_GAP in sync with MU_LEVEL_MIN_GAP in trading_bot_futures.py.
 */
(function (root) {
  'use strict';
  var MIN_GAP = 0.0005;                    // a stop/target must be >= 0.05% away from the current price
  var DEFAULT_FEE = 0.0005;                // 0.05% taker per side

  // How often MU has dipped AT LEAST this far below its price (measured from a random moment) within
  // 30 minutes / 8 hours. 238,617 one-minute bars of the MUUSDT perp, Mar-Sep 2026.
  //          distance %, P(30 min) %, P(8 h) %
  var NOISE = [[0.12, 65, 90], [0.25, 45, 81], [0.5, 25, 68], [1.0, 10, 51], [2.0, 2, 32], [3.5, 0, 15]];

  function num(v) { var f = parseFloat(v); return isNaN(f) ? NaN : f; }
  function feeRate(d) { return d && d.fee_rate != null ? +d.fee_rate : DEFAULT_FEE; }

  /* Net P&L (after the entry fee already paid and the exit fee) if the long is closed at px. */
  function netAt(d, px) {
    var q = +d.qty, e = +d.entry_price, ef = +(d.entry_fee || 0);
    return (px - e) * q - ef - q * px * feeRate(d);
  }
  /* Inverse of netAt: the price at which closing nets `net`. */
  function priceForNet(d, net) {
    var q = +d.qty, e = +d.entry_price, ef = +(d.entry_fee || 0), f = feeRate(d);
    return (net + ef + e * q) / (q * (1 - f));
  }

  /* Chance an ordinary wiggle reaches a stop this far (in %) below the price. Log-interpolated. */
  function noiseHint(distPct) {
    var t = NOISE, x = Math.max(distPct, 0);
    var m30, h8;
    if (x <= t[0][0]) { m30 = t[0][1]; h8 = t[0][2]; }
    else if (x >= t[t.length - 1][0]) { m30 = t[t.length - 1][1]; h8 = t[t.length - 1][2]; }
    else {
      for (var i = 0; i < t.length - 1; i++) {
        if (x >= t[i][0] && x <= t[i + 1][0]) {
          var f = (Math.log(x) - Math.log(t[i][0])) / (Math.log(t[i + 1][0]) - Math.log(t[i][0]));
          m30 = t[i][1] + f * (t[i + 1][1] - t[i][1]);
          h8 = t[i][2] + f * (t[i + 1][2] - t[i][2]);
          break;
        }
      }
    }
    return { m30: Math.round(m30), h8: Math.round(h8), tight: m30 >= 25 };
  }

  function validateStop(d, stop) {
    if (isNaN(stop) || stop <= 0) return { ok: false, msg: 'Enter a price (or the profit you want to keep).' };
    var price = +d.current_price, def = +d.sl_default;
    if (def && stop <= def) return { ok: false, msg: 'Must be above the default stop ($' + def.toFixed(2) + ').' };
    if (stop >= price * (1 - MIN_GAP)) return { ok: false, msg: 'At or above the current price ($' + price.toFixed(2) + ') -- it would close immediately. Use Close instead.' };
    return { ok: true, msg: '' };
  }
  function validateTarget(d, tgt) {
    if (isNaN(tgt) || tgt <= 0) return { ok: false, msg: 'Enter a price (or the profit you want to reach).' };
    var price = +d.current_price;
    if (tgt <= price * (1 + MIN_GAP)) return { ok: false, msg: 'At or below the current price ($' + price.toFixed(2) + ') -- it would close immediately. Use Close instead.' };
    return { ok: true, msg: '' };
  }

  function money(x, dec) {
    dec = dec == null ? 2 : dec;
    if (isNaN(x)) return '--';
    return (x < 0 ? '-$' : '+$') + Math.abs(x).toFixed(dec);
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function ago(iso) {
    if (!iso) return '';
    var s = (Date.now() - new Date(iso).getTime()) / 1000;
    return s < 90 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago' : Math.round(s / 3600) + ' h ago';
  }

  var CSS = '.mxl{font-size:12px;color:var(--text,#e5e7eb)}' +
    '.mxl-title{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted,#9ca3af);margin-bottom:8px}' +
    '.mxl-active{display:flex;flex-direction:column;gap:5px;margin-bottom:10px}' +
    '.mxl-tag{display:flex;align-items:center;justify-content:space-between;gap:8px;background:rgba(255,255,255,.05);border-radius:8px;padding:7px 9px}' +
    '.mxl-row{border-top:1px solid var(--border,#262c3a);padding:10px 0 4px}' +
    '.mxl-row label{display:block;font-weight:600;margin-bottom:6px}' +
    '.mxl-in{display:flex;align-items:center;gap:6px;flex-wrap:wrap}' +
    '.mxl-in input{width:96px;padding:8px 8px;border-radius:8px;border:1px solid var(--border,#262c3a);background:rgba(0,0,0,.25);color:inherit;font:inherit;font-size:14px}' +
    '.mxl-chips{display:flex;gap:6px;flex-wrap:wrap;margin:7px 0 2px}' +
    '.mxl-chip{padding:5px 9px;border-radius:14px;border:1px solid var(--border,#262c3a);background:transparent;color:var(--muted,#9ca3af);font:inherit;font-size:11px;cursor:pointer}' +
    '.mxl-help{min-height:16px;margin:6px 0;line-height:1.45;color:var(--muted,#9ca3af)}' +
    '.mxl-help .warn{color:var(--amber,var(--yellow,#f59e0b))}.mxl-help .bad{color:var(--red,#ef4444)}.mxl-help .good{color:var(--green,#22c55e)}' +
    '.mxl-btn{padding:9px 14px;border-radius:9px;border:0;font:inherit;font-weight:700;font-size:13px;cursor:pointer;background:var(--accent,#6366f1);color:#fff}' +
    '.mxl-btn:disabled{opacity:.35;cursor:not-allowed}' +
    '.mxl-btn.ghost{background:transparent;border:1px solid var(--border,#262c3a);color:var(--muted,#9ca3af);padding:4px 9px;font-size:11px;font-weight:600}' +
    '.mxl-btn.danger{background:#dc2626;width:100%;margin-top:10px;min-height:42px}' +
    '.mxl-note{margin-top:8px;font-size:10.5px;line-height:1.45;color:var(--muted,#9ca3af)}' +
    '.mxl-status{float:right;text-transform:none;letter-spacing:0;font-weight:500}';

  var TEMPLATE =
    '<div class="mxl"><div class="mxl-title">Exit controls <span class="mxl-status" data-r="status"></span></div>' +
    '<div class="mxl-active" data-r="active"></div>' +
    '<div class="mxl-row"><label>Stop -- sell if MU falls to</label>' +
    '<div class="mxl-in"><span>$</span><input type="number" inputmode="decimal" step="0.01" data-r="stopPrice" placeholder="price">' +
    '<span>or keep</span><span>$</span><input type="number" inputmode="decimal" step="0.01" data-r="stopProfit" placeholder="profit"></div>' +
    '<div class="mxl-chips" data-r="chips"></div><div class="mxl-help" data-r="stopHelp"></div>' +
    '<button class="mxl-btn" data-r="setStop" disabled>Set stop</button></div>' +
    '<div class="mxl-row"><label>Take profit -- sell if MU rises to</label>' +
    '<div class="mxl-in"><span>$</span><input type="number" inputmode="decimal" step="0.01" data-r="tgtPrice" placeholder="price">' +
    '<span>or reach</span><span>$</span><input type="number" inputmode="decimal" step="0.01" data-r="tgtProfit" placeholder="profit"></div>' +
    '<div class="mxl-help" data-r="tgtHelp"></div><button class="mxl-btn" data-r="setTgt" disabled>Set take-profit</button></div>' +
    '<button class="mxl-btn danger" data-r="closeNow">Close MU now at market</button>' +
    '<div class="mxl-note">The bot enforces these in software, checking about every 40 s. If MU falls faster than that it can slip past your stop and sell lower, so the price you set is a trigger, not a guaranteed fill. Profit figures are after fees.</div></div>';

  function mount(el, opts) {
    if (!document.getElementById('mxl-css')) {
      var st = document.createElement('style'); st.id = 'mxl-css'; st.textContent = CSS; document.head.appendChild(st);
    }
    el.innerHTML = TEMPLATE;
    var r = {};
    [].forEach.call(el.querySelectorAll('[data-r]'), function (n) { r[n.getAttribute('data-r')] = n; });
    var busy = false;
    var cache = {}, lastChipKey = null;
    function setHtml(node, key, html) { if (cache[key] !== html) { node.innerHTML = html; cache[key] = html; } }   // only touch the DOM when the content changed

    function d() { return opts.getData(); }
    function open() { var x = d(); return x && x.position === 'LONG'; }

    function bindPair(kind) {
      var keys = kind === 'stop' ? ['stopPrice', 'stopProfit'] : ['tgtPrice', 'tgtProfit'];
      r[keys[0]].addEventListener('input', function () {
        var x = d(); if (!open()) return; var p = num(r[keys[0]].value);
        r[keys[1]].value = isNaN(p) ? '' : netAt(x, p).toFixed(2); update(true);
      });
      r[keys[1]].addEventListener('input', function () {
        var x = d(); if (!open()) return; var q = num(r[keys[1]].value);
        r[keys[0]].value = isNaN(q) ? '' : priceForNet(x, q).toFixed(2); update(true);
      });
    }
    bindPair('stop'); bindPair('tgt');

    function chip(label, ratio) {                     // ratio of the CURRENT profit to keep (0 = breakeven), computed at click time
      var b = document.createElement('button'); b.className = 'mxl-chip'; b.textContent = label;
      b.addEventListener('click', function () {
        var x = d(); if (!open()) return;
        var net = ratio * netAt(x, +x.current_price);
        r.stopPrice.value = priceForNet(x, net).toFixed(2); r.stopProfit.value = netAt(x, +r.stopPrice.value).toFixed(2); update(true);
      });
      return b;
    }

    function send(req, okMsg) {
      var x = d();
      if (busy || !open()) return;
      busy = true;
      req.for_opened_at = x.opened_at; req.requested_at = new Date().toISOString();
      Promise.resolve(opts.send({ futures_mu_levels_request: req })).then(function () {
        opts.toast(okMsg || 'Sent -- the bot applies it within about a minute');
        if (opts.onSent) opts.onSent();
      }).catch(function (e) { opts.toast('Failed: ' + (e && e.message || e)); }).then(function () { busy = false; });
    }

    r.setStop.addEventListener('click', function () {
      var x = d(), stop = num(r.stopPrice.value), v = validateStop(x, stop); if (!v.ok) return;
      if (!opts.confirm('Set MU stop at $' + stop.toFixed(2) + '? You would keep about ' + money(netAt(x, stop)) + ' after fees if it fills there.')) return;
      send({ stop: stop }, 'Stop sent -- watch "Exit controls" for confirmation');
    });
    r.setTgt.addEventListener('click', function () {
      var x = d(), t = num(r.tgtPrice.value), v = validateTarget(x, t); if (!v.ok) return;
      if (!opts.confirm('Sell MU automatically if it reaches $' + t.toFixed(2) + ' (about ' + money(netAt(x, t)) + ' after fees)?')) return;
      send({ target: t }, 'Take-profit sent');
    });
    r.closeNow.addEventListener('click', function () { if (open()) opts.closeNow(); });
    r.active.addEventListener('click', function (e) {
      var a = e.target && e.target.getAttribute && e.target.getAttribute('data-act');
      if (a === 'reset') send({ reset_stop: true }, 'Stop reset to default');
      if (a === 'clear') send({ clear_target: true }, 'Take-profit cleared');
    });

    function update(fromInput) {
      var x = d(), show = open();
      el.style.display = show ? '' : 'none';
      if (!show) return;
      var price = +x.current_price, net = netAt(x, price);
      // active levels + bot confirmation
      var tags = [];
      tags.push('<div class="mxl-tag"><span>' + (x.sl_custom ? '&#128737; Custom stop <b>$' + (+x.sl_price).toFixed(2) + '</b> (keeps about ' + money(netAt(x, +x.sl_price)) + ')'
        : 'Default stop <b>$' + (+x.sl_price).toFixed(2) + '</b> (-3.5% from entry)') + '</span>' +
        (x.sl_custom ? '<button class="mxl-btn ghost" data-act="reset">Reset</button>' : '') + '</div>');
      if (x.target_price) tags.push('<div class="mxl-tag"><span>&#127919; Take profit <b>$' + (+x.target_price).toFixed(2) + '</b> (about ' + money(netAt(x, +x.target_price)) + ')</span><button class="mxl-btn ghost" data-act="clear">Clear</button></div>');
      setHtml(r.active, 'active', tags.join(''));
      var ls = x.levels_status;
      setHtml(r.status, 'status', ls ? (ls.ok ? '<span class="good">&#10003; ' : '<span class="warn">&#9888; ') + esc(ls.msg).slice(0, 120) + '</span> <span style="opacity:.6">' + ago(ls.at) + '</span>' : '');
      // quick chips (only meaningful while in profit); rebuilt only when the position or the profit sign changes
      var chipKey = (x.opened_at || '') + '|' + (net > 0 ? 'p' : 'n');
      if (chipKey !== lastChipKey) {
        lastChipKey = chipKey; r.chips.innerHTML = '';
        r.chips.appendChild(chip('Breakeven', 0));
        if (net > 0) { r.chips.appendChild(chip('Keep 75%', 0.75)); r.chips.appendChild(chip('Keep 50%', 0.5)); }
      }
      // stop validation + help
      var stop = num(r.stopPrice.value), h = '';
      if (r.stopPrice.value === '') { h = 'Now ' + money(net) + ' net at $' + price.toFixed(2) + '. Type a price, or the profit you want to keep.'; r.setStop.disabled = true; }
      else {
        var v = validateStop(x, stop); r.setStop.disabled = !v.ok;
        if (!v.ok) h = '<span class="bad">' + esc(v.msg) + '</span>';
        else {
          var dist = (1 - stop / price) * 100, nh = noiseHint(dist);
          h = 'Sells at market if the price falls to <b>$' + stop.toFixed(2) + '</b> (' + dist.toFixed(2) + '% below now) &rarr; you keep about <b>' + money(netAt(x, stop)) + '</b>.';
          h += nh.tight ? '<br><span class="warn">&#9888; Very tight: in the past 6 months MU dipped this far about ' + nh.m30 + '% of the time within 30 min (' + nh.h8 + '% within 8 h), so this would likely trigger on normal wiggles.</span>'
            : '<br>For reference, MU dipped this far about ' + nh.m30 + '% of the time within 30 min (' + nh.h8 + '% within 8 h).';
        }
      }
      setHtml(r.stopHelp, 'stopHelp', h);
      // target validation + help
      var t = num(r.tgtPrice.value), th = '';
      if (r.tgtPrice.value === '') { th = 'Optional. Sells at market when the price rises to your level.'; r.setTgt.disabled = true; }
      else {
        var tv = validateTarget(x, t); r.setTgt.disabled = !tv.ok;
        th = tv.ok ? 'Sells at market if the price rises to <b>$' + t.toFixed(2) + '</b> &rarr; about <b>' + money(netAt(x, t)) + '</b> after fees.' : '<span class="bad">' + esc(tv.msg) + '</span>';
      }
      setHtml(r.tgtHelp, 'tgtHelp', th);
    }
    update();
    return { update: function () { update(false); } };
  }

  var API = { netAt: netAt, priceForNet: priceForNet, noiseHint: noiseHint, validateStop: validateStop, validateTarget: validateTarget, mount: mount, MIN_GAP: MIN_GAP };
  root.MuLevels = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof window !== 'undefined' ? window : globalThis);

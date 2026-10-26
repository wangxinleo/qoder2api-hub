/* 官方积分消耗面板：空逐日数据 / 企业版不适用 / 账号状态签名。
 *
 * 为什么测：旧实现在 daily 为空时直接早退，只渲染一句「逐日消耗为空」，
 * 把账号明细表一起藏掉——企业版账号恒为空，于是多账号面板永远看不到
 * 哪个账号是「不适用」、哪个是「查询失败」。这类退化不会抛错，只会让
 * 面板静默少显示信息，所以判据直接钉在**渲染输出**上：
 *   · 全企业版 + daily=[] -> 明细表仍渲染，行内是「不适用（企业版）」，不是「查询失败」；
 *   · 公共账号 daily=[] -> 明细表仍渲染，并说明可能还没产生用量；
 *   · 混合账号 -> 失败计数与企业版计数分开；
 *   · total/peak/daily 不变、仅账号状态变化 -> 必须重绘（签名含账号状态）。
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const ROOT = process.env.QD_DASH_ROOT || path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'dashboard.html'), 'utf8');
const SUITE_MD5 = crypto.createHash('md5').update(fs.readFileSync(__filename)).digest('hex');
let PASS = 0, FAIL = 0;
function check(name, cond, detail){
  if(cond){ PASS++; console.log('  ok   ' + name); }
  else { FAIL++; console.log('  FAIL ' + name + (detail === undefined ? '' : ' -> ' + JSON.stringify(detail))); }
}
function slice(from, to){
  const a = html.indexOf(from), b = html.indexOf(to);
  return (a >= 0 && b > a) ? html.slice(a, b) : '';
}
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = n => (n ?? 0).toLocaleString('en-US');

const FN = slice('async function loadCreditUsage(force){', '        // 上次渲染的数据签名');
check('锚点：loadCreditUsage 抽到', FN.length > 1200, FN.length);

function panel(payloads){
  const st = { sets: 0 };
  const box = {
    _html: '',
    querySelector(){ return null; },
    querySelectorAll(){ return []; },
  };
  Object.defineProperty(box, 'innerHTML', {
    get(){ return box._html; },
    set(v){ st.sets++; box._html = String(v ?? ''); },
  });
  const hint = { textContent: '' };
  const doc = { getElementById: id => id === 'creditUsageWrap' ? box
    : (id === 'creditUsageHint' ? hint : null) };
  let i = 0;
  const api = new Function('document', 'getJSON', 'fmt', 'statSetText', 'statBindTip', 'esc',
    'let CREDIT_USAGE_SIG = "", CREDIT_USAGE_AT = 0;\n' + FN
    + '; return loadCreditUsage;')(
    doc,
    async () => payloads[Math.min(i++, payloads.length - 1)],
    fmt,
    (el, text) => { if(el) el.textContent = String(text == null ? '' : text); },
    () => {},
    esc);
  return { api: api, box: box, hint: hint, st: st };
}

const ent = (uid, nick) => ({ uid: uid, nickname: nick, realm: 'cn', ok: false,
  unavailable: true, reason: 'enterprise_account', total: 0, peak: 0, peak_date: '',
  error: '企业版账号不参与每日签到/活动' });
const failed = (uid, nick, err) => ({ uid: uid, nickname: nick, realm: 'intl', ok: false,
  unavailable: false, reason: '', total: 0, peak: 0, peak_date: '', error: err });
const good = (uid, nick, total, peak, peakDate) => ({ uid: uid, nickname: nick,
  realm: 'intl', ok: true, unavailable: false, reason: '', total: total, peak: peak,
  peak_date: peakDate, error: '' });

(async () => {
  // ---------------------------------------------------------- ① 全企业版 + 空逐日
  let p = panel([{ accounts: [ent('e1', 'Ent One'), ent('e2', 'Ent Two')],
    total: 0, peak: 0, peak_date: '', daily: [] }]);
  await p.api(true);
  let h = p.box._html;
  check('全企业版：账号明细表仍然渲染（不因 daily 为空而早退）',
    h.includes('Ent One') && h.includes('Ent Two'), h);
  check('全企业版：行内说明「不适用（企业版）」而不是「查询失败」',
    (h.match(/不适用（企业版）/g) || []).length === 2 && !h.includes('查询失败'), h);
  check('全企业版：汇总区标注不适用账号数',
    h.includes('2 个企业版账号不适用'), h);
  check('全企业版：热力图区域给出企业版原因',
    h.includes('企业版账号不适用官方积分消耗统计') && !h.includes('heat-grid'), h);
  check('全企业版：顶部 hint 仍写官方口径累计', /官方口径 · 累计 0 积分/.test(p.hint.textContent),
    p.hint.textContent);

  // ---------------------------------------------------------- ② 公共账号 + 空逐日
  p = panel([{ accounts: [good('p1', 'Fresh One', 0, 0, '')],
    total: 0, peak: 0, peak_date: '', daily: [] }]);
  await p.api(true);
  h = p.box._html;
  check('公共账号空逐日：明细表渲染并提示可能还没产生用量',
    h.includes('Fresh One') && h.includes('官方返回的逐日消耗为空')
    && !h.includes('企业版账号不适用'), h);

  // ---------------------------------------------------------- ③ 混合：成功 / 失败 / 企业版
  p = panel([{
    accounts: [good('p1', 'Public One', 120, 40, '2026-10-20'),
               failed('p2', 'Public Bad', 'HTTP 500 boom'),
               ent('e1', 'Ent One')],
    total: 120, peak: 40, peak_date: '2026-10-20',
    daily: [{ date: '2026-10-20', value: 40 }],
  }]);
  await p.api(true);
  h = p.box._html;
  check('混合：热力图正常渲染', h.includes('heat-grid') && h.includes('data-tip="2026-10-20'), h);
  check('混合：失败计数只统计真实失败（不含企业版）',
    h.includes('1 个账号查询失败') && h.includes('1 个企业版账号不适用'), h);
  check('混合：成功行显示峰值日期、失败行显示错误、企业版行显示不适用',
    h.includes('2026-10-20') && h.includes('查询失败：HTTP 500 boom')
    && h.includes('不适用（企业版）'), h);

  // ------------------------------------------- ④ 签名：total/peak/daily 不变、账号状态变化
  const emptyMeta = { total: 0, peak: 0, peak_date: '', daily: [] };
  p = panel([
    Object.assign({ accounts: [failed('a1', 'Acct One', 'boom')] }, emptyMeta),
    Object.assign({ accounts: [good('a1', 'Acct One', 0, 0, '')] }, emptyMeta),
  ]);
  await p.api(true);
  check('签名回归：首次渲染为查询失败', p.box._html.includes('查询失败：boom'), p.box._html);
  await p.api(true);
  check('签名回归：仅账号状态变化也会重绘（不再停留在旧的查询失败）',
    !p.box._html.includes('查询失败'), p.box._html);

  // ---------------------------------------------------------- ⑤ 相同数据不重复重绘
  const same = Object.assign({ accounts: [ent('e1', 'Ent One')] }, emptyMeta);
  p = panel([same, same]);
  await p.api(true);
  const setsAfterFirst = p.st.sets;
  await p.api(true);
  check('相同数据第二次调用不重绘（60s 节流之外仍有签名去重）',
    p.st.sets === setsAfterFirst, { first: setsAfterFirst, now: p.st.sets });

  console.log('SUITE: ' + path.basename(__filename) + ' md5=' + SUITE_MD5);
  console.log('FILE: ' + path.basename(path.join(ROOT, 'dashboard.html'))
    + ' md5=' + crypto.createHash('md5').update(html).digest('hex')
    + ' bytes=' + Buffer.byteLength(html, 'utf8') + ' codeunits=' + html.length
    + ' as-of=' + new Date().toISOString());
  console.log('');
  console.log('SUMMARY: TOTAL ' + (PASS + FAIL) + ' checks, ' + PASS + ' passed, ' + FAIL + ' failed');
  console.log('RESULT: ' + (FAIL ? 'RED' : 'GREEN') + ' (exit ' + (FAIL ? 1 : 0) + ')');
  process.exit(FAIL ? 1 : 0);
})().catch(e => { console.error('SUITE ERROR', e); process.exit(2); });

/* 企业版 PAT 弹窗接线 + 账号行 badge（PRD A7）。
 *
 * 为什么测：企业实例的 PAT 只在 {实例}-openapi.vpc.qoder.com.cn 有效——前端如果把
 * 用户填的企业域名丢掉（或仍按区域单选发 realm=intl），后端就走公共链路；丢字段是
 * 静默的（请求 200 但导入失败/导入到错误区域），只有请求体断言能拦住。同理，
 * 「填域名必须锁死国内版」与「账号行必须能一眼分辨企业账号」都是用户可操作面的
 * 契约，断掉不会报错、只会让看板说谎。
 *
 * 判据拦在**请求体与渲染输出**上（不是「函数被调用过」）：
 *   · doImportPat 的 postJSON payload 必须原样含 enterpriseDomain / realm 规则；
 *   · 后端 400 原文（含企业 host）必须展示在弹窗正文；
 *   · accountRow 对有 enterpriseDomain 的账号渲染「企业 · <实例>」badge，公共账号不渲染。
 */
const fs = require('fs');
const path = require('path');
const ROOT = process.env.QD_DASH_ROOT || path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'dashboard.html'), 'utf8');
// 套件自身指纹在**启动时**取一次：它代表**真正被执行的那份字节**（运行中被编辑也不会让这行说谎）
const SUITE_MD5 = require('crypto').createHash('md5').update(require('fs').readFileSync(__filename)).digest('hex');
let PASS = 0, FAIL = 0;
function check(name, cond){ if(cond){ PASS++; console.log('  ok   ' + name); } else { FAIL++; console.log('  FAIL ' + name); } }
function slice(from, to){
  const a = html.indexOf(from), b = html.indexOf(to);
  return (a >= 0 && b > a) ? html.slice(a, b) : '';
}
// 与 dashboard.html 同款转义（被测函数用它做输出转义）
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function mkEl(init){
  const el = Object.assign({ value: '', checked: false, disabled: false,
    textContent: '', _html: '', classList: { add(){}, remove(){}, contains(){ return false; } } },
    init || {});
  // 桩要照 DOM 语义做：innerHTML 的 setter 会把任何值转成字符串
  Object.defineProperty(el, 'innerHTML', { get(){ return el._html; },
    set(v){ el._html = String(v ?? ''); } });
  return el;
}

// ---------------------------------------------------------------- 静态锚点
const MODAL = slice('id="patEntDomain"', '<div id="patBody"');
check('锚点：PAT 弹窗企业域名输入区抽到', MODAL.length > 300);
check('输入框 placeholder 为 acme.vpc.qoder.com.cn 且挂在 oninput 联动',
  /id="patEntDomain"[^>]*placeholder="acme\.vpc\.qoder\.com\.cn"/.test(MODAL)
  && /oninput="onPatEntDomainInput\(\)"/.test(MODAL));
check('帮助文案指向 {域名}/account/integrations 并声明企业账号不参与签到/活动',
  MODAL.includes('https://{域名}/account/integrations') && MODAL.includes('不参与每日签到/活动'));

const OPEN = slice('function openPatImport(){', '/* 填写企业域名即锁定为企业版');
const ENT = slice('function onPatEntDomainInput(){', 'function closePatImport(){');
const DO = slice('async function doImportPat(btn){', '/* ------------------------------- dashboard -');
const ROW = slice('function accountRow(a){', 'function renderAccounts(){');
check('锚点：openPatImport / onPatEntDomainInput / doImportPat / accountRow 全部抽到',
  OPEN.length > 300 && ENT.length > 150 && DO.length > 900 && ROW.length > 1500);

function fnEnv(viewRealm, init){
  const els = {
    patModal: mkEl(),
    patInput: mkEl({ value: init.pat || '' }),
    patEntDomain: mkEl({ value: init.ent || '' }),
    patRadioCn: mkEl({ checked: !!init.cn }),
    patRadioIntl: mkEl({ checked: !!init.intl }),
    patBody: mkEl(),
  };
  const doc = { getElementById: id => els[id] || null };
  const api = new Function('document', 'window', OPEN + ENT + '; return {openPatImport:openPatImport, onPatEntDomainInput:onPatEntDomainInput};')(
    doc, { VIEW_REALM: viewRealm });
  return { els: els, api: api, doc: doc };
}

// ------------------------------------------------- ① 区域单选联动（锁死国内版）
let env = fnEnv('cn', {});
env.api.onPatEntDomainInput();
check('未填域名：不触碰单选状态（国际版仍可点）',
  env.els.patRadioIntl.disabled === false && env.els.patRadioCn.checked === false);
env.els.patEntDomain.value = 'acme.vpc.qoder.com.cn';
env.api.onPatEntDomainInput();
check('填域名：国内版被选中、国际版禁用且取消勾选',
  env.els.patRadioCn.checked === true && env.els.patRadioIntl.disabled === true
  && env.els.patRadioIntl.checked === false);
env.els.patEntDomain.value = '  ';
env.api.onPatEntDomainInput();
check('清空（含纯空白）：国际版恢复可用', env.els.patRadioIntl.disabled === false);

env = fnEnv('intl', {});
env.els.patEntDomain.value = 'stale-ent';
env.api.openPatImport();
check('重开弹窗：企业域名输入框重置、国际版恢复可用（VIEW_REALM=intl 选中它）',
  env.els.patEntDomain.value === '' && env.els.patRadioIntl.disabled === false
  && env.els.patRadioIntl.checked === true && env.els.patRadioCn.checked === false);
env = fnEnv('cn', {});
env.api.openPatImport();
check('重开弹窗（VIEW_REALM=cn）：默认国内版、国际版未勾选',
  env.els.patRadioCn.checked === true && env.els.patRadioIntl.checked === false);

// ------------------------------------------------- ② 提交载荷（请求体断言）
function doEnv(init){
  const els = {
    patInput: mkEl({ value: init.pat }),
    patEntDomain: mkEl({ value: init.ent || '' }),
    patRadioIntl: mkEl({ checked: !!init.intl }),
    patBody: mkEl(),
  };
  const st = { posted: [], toasts: [], closed: 0 };
  const doc = { getElementById: id => els[id] || null };
  const api = new Function('document', 'esc', 'postJSON', 'toast', 'loadAccounts',
    'refresh', 'closePatImport', 'setTimeout', DO + '; return doImportPat;')(
    doc, esc,
    async (url, body) => {
      st.posted.push({ url: url, body: body });
      if(init.fail) throw new Error(init.fail);
      return { imported: [{ nickname: 'Ent One', uid: 'ent-uid-1' }] };
    },
    m => st.toasts.push(m),
    async () => {}, async () => {},
    () => { st.closed++; }, () => {});
  return { els: els, st: st, api: api };
}

(async () => {
  let e = doEnv({ pat: 'pt-ent', ent: 'acme.vpc.qoder.com.cn', intl: true });
  await e.api(null);
  let sent = e.st.posted[0];
  check('填域名时提交：打到 /accounts/import/pat 且 realm 强制 cn（忽略国际版勾选）',
    e.st.posted.length === 1 && sent && sent.url === '/accounts/import/pat'
    && sent.body.realm === 'cn', e.st.posted);
  check('请求体原样携带 enterpriseDomain（企业域名不被丢弃）',
    !!sent && sent.body.enterpriseDomain === 'acme.vpc.qoder.com.cn'
    && sent.body.pat === 'pt-ent', sent && sent.body);
  check('成功文案区分企业版（弹窗 + toast 都点名实例）',
    /企业版 acme\.vpc\.qoder\.com\.cn/.test(e.els.patBody.innerHTML)
    && e.st.toasts.some(m => /企业版 acme\.vpc\.qoder\.com\.cn/.test(m)),
    e.st.toasts);

  e = doEnv({ pat: 'pt-pub', intl: true });
  await e.api(null);
  sent = e.st.posted[0];
  check('公共路径零变化：不填域名时 payload 无 enterpriseDomain 且 realm=intl',
    e.st.posted.length === 1 && sent && sent.body.realm === 'intl'
    && !('enterpriseDomain' in sent.body), sent && sent.body);
  check('公共路径成功文案仍为「国际版」', /已导入 Ent One 到 国际版/.test(e.els.patBody.innerHTML),
    e.els.patBody.innerHTML);

  const SERVER_ERR = 'PAT import failed: 企业版 PAT 验证失败 '
    + '(https://acme-openapi.vpc.qoder.com.cn): HTTP 400 TOKEN_INVALID';
  e = doEnv({ pat: 'pt-bad', ent: 'acme', fail: SERVER_ERR });
  await e.api(null);
  check('后端 400 原文（含企业 host）原样展示在弹窗正文',
    e.els.patBody.innerHTML.includes('企业版 PAT 验证失败')
    && e.els.patBody.innerHTML.includes('acme-openapi.vpc.qoder.com.cn'),
    e.els.patBody.innerHTML);

  const BTN = { disabled: false, textContent: '导入 PAT' };
  e = doEnv({ pat: 'pt-pressure', ent: 'acme', fail: SERVER_ERR });
  await e.api(BTN);
  check('失败路径按钮恢复可点（finally 不吞）',
    BTN.disabled === false && BTN.textContent === '导入 PAT', BTN);
  check('失败时也保持企业域名输入框内容（不逼用户重填）', e.els.patEntDomain.value === 'acme');

  e = doEnv({ pat: 'not-a-pat', ent: 'acme' });
  await e.api(null);
  check('前缀校验仍最优先：非 pt- 不发请求且提示原文',
    e.st.posted.length === 0 && /令牌必须以 pt- 开头/.test(e.els.patBody.innerHTML),
    e.els.patBody.innerHTML);

  // ------------------------------------------------- ③ 账号行企业 badge
  // 同步上游 #90 后 accountRow 额外引用 teamBadge(a)（团队/组织徽标）。VPC 账号必须
  // 抑制它（否则「企业」徽标重复两次），非 VPC 团队账号则要显示上游徽标 —— 故这里
  // 注入可计数的桩，把「调没调」也钉死。
  const TB = { calls: 0 };
  const row = new Function('esc', 'USAGE_BY_ACCOUNT', 'accountGuardBadge', 'fmt',
    'checkinBadge', 'creditsDeltaChip', 'teamBadge',
    ROW + '; return accountRow;')(esc, {}, () => '', v => String(v ?? 0),
    () => '<ck-stub>', () => '<delta-stub>',
    (a) => { TB.calls++; return (a && a.isTeam) ? '<team-stub>' : ''; });
  const base = { uid: 'ent-uid-1', nickname: 'Ent One', realm: 'cn', enabled: true,
    source: 'pat', expiresIn: '23h', lastError: '', credits: null };
  const entHtml = row(Object.assign({}, base, { enterpriseDomain: 'acme' }));
  check('企业账号行渲染「企业 · acme」badge（含免签到 title 说明）',
    /企业 · acme/.test(entHtml) && /不参与每日签到\/活动/.test(entHtml), null);
  check('企业徽标与上游签到/积分 chip 同时渲染（同步冲突合并）',
    entHtml.includes('<ck-stub>') && entHtml.includes('<delta-stub>'), entHtml);
  check('VPC 账号抑制上游团队徽标（不重复出现两个「企业」徽标）',
    !entHtml.includes('<team-stub>') && TB.calls === 0,
    (TB.calls, entHtml.indexOf('team-stub')));
  const pubHtml = row(Object.assign({}, base, { enterpriseDomain: '' }));
  check('公共账号（空串）不渲染企业 badge', !/企业 ·/.test(pubHtml), pubHtml.indexOf('企业 ·'));
  const teamHtml = row(Object.assign({}, base, { enterpriseDomain: '',
    isTeam: true, orgLabel: 'AcmeCorp' }));
  check('非 VPC 团队账号渲染上游团队徽标（企业 · 组织）',
    teamHtml.includes('<team-stub>') && !/企业 · acme/.test(teamHtml), teamHtml);

  // 上游 teamBadge 本体：VPC 账号不走它（上面已断言零调用），团队账号文案为「企业 · <组织>」
  const TBFN = slice('function teamBadge(a){', '// 今日是否已签到');
  check('锚点：上游 teamBadge 定义抽到', TBFN.length > 200);
  const realTeamBadge = new Function('esc', TBFN + '; return teamBadge;')(esc);
  check('teamBadge 本体：团队账号渲染「企业 · 组织」、非团队账号空串',
    /企业 · AcmeCorp/.test(realTeamBadge({ isTeam: true, orgLabel: 'AcmeCorp' }))
    && realTeamBadge({ orgLabel: 'AcmeCorp' }) === ''
    && realTeamBadge(null) === '', realTeamBadge({ isTeam: true, orgLabel: 'AcmeCorp' }));

  // 企业额度到期小字（task 10-16 · R5）：真实到期显示、占位/公共号不显示
  const expDays = Math.floor(Date.now() / 1000) + 5 * 86400;
  const entCred = { remain: 851, size: 2000, expires_at: expDays };
  const entHtml5 = row(Object.assign({}, base, { enterpriseDomain: 'acme', credits: entCred }));
  check('企业号 + credits.expires_at 5 天后 -> 渲染「5 天后到期」小字',
    /额度 5 天后到期/.test(entHtml5), null);
  const pubHtmlExp = row(Object.assign({}, base, { enterpriseDomain: '', credits: entCred }));
  check('公共号（有到期值）不渲染「额度」小字', !/额度/.test(pubHtmlExp),
    pubHtmlExp.indexOf('额度'));
  const entHtmlHold = row(Object.assign({}, base, { enterpriseDomain: 'acme',
    credits: { remain: 851, size: 2000, expires_at: 253402214 } }));
  check('企业号 + 占位 expires_at=253402214（CN 永不过期）-> 不渲染「额度」小字',
    !/额度/.test(entHtmlHold), entHtmlHold.indexOf('额度'));

  // 指纹两行：**被测文件**与**套件自身**，各带 as-of（UTC）——并发编辑下先比时间戳，再谈要不要重跑
  console.log('SUITE: ' + path.basename(__filename) + ' md5=' + SUITE_MD5);
  console.log('FILE: ' + path.basename(path.join(ROOT, 'dashboard.html'))
    + ' md5=' + require('crypto').createHash('md5').update(html).digest('hex')
    + ' bytes=' + Buffer.byteLength(html, 'utf8') + ' codeunits=' + html.length
    + ' as-of=' + new Date().toISOString());
  console.log('');
  console.log('SUMMARY: TOTAL ' + (PASS + FAIL) + ' checks, ' + PASS + ' passed, ' + FAIL + ' failed');
  console.log('RESULT: ' + (FAIL ? 'RED' : 'GREEN') + ' (exit ' + (FAIL ? 1 : 0) + ')');
  process.exit(FAIL ? 1 : 0);
})().catch(e => { console.error('SUITE ERROR', e); process.exit(2); });

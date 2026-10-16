"""企业优先派单套件（R1 分层 / R2 设置 / R4 企业层内临期加权）。

    python tests/_test_enterprise_first.py

判据（design.md §3/§4/§6，全部落在选号函数与设置读写的判据层）：
  · 设置往返：set_limit -> limits_data 形状 -> apply_settings -> 池开关；realm 覆盖；
    旧 settings 文件（无 enterprise_first 键）读作关；
  · 分层矩阵：ON 全落企业 / 企业不可用（停用/冷却/熔断/模型冷却）个人兜底 + 恢复 /
    一次请求内 exclude 轮换 / 降权两轮 / OFF 对照（与纯轮转逐位一致）/ 亲和优先；
  · 加权数学：窗口 7、5 天 vs 60 天 -> 权重 3:1、近到期号占比 > 60%；窗口 0 -> 均匀；
  · top_expiry_days 边界：正常 / 无 credits / 缺键 / 占位 / 非法 / 毫秒未归一 / 已过期。

夹具纪律（tests/README.md）：零出网（本套件只调选号与设置读写的纯函数）；
ACCOUNTS_DIR / QD_PROXY_USAGE_DIR 指向 tempdir。
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_TMP = tempfile.mkdtemp(prefix="qd-ent-first-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_TMP, "usage")
os.environ["QD_NATIVE_IDENTITY"] = "0"

from _suite_head import *          # noqa: E402,F401,F403
P, A = bootstrap()
import qoder_settings as S         # noqa: E402

declare_suite(__file__, ["qoder_accounts.py", "qoder_settings.py",
                         "qoder_proxy.py"])
print("[enterprise-first] R1/R2/R4：企业优先分层 + 设置 + 企业层内临期加权")

_POOL_SEQ = [0]


def _acc(uid, enterprise=False, credits=None, realm="cn"):
    data = {"uid": uid, "realm": realm, "accessToken": "dt-" + uid,
            "refreshToken": "drt-" + uid, "expiresAt": 9999999999,
            "enterpriseDomain": "acme" if enterprise else ""}
    if credits is not None:
        data["credits"] = credits
    account = A.Account(data)
    account.enabled = True
    return account


def _pool(accounts, name=""):
    _POOL_SEQ[0] += 1
    pool = A.AccountPool(os.path.join(_TMP, "pool-%d-%s" % (_POOL_SEQ[0], name)))
    for account in accounts:
        pool.add(account)
    return pool


def _limits(ef_global=0, ef_cn=None, ef_intl=None, window=0):
    return {
        "enterprise_first": {"global": ef_global, "intl": ef_intl,
                             "cn": ef_cn},
        "expiring_window_days": {"global": window, "intl": None, "cn": None},
    }


def _skew(counts):
    return max(counts.values()) - min(counts.values())


# ---------------------------------------------------------------------------
# ① 设置往返 + realm 覆盖 + 旧 settings 文件
# ---------------------------------------------------------------------------
_SDIR = os.path.join(_TMP, "settings")
entry = S.set_limit(_SDIR, "enterprise_first", "global", 1)
ld1 = S.limits_data(_SDIR)
check("① set_limit(global,1) -> 形状完整 {'global':1,'intl':None,'cn':None}",
      entry == {"global": 1, "intl": None, "cn": None}
      and ld1.get("enterprise_first") == {"global": 1, "intl": None, "cn": None},
      ld1.get("enterprise_first"))
pool = _pool([_acc("rt-e1", True), _acc("rt-p1")], "rt")
pool.apply_settings(limits_data=ld1)
check("① apply_settings -> 池开关 ON（global=1，cn/intl 都继承）",
      pool.enterprise_first_enabled("cn")
      and pool.enterprise_first_enabled("intl"))

S.set_limit(_SDIR, "enterprise_first", "global", 0)
S.set_limit(_SDIR, "enterprise_first", "cn", 1)
pool.apply_settings(limits_data=S.limits_data(_SDIR))
check("① realm 覆盖：cn=1 + global=0 -> 仅 cn 开、intl 关",
      pool.enterprise_first_enabled("cn")
      and not pool.enterprise_first_enabled("intl"))

S.save(_SDIR, {"limits": {"reserve_credits": {"global": 500, "intl": None,
                                              "cn": None}}})
ld_old = S.limits_data(_SDIR)
pool.apply_settings(limits_data=ld_old)
check("① 旧 settings 文件（无 enterprise_first 键）-> 补全形状 0 + 池开关关",
      ld_old.get("enterprise_first") == {"global": 0, "intl": None, "cn": None}
      and not pool.enterprise_first_enabled("cn")
      and not pool.enterprise_first_enabled("intl"))

# ---------------------------------------------------------------------------
# ② 分层矩阵
# ---------------------------------------------------------------------------
# A. ON + 企业就绪 -> 全落企业号、计数均匀（窗口 0）
pool = _pool([_acc("tier-e1", True), _acc("tier-e2", True),
              _acc("tier-p1")], "a")
pool.apply_settings(limits_data=_limits(ef_global=1))
uids = [pool.pick(realm="cn", model="m").uid for _ in range(100)]
counts = {u: uids.count(u) for u in ("tier-e1", "tier-e2")}
check("② ON + 企业就绪 -> 100 次 pick 全落企业号且计数偏差 <= 1",
      set(uids) == {"tier-e1", "tier-e2"} and _skew(counts) <= 1, counts)

# B. ON + 企业号不可用（停用/冷却/熔断/模型冷却）-> 个人兜底；恢复后回企业层
_MUTES = (
    ("停用", lambda a: setattr(a, "enabled", False)),
    ("账号冷却", lambda a: setattr(a, "cooldown_until", time.time() + 60)),
    ("熔断", lambda a: setattr(a, "breaker_until", time.time() + 60)),
    ("模型冷却", lambda a: a.model_cooldowns.__setitem__("m", time.time() + 60)),
)
for kind, mute in _MUTES:
    pool = _pool([_acc("fb-e1", True), _acc("fb-p1")], "b")
    pool.apply_settings(limits_data=_limits(ef_global=1))
    ent, pub = pool.accounts[0], pool.accounts[1]
    mute(ent)
    picked = {pool.pick(realm="cn", model="m").uid for _ in range(10)}
    check("② ON + 企业号「%s」不可用 -> 10 次 pick 全落个人号（兜底）" % kind,
          picked == {pub.uid}, picked)
    if kind == "停用":       # 恢复：企业号重新就绪 -> 新选号回到企业层
        ent.enabled = True
        check("② 企业号恢复就绪 -> 新会话回到企业层",
              pool.pick(realm="cn", model="m").uid == ent.uid)

# C. ON + 一次请求内 exclude（失败轮换）：不重选已试号，全试完个人兜底/None
pool = _pool([_acc("ex-e1", True), _acc("ex-e2", True), _acc("ex-p1")], "c")
pool.apply_settings(limits_data=_limits(ef_global=1))
e1, e2, p1 = pool.accounts
try_first = pool.pick(realm="cn", model="m", exclude={e1.uid})
both_ent_excluded = pool.pick(realm="cn", model="m",
                              exclude={e1.uid, e2.uid})
all_excluded = pool.pick(realm="cn", model="m",
                         exclude={e1.uid, e2.uid, p1.uid})
check("② exclude 轮换：排除企业号1 -> 企业号2；两企业号都排除 -> 个人；"
      "全排除 -> None（不重选已试号）",
      try_first.uid == e2.uid and both_ent_excluded.uid == p1.uid
      and all_excluded is None,
      (try_first.uid, both_ent_excluded.uid, all_excluded))

# D. ON + 企业号处于降权窗口：先选未降权企业号；全降权才用降权企业号（不落个人）
pool = _pool([_acc("dg-e1", True), _acc("dg-e2", True), _acc("dg-p1")], "d")
pool.apply_settings(limits_data=_limits(ef_global=1))
e1, e2, p1 = pool.accounts
e1.degrade_until = time.time() + 60
picked = [pool.pick(realm="cn", model="m").uid for _ in range(20)]
check("② 降权两轮：企业号1 降权 -> 20 次全选未降权的企业号2（不落个人号）",
      set(picked) == {e2.uid}, sorted(set(picked)))
e2.degrade_until = time.time() + 60
picked = [pool.pick(realm="cn", model="m").uid for _ in range(20)]
check("② 全降权不楔死：两个企业号都降权 -> 仍用企业号（两者都出现）、不落个人号",
      set(picked) == {e1.uid, e2.uid}, sorted(set(picked)))

# E. OFF（默认）：混合池与旧纯轮转逐位一致
pool = _pool([_acc("off-e1", True), _acc("off-p1"), _acc("off-e2", True),
              _acc("off-p2")], "e")
seq = [pool.pick_for_session(realm="cn", session_key="off-%d" % i,
                             model="m").uid for i in range(8)]
check("② OFF（默认）：pick_for_session 与旧纯轮转逐位一致（插入序循环 2 轮）",
      seq == ["off-e1", "off-p1", "off-e2", "off-p2"] * 2, seq)

# F. 亲和优先：绑定号就绪即复用（存量会话不因开启分层被抢占）
pool = _pool([_acc("af-p1"), _acc("af-e1", True)], "f")
first = pool.pick_for_session(realm="cn", session_key="keep", model="m")
second = pool.pick_for_session(realm="cn", session_key="keep", model="m")
check("② 亲和：同 session_key 两次 pick_for_session 复用同一账号",
      first.uid == second.uid == "af-p1", (first.uid, second.uid))
pool.apply_settings(limits_data=_limits(ef_global=1))
third = pool.pick_for_session(realm="cn", session_key="keep", model="m")
fourth = pool.pick_for_session(realm="cn", session_key="fresh", model="m")
check("② 亲和优先于分层：ON 后存量会话（已绑定公共号）继续复用、不被抢占",
      third.uid == "af-p1", third.uid)
check("② 新会话（新 key）回到企业层", fourth.uid == "af-e1", fourth.uid)

# ---------------------------------------------------------------------------
# ③ 企业层内临期加权（R4）
# ---------------------------------------------------------------------------
_NOW = time.time()
near = _acc("wt-near", True, credits={"expires_at": _NOW + 5 * 86400})
far = _acc("wt-far", True, credits={"expires_at": _NOW + 60 * 86400})
pool = _pool([near, far], "w")
pool.apply_settings(limits_data=_limits(ef_global=1, window=7))
check("③ 权重公式（顶层 expires_at）：窗口 7、5 天 -> 3；60 天 -> 1",
      (pool._enterprise_expiry_weight(near),
       pool._enterprise_expiry_weight(far)) == (3, 1),
      (pool._enterprise_expiry_weight(near),
       pool._enterprise_expiry_weight(far)))
uids = [pool.pick(realm="cn", model="m").uid for _ in range(200)]
near_count = uids.count("wt-near")
check("③ 加权数学：窗口 7、5 天 vs 60 天 -> 近到期号占比 > 60%（smooth WRR 3:1）",
      near_count > 120, near_count)

near0 = _acc("w0-near", True, credits={"expires_at": time.time() + 5 * 86400})
far0 = _acc("w0-far", True, credits={"expires_at": time.time() + 60 * 86400})
pool = _pool([near0, far0], "w0")
pool.apply_settings(limits_data=_limits(ef_global=1, window=0))
counts = {u: 0 for u in ("w0-near", "w0-far")}
for _ in range(200):
    counts[pool.pick(realm="cn", model="m").uid] += 1
check("③ 窗口 0 -> 等权（权重皆 1）：企业层纯轮转、计数偏差 <= 1",
      _skew(counts) <= 1, counts)

# ---------------------------------------------------------------------------
# ④ top_expiry_days 边界 + 企业权重对占位/无数据的兜底
# ---------------------------------------------------------------------------
_BASE = time.time()
a_norm = _acc("ted-norm", True, credits={"expires_at": _BASE + 5 * 86400})
check("④ 正常值（+5 天）≈ 5", 4.9 < a_norm.top_expiry_days(now=_BASE) < 5.1,
      a_norm.top_expiry_days(now=_BASE))
a_none = _acc("ted-none", True)                       # credits 缺省为 None
check("④ 无 credits -> None", a_none.top_expiry_days() is None)
a_miss = _acc("ted-miss", True, credits={})           # 缺 expires_at 键
check("④ 缺键 -> None", a_miss.top_expiry_days() is None)
a_hold = _acc("ted-hold", True, credits={"expires_at": 253402214400})
# 253402214400 是 CN 占位的**毫秒原始值**：按 >1e11 判据归一为 253402214
# （= fetch_credits 实际落库形态，见 _test_credit_expiry.py E1）-> days 为负，
# 由权重函数按"已过期"归 1 —— 不进加权、也不会被顶格（双保险之一）。
check("④ 占位 253402214400（毫秒原始值）-> 归一为落库形态、days 为负",
      (a_hold.top_expiry_days(now=_BASE) or 0) < 0,
      a_hold.top_expiry_days(now=_BASE))
a_hold_norm = _acc("ted-hold-norm", True, credits={"expires_at": 253402214})
check("④ 占位落库形态 253402214 -> days 为负（同上，不进加权）",
      (a_hold_norm.top_expiry_days(now=_BASE) or 0) < 0,
      a_hold_norm.top_expiry_days(now=_BASE))
a_hold_ms = _acc("ted-hold-ms", True, credits={"expires_at": 253402214400000})
check("④ 占位毫秒版 253402214400000 -> 归一后 == _EXPIRY_NEVER_EPOCH -> None",
      a_hold_ms.top_expiry_days() is None)
a_bad = _acc("ted-bad", True, credits={"expires_at": "not-a-number"})
check("④ 非法串 -> None", a_bad.top_expiry_days() is None)
a_ms = _acc("ted-ms", True, credits={"expires_at": 1792512000000})
ms_days = a_ms.top_expiry_days(now=_BASE)
check("④ 毫秒未归一 1792512000000 -> 防御性 /1000（≈ 与归一秒值一致）",
      abs(ms_days - (1792512000.0 - _BASE) / 86400.0) < 0.01, ms_days)
a_past = _acc("ted-past", True, credits={"expires_at": _BASE - 86400})
check("④ 已过期 -> 如实返回负数",
      -1.1 < a_past.top_expiry_days(now=_BASE) < -0.9,
      a_past.top_expiry_days(now=_BASE))
pool = _pool([a_hold, a_hold_ms, a_none, a_norm], "guards")
pool.apply_settings(limits_data=_limits(ef_global=1, window=7))
check("④ 企业权重兜底：占位（毫秒原始/毫秒版）/无数据 -> 1（不进加权）",
      pool._enterprise_expiry_weight(a_hold) == 1
      and pool._enterprise_expiry_weight(a_hold_ms) == 1
      and pool._enterprise_expiry_weight(a_none) == 1,
      (pool._enterprise_expiry_weight(a_hold),
       pool._enterprise_expiry_weight(a_hold_ms),
       pool._enterprise_expiry_weight(a_none)))

shutil.rmtree(_TMP, ignore_errors=True)
finish()

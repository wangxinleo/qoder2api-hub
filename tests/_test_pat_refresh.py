"""企业 PAT 刷新缺陷回归。

    python tests/_test_pat_refresh.py

覆盖：
  · jrt- 401 TOKEN_EXPIRE 后 PAT 兜底成功 -> 不停用、token/有效期推进
  · 双失败 -> 停用；drt- 无兜底失败 -> 停用
  · expires_in 缺失 -> 24h；存在 -> 按响应
  · run_keepalive 自愈旧版本停用的 PAT 账号（并落盘）
  · open_upstream 401 登录错误 -> refresh + 同账号重试
  · 队列 10605 / 额度 112 不触发 refresh
  · 信封 TOKEN_EXPIRE -> 刷新成功不冷却/不停用
  · 10s 自愈门限
"""
import io
import json
import os
import sys
import tempfile
import time
import types
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: E402,F401,F403

P, A = bootstrap()
import qoder_tasks as T            # noqa: E402

declare_suite(__file__, ["qoder_accounts.py", "qoder_tasks.py", "qoder_proxy.py"])

TMP = tempfile.mkdtemp(prefix="qd-pat-refresh-")
QUEUE_BODY = ('{"code":"403","message":"{\\"code\\":\\"10605\\",\\"message\\":'
              '\\"{\\\\\\"isQueued\\\\\\":true,\\\\\\"retryAfterSeconds\\\\\\":30}\\"}"}')
DEAD_BODY = '{"code":"TOKEN_EXPIRE","message":"login required"}'

_ORIG_HTTP = A.http_json
_ORIG_VALIDATE = P.validate_public_http_url


def _http_error(code, body):
    return urllib.error.HTTPError(
        "https://u.invalid", code, "err", {},
        io.BytesIO((body or "").encode("utf-8")))


def _route(http_calls, routes):
    """routes: suffix -> dict | Exception 实例。记录每次 http_json 调用。"""
    def _fake(url, data=None, method="GET", headers=None, timeout=None,
              retries=None, **kw):
        http_calls.append(url)
        for suffix, val in routes.items():
            if url.endswith(suffix):
                if isinstance(val, urllib.error.HTTPError):
                    # 真实 urlopen 每次产生新异常；复用同一实例会让第二次
                    # read() 拿到空 body（双失败场景会被误判成非 session-dead）。
                    try:
                        body = val.fp.getvalue()
                    except Exception:
                        body = b""
                    raise urllib.error.HTTPError(
                        val.url, val.code, val.msg, val.hdrs,
                        io.BytesIO(body))
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError("unexpected http_json outbound: %s" % url)
    return _fake


def _acc(uid, **extra):
    d = {"uid": uid, "realm": "cn", "enterpriseDomain": "acme",
         "accessToken": "jt-old", "source": "pat", "enabled": True}
    d.update(extra)
    return A.Account(d)


class _Resp(object):
    def close(self):
        pass

    def read(self, n=None):
        return b""


class _Sess(object):
    def headers(self, *a, **k):
        return {"Content-Type": "application/json"}


def _run_open(pool, urlopen_impl, http_impl):
    """在受控 urllib/SESSIONS/time/POOL 下跑一次 open_upstream。"""
    real = (P.urllib, P.SESSIONS, P.time, P.POOL, A.http_json,
            P.validate_public_http_url)
    ft = types.SimpleNamespace(**dict((k, getattr(real[2], k))
                                      for k in dir(real[2])
                                      if not k.startswith("_")))
    ft.sleep = lambda s: None
    P.urllib = types.SimpleNamespace(
        request=types.SimpleNamespace(Request=real[0].request.Request,
                                      urlopen=urlopen_impl),
        error=real[0].error, parse=real[0].parse)
    P.SESSIONS = types.SimpleNamespace(get=lambda a: _Sess())
    P.time = ft
    P.POOL = pool
    A.http_json = http_impl
    P.validate_public_http_url = lambda url, allow_local=False: url
    try:
        return P.open_upstream(
            {"model": "DeepSeek-Flash",
             "messages": [{"role": "user", "content": "hi"}]},
            session_key="pat-refresh", target_realm="cn")
    finally:
        (P.urllib, P.SESSIONS, P.time, P.POOL, A.http_json,
         P.validate_public_http_url) = real


def _pool_with(acc):
    pool = A.AccountPool(os.path.join(TMP, "pool-" + acc.uid),
                         log=lambda *a, **k: None)
    pool.add(acc)
    return pool


# ---------------------------------------------------------------- 1/3 fallback
calls = []
routes = {A.PATH_JOB_REFRESH: _http_error(401, DEAD_BODY),
          A.PATH_JOB_EXCHANGE: {"token": "jt-new", "refresh_token": "jrt-new"}}
A.http_json = _route(calls, routes)
ent = _acc("r1", refreshToken="jrt-dead", personalToken="pt-live",
           expiresAt=int(time.time()) - 3600)
t0 = time.time()
ok1 = ent.refresh()
check("jrt- 401 TOKEN_EXPIRE + PAT 成功：refresh True 且不停用",
      ok1 is True and ent.enabled is True, (ok1, ent.enabled, ent.last_error))
check("PAT 兜底写入新 token/refresh",
      ent.access_token == "jt-new" and ent.refresh_token == "jrt-new",
      (ent.access_token, ent.refresh_token))
check("无 expires_in 时有效期按 24h 推进",
      abs(ent.expires_at - (t0 + 24 * 3600)) <= 10,
      ent.expires_at - t0)
check("兜底成功清空 last_error / cooldown",
      ent.last_error == "" and ent.cooldown_until == 0,
      (ent.last_error, ent.cooldown_until))
check("兜底链路共 2 次出站（refresh + exchange）且均为企业 host",
      len(calls) == 2 and all("acme-openapi.vpc.qoder.com.cn" in u for u in calls),
      calls)

# expires_in 存在时按响应推进
calls[:] = []
routes = {A.PATH_JOB_REFRESH: _http_error(401, DEAD_BODY),
          A.PATH_JOB_EXCHANGE: {"token": "jt-new2", "refresh_token": "jrt-new2",
                                "expires_in": 3600000}}
A.http_json = _route(calls, routes)
ent2 = _acc("r2", refreshToken="jrt-dead", personalToken="pt-live",
            expiresAt=int(time.time()) - 3600)
t0 = time.time()
ok2 = ent2.refresh()
check("expires_in 存在时按响应推进（1h）",
      ok2 is True and abs(ent2.expires_at - (t0 + 3600)) <= 10,
      ent2.expires_at - t0)

# ---------------------------------------------------------------- 2/4 dead
calls[:] = []
routes = {A.PATH_JOB_REFRESH: _http_error(401, DEAD_BODY),
          A.PATH_JOB_EXCHANGE: _http_error(401, DEAD_BODY)}
A.http_json = _route(calls, routes)
ent3 = _acc("r3", refreshToken="jrt-dead", personalToken="pt-dead",
            expiresAt=int(time.time()) - 3600)
ok3 = ent3.refresh()
check("双刷新都 session dead -> 停用",
      ok3 is False and ent3.enabled is False,
      (ok3, ent3.enabled))
check("双失败的 last_error 含 session dead",
      "session dead" in ent3.last_error, ent3.last_error)

# 刷新触发的停用必须落盘（旧实现只在内存里 disable）
pool_p = A.AccountPool(os.path.join(TMP, "persist"), log=lambda *a, **k: None)
pers = _acc("r-persist", refreshToken="jrt-dead", personalToken="pt-dead",
            expiresAt=int(time.time()) - 60)
pool_p.add(pers)
okp = pers.refresh()
disk_p = json.load(open(pers.path, encoding="utf-8"))
check("刷新最终失败：内存 disabled 且标记为 session-dead",
      okp is False and pers.enabled is False
      and pers.disabled_by_session_dead is True,
      (okp, pers.enabled, pers.disabled_by_session_dead))
check("刷新最终失败：enabled/标记已落盘",
      disk_p.get("enabled") is False
      and disk_p.get("disabledBySessionDead") is True,
      (disk_p.get("enabled"), disk_p.get("disabledBySessionDead")))

calls[:] = []
routes = {A.PATH_DEVICE_REFRESH: _http_error(401, DEAD_BODY)}
A.http_json = _route(calls, routes)
dev = A.Account({"uid": "rdev", "realm": "cn", "accessToken": "dt-old",
                 "refreshToken": "drt-dead", "enabled": True})
ok4 = dev.refresh()
check("drt- device 401 TOKEN_EXPIRE 无 PAT 时仍停用",
      ok4 is False and dev.enabled is False, (ok4, dev.enabled))

calls[:] = []
routes = {A.PATH_DEVICE_REFRESH: _http_error(401, DEAD_BODY),
          A.PATH_JOB_EXCHANGE: {"token": "jt-dev", "refresh_token": "jrt-dev",
                                "expires_in": 3600000}}
A.http_json = _route(calls, routes)
dev_pat = A.Account({"uid": "rdev-pat", "realm": "cn",
                     "accessToken": "dt-old", "refreshToken": "drt-dead",
                     "personalToken": "pt-live", "enabled": True})
ok4b = dev_pat.refresh()
check("drt- 失败 + PAT 兜底成功 -> 不停用",
      ok4b is True and dev_pat.enabled is True
      and dev_pat.access_token == "jt-dev",
      (ok4b, dev_pat.enabled, dev_pat.access_token))
check("drt- + PAT 兜底走 device 后 exchange（2 次出站）",
      len(calls) == 2 and calls[0].endswith(A.PATH_DEVICE_REFRESH)
      and calls[1].endswith(A.PATH_JOB_EXCHANGE), calls)

# ---------------------------------------------------------------- 5 keepalive
calls[:] = []
routes = {A.PATH_JOB_REFRESH: _http_error(401, DEAD_BODY),
          A.PATH_JOB_EXCHANGE: {"token": "jt-recover", "refresh_token": "jrt-ok",
                                "expires_in": 3600000}}
A.http_json = _route(calls, routes)
old = A.Account({"uid": "r-old", "realm": "cn", "accessToken": "jt-old",
                 "refreshToken": "jrt-dead", "personalToken": "pt-live",
                 "enabled": False, "source": "pat",
                 "lastError": "session dead (TOKEN_EXPIRE): re-login required",
                 "expiresAt": int(time.time()) - 86400})
pool = A.AccountPool(os.path.join(TMP, "recover"), log=lambda *a, **k: None)
pool.add(old)
real_time = T.time
ft = types.SimpleNamespace(**dict((k, getattr(real_time, k))
                                  for k in dir(real_time)
                                  if not k.startswith("_")))
ft.sleep = lambda s: None
T.time = ft
try:
    ka = T.run_keepalive(pool, force=False, threshold_seconds=4 * 3600)
finally:
    T.time = real_time
check("旧停用 PAT 账号被保活恢复（enabled=True）",
      old.enabled is True and ka.get("refreshed") == 1,
      (old.enabled, ka))
check("恢复成功后落盘 enabled=true",
      json.load(open(old.path, encoding="utf-8")).get("enabled") is True,
      old.path)
check("恢复成功后清除 disabledBySessionDead 标记",
      json.load(open(old.path, encoding="utf-8")).get(
          "disabledBySessionDead") is False, old.path)
check("恢复日志明确可见",
      any("已自动恢复" in ln for ln in ka.get("logs") or []), ka.get("logs"))

# 手动停用（无 session-dead 标记）不自动恢复
manual = A.Account({"uid": "r-manual", "realm": "cn", "accessToken": "jt-x",
                    "personalToken": "pt-x", "enabled": False,
                    "source": "pat", "lastError": "manually disabled"})
calls[:] = []
T_http = A.http_json
T.time = ft
try:
    ka2 = T.run_keepalive(_pool_with(manual), force=False)
finally:
    T.time = real_time
    A.http_json = T_http
check("手动停用账号不被自愈",
      manual.enabled is False and ka2.get("refreshed") == 0, ka2)

# 旧缺陷真实落盘态：PAT 兜底成功时清空了 lastError，只剩 enabled=false
calls[:] = []
routes = {A.PATH_JOB_REFRESH: _http_error(401, DEAD_BODY),
          A.PATH_JOB_EXCHANGE: {"token": "jt-legacy",
                                "refresh_token": "jrt-legacy",
                                "expires_in": 3600000}}
A.http_json = _route(calls, routes)
legacy = _acc("r-legacy", refreshToken="jrt-dead",
              personalToken="pt-live", enabled=False, lastError="",
              expiresAt=int(time.time()) + 3600)
pool_l = A.AccountPool(os.path.join(TMP, "recover-legacy"),
                       log=lambda *a, **k: None)
pool_l.add(legacy)
T.time = ft
try:
    ka_l = T.run_keepalive(pool_l, force=False)
finally:
    T.time = real_time
check("旧版真实停用态（enabled=false + lastError 空）可自愈",
      legacy.enabled is True and ka_l.get("refreshed") == 1,
      (legacy.enabled, ka_l))

# 手动停用标记必须压过 session-dead 文本（disabledByUser 持久化）
calls[:] = []
A.http_json = _route(calls, {})
manual2 = _acc("r-manual2", refreshToken="jrt-dead",
               personalToken="pt-live",
               lastError="session dead (TOKEN_EXPIRE): re-login required")
pool_m2 = A.AccountPool(os.path.join(TMP, "manual2"),
                        log=lambda *a, **k: None)
pool_m2.add(manual2)
pool_m2.set_enabled("r-manual2", False)
T.time = ft
try:
    ka_m2 = T.run_keepalive(pool_m2, force=False)
finally:
    T.time = real_time
check("手动停用（disabledByUser）不被 session-dead 文本重新拉起",
      manual2.enabled is False and ka_m2.get("refreshed") == 0
      and calls == [], (manual2.enabled, ka_m2, calls))

# 旧账号首次恢复遇网络失败：标记需持久化，lastError 被覆盖后仍能重试
calls[:] = []
A.http_json = _route(calls, {
    A.PATH_JOB_REFRESH: RuntimeError("network down"),
    A.PATH_JOB_EXCHANGE: RuntimeError("network down")})
trans = A.Account({"uid": "r-trans", "realm": "cn", "accessToken": "jt-old",
                   "refreshToken": "jrt-dead", "personalToken": "pt-live",
                   "enabled": False, "source": "pat",
                   "lastError": "session dead (TOKEN_EXPIRE): re-login required"})
pool_t = A.AccountPool(os.path.join(TMP, "recover-trans"),
                       log=lambda *a, **k: None)
pool_t.add(trans)
T.time = ft
try:
    ka_t1 = T.run_keepalive(pool_t, force=False)
    en1 = trans.enabled
    disk_t = json.load(open(trans.path, encoding="utf-8"))
    routes = {A.PATH_JOB_REFRESH: _http_error(401, DEAD_BODY),
              A.PATH_JOB_EXCHANGE: {"token": "jt-back",
                                    "refresh_token": "jrt-back",
                                    "expires_in": 3600000}}
    A.http_json = _route(calls, routes)
    ka_t2 = T.run_keepalive(pool_t, force=False)
finally:
    T.time = real_time
check("恢复遇网络失败：仍 disabled 且标记已落盘",
      en1 is False and disk_t.get("disabledBySessionDead") is True,
      (en1, disk_t.get("disabledBySessionDead")))
check("网络恢复后第二次保活可继续自愈",
      trans.enabled is True and ka_t2.get("refreshed") == 1,
      (trans.enabled, ka_t2))

# ------------------------------------------------------- 6 open_upstream 401
calls[:] = []
routes = {A.PATH_JOB_REFRESH: _http_error(401, DEAD_BODY),
          A.PATH_JOB_EXCHANGE: {"token": "jt-retry", "refresh_token": "jrt-ok",
                                "expires_in": 3600000}}
http_impl = _route(calls, routes)
ent6 = _acc("r6", refreshToken="jrt-dead", personalToken="pt-live",
            expiresAt=0)
open_hits = []


def _auth_then_ok(req, timeout=None, *a, **k):
    open_hits.append(getattr(req, "full_url", str(req)))
    if len(open_hits) == 1:
        raise _http_error(401, DEAD_BODY)
    return _Resp()


resp6, acc6, _ = _run_open(_pool_with(ent6), _auth_then_ok, http_impl)
check("HTTP 401 -> refresh -> 同账号重试成功（2 次 urlopen）",
      resp6 is not None and acc6 is ent6 and len(open_hits) == 2, open_hits)
check("重试使用刷新后的 token 且账号未冷却/未停用",
      ent6.access_token == "jt-retry" and ent6.enabled is True
      and ent6.cooldown_until == 0,
      (ent6.access_token, ent6.enabled, ent6.cooldown_until))
check("401 自愈只做一次 refresh（2 次出站）", len(calls) == 2, calls)

# 双失败 -> 停用
calls[:] = []
routes = {A.PATH_JOB_REFRESH: _http_error(401, DEAD_BODY),
          A.PATH_JOB_EXCHANGE: _http_error(401, DEAD_BODY)}
http_impl = _route(calls, routes)
ent7 = _acc("r7", refreshToken="jrt-dead", personalToken="pt-dead",
            expiresAt=0)
open_hits[:] = []
err7 = None
try:
    _run_open(_pool_with(ent7), lambda req, timeout=None, *a, **k: (
        open_hits.append(1) or (_ for _ in ()).throw(_http_error(401, DEAD_BODY))),
        http_impl)
except Exception as exc:
    err7 = exc
check("401 双失败 -> 账号停用且上抛原错误",
      ent7.enabled is False and isinstance(err7, urllib.error.HTTPError),
      (ent7.enabled, type(err7).__name__ if err7 else None))
check("双失败只重试一次请求（1 次 urlopen）", len(open_hits) == 1, open_hits)

# 无凭证账号的 401 不得吃掉请求级刷新预算
calls[:] = []
routes = {A.PATH_JOB_REFRESH: _http_error(401, DEAD_BODY),
          A.PATH_JOB_EXCHANGE: {"token": "jt-budget",
                                "refresh_token": "jrt-budget",
                                "expires_in": 3600000}}
http_impl = _route(calls, routes)
acc_a = A.Account({"uid": "r-budget-a", "realm": "cn",
                   "accessToken": "jt-a", "enabled": True, "expiresAt": 0})
acc_b = A.Account({"uid": "r-budget-b", "realm": "cn",
                   "accessToken": "jt-b", "refreshToken": "jrt-dead",
                   "personalToken": "pt-live", "enabled": True,
                   "expiresAt": 0})
pool_b = A.AccountPool(os.path.join(TMP, "budget"),
                       log=lambda *a, **k: None)
pool_b.accounts = [acc_a, acc_b]
pool_b._cursor = 0
open_hits[:] = []


def _two_401_then_ok(req, timeout=None, *a, **k):
    open_hits.append(1)
    if len(open_hits) <= 2:
        raise _http_error(401, DEAD_BODY)
    return _Resp()


resp_b, acc_b2, _ = _run_open(pool_b, _two_401_then_ok, http_impl)
check("无凭证 401 不吃预算：后续 PAT 账号仍能刷新并成功",
      acc_b2 is acc_b and acc_b.access_token == "jt-budget"
      and len(open_hits) == 3,
      (acc_b2 is acc_b, acc_b.access_token, len(open_hits)))
check("无凭证 TOKEN_EXPIRE 账号仍按原语义停用",
      acc_a.enabled is False, acc_a.enabled)

# ------------------------------------------------------- 8 queue/quota no refresh
calls[:] = []
http_impl = _route(calls, {})       # 任何 http_json 调用都会失败
ent8 = _acc("r8", refreshToken="jrt-live", personalToken="pt-live",
            expiresAt=0)
err8 = None
try:
    _run_open(_pool_with(ent8),
              lambda req, timeout=None, *a, **k: (
                  (_ for _ in ()).throw(_http_error(403, QUEUE_BODY))),
              http_impl)
except Exception as exc:
    err8 = exc
check("队列 403 不触发 refresh（零 http_json 出站）",
      isinstance(err8, P.UpstreamQueued) and calls == [],
      (type(err8).__name__ if err8 else None, calls))
check("队列 403 不停用账号", ent8.enabled is True, ent8.enabled)

calls[:] = []
http_impl = _route(calls, {})
ent9 = _acc("r9", refreshToken="jrt-live", personalToken="pt-live",
            expiresAt=0)
err9 = None
try:
    _run_open(_pool_with(ent9),
              lambda req, timeout=None, *a, **k: (
                  (_ for _ in ()).throw(_http_error(
                      403, '{"code":"112","message":"quota exhausted"}'))),
              http_impl)
except Exception as exc:
    err9 = exc
check("额度 403（112）不触发 refresh", calls == [], calls)
check("额度 403 仍走长冷却",
      ent9.cooldown_until > time.time(), ent9.cooldown_until)

# ------------------------------------------------------- 9 envelope self-heal
calls[:] = []
routes = {A.PATH_JOB_REFRESH: _http_error(401, DEAD_BODY),
          A.PATH_JOB_EXCHANGE: {"token": "jt-env", "refresh_token": "jrt-env",
                                "expires_in": 3600000}}
A.http_json = _route(calls, routes)
ent10 = _acc("r10", refreshToken="jrt-dead", personalToken="pt-live",
             expiresAt=int(time.time()) - 60)
P._handle_envelope_account_cooldown(
    ent10, P.UpstreamStatus(403, DEAD_BODY), model="DeepSeek-Flash")
check("信封 TOKEN_EXPIRE -> 刷新成功，不冷却/不停用",
      ent10.enabled is True and ent10.cooldown_until == 0
      and ent10.access_token == "jt-env",
      (ent10.enabled, ent10.cooldown_until, ent10.access_token))

calls[:] = []
routes = {A.PATH_JOB_REFRESH: _http_error(401, DEAD_BODY),
          A.PATH_JOB_EXCHANGE: {"token": "jt-env401",
                                "refresh_token": "jrt-env401",
                                "expires_in": 3600000}}
A.http_json = _route(calls, routes)
ent11 = _acc("r11", refreshToken="jrt-dead", personalToken="pt-live",
             expiresAt=int(time.time()) - 60)
P._handle_envelope_account_cooldown(
    ent11, P.UpstreamStatus(401, ""), model="DeepSeek-Flash")
check("信封 401（空 body）也尝试刷新，不冷却/不停用",
      ent11.enabled is True and ent11.cooldown_until == 0
      and ent11.access_token == "jt-env401",
      (ent11.enabled, ent11.cooldown_until, ent11.access_token))

# ------------------------------------------------------- 10 10s recovery gate
calls[:] = []
gate = A.Account({"uid": "r-gate", "realm": "cn", "accessToken": "jt-x",
                  "personalToken": "pt-x", "enabled": True})
refresh_hits = []


def _fake_refresh():
    refresh_hits.append(1)
    return True


gate.refresh = _fake_refresh
ok_a = P._recover_account_token(gate, "gate-1")
ok_b = P._recover_account_token(gate, "gate-2")
check("10s 门限：连续恢复只刷新一次",
      ok_a is True and ok_b is False and len(refresh_hits) == 1,
      (ok_a, ok_b, refresh_hits))

# ---------------------------------------------------------------- 收尾
A.http_json = _ORIG_HTTP
P.validate_public_http_url = _ORIG_VALIDATE
finish()

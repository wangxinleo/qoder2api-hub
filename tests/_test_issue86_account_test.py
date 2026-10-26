"""#86 账号测试必须落在被点的那个账号上。"""
import io
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py", "dashboard.html"])

print("[#86] 源码守卫")
_src = io.open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
check("open_upstream 有 only_account 参数（#87 起还带 on_queue）",
      "usage_ctx=None,\n                  only_account=None, on_queue=None):" in _src)
check("选取账号时优先用钉死账号",
      "account = only_account if only_account.uid not in tried else None" in _src)
check("钉死时按单账号池处理（total=1）",
      "total = 1 if only_account is not None else" in _src)
check("账号测试路由传入 only_account",
      "only_account=account)" in _src)
check("成功路径清的是实际账号（acc）",
      "acc.clear_error()" in _src)

print("[#86] 行为：钉死账号不会被池替换")


class _Sess(object):
    def __init__(self, acc):
        self.acc = acc

    def headers(self, *a, **k):
        # 把 access_token 塞进头里，便于断言用的是哪个号
        return {"Content-Type": "application/json",
                "X-Test-Token": self.acc.access_token}


class _Resp(object):
    def read(self, n=None):
        return b"data: [DONE]\n\n"

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_tmp = tempfile.mkdtemp(prefix="i86-")
_pool = A.AccountPool(_tmp, log=lambda *a, **k: None)
_accA = A.Account({"uid": "acc-A", "realm": "cn", "accessToken": "tok-A"})
_accB = A.Account({"uid": "acc-B", "realm": "cn", "accessToken": "tok-B"})
for _a in (_accA, _accB):
    _a.enabled = True
    _a.expires_at = 0
    _a.credits = {"remain": 5000, "remain_effective": 5000}
    _pool.accounts.append(_a)

_seen = []
_real = (P.urllib, P.SESSIONS, P.POOL, P.time)


def _fake_urlopen(req, timeout=None):
    _seen.append(req.get_header("X-test-token") or req.headers.get("X-test-token"))
    return _Resp()


_ft = types.SimpleNamespace(**{k: getattr(_real[3], k) for k in dir(_real[3])
                               if not k.startswith("_")})
_ft.sleep = lambda s: None
P.urllib = types.SimpleNamespace(
    request=types.SimpleNamespace(Request=_real[0].request.Request,
                                  urlopen=_fake_urlopen),
    error=_real[0].error, parse=_real[0].parse)
P.SESSIONS = types.SimpleNamespace(get=lambda acc: _Sess(acc))
P.POOL = _pool
P.time = _ft
try:
    _payload = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
    _resp, _used, _ = P.open_upstream(_payload, target_realm="cn",
                                      only_account=_accB)
    try:
        _resp.close()
    except Exception:
        pass
finally:
    P.urllib, P.SESSIONS, P.POOL, P.time = _real
check("返回的账号就是被钉死的那个", _used.uid == "acc-B", _used.uid)
check("实际请求头用的是 B 的 token（没被池替换）",
      _seen and (_seen[0] or "").lower() == "tok-b", _seen)

print("[#86] 未钉死时仍走池（回归）")
_seen2 = []


def _fake_urlopen2(req, timeout=None):
    _seen2.append(req.get_header("X-test-token") or req.headers.get("X-test-token"))
    return _Resp()


_real2 = (P.urllib, P.SESSIONS, P.POOL, P.time)
P.urllib = types.SimpleNamespace(
    request=types.SimpleNamespace(Request=_real2[0].request.Request,
                                  urlopen=_fake_urlopen2),
    error=_real2[0].error, parse=_real2[0].parse)
P.SESSIONS = types.SimpleNamespace(get=lambda acc: _Sess(acc))
P.POOL = _pool
P.time = _ft
try:
    _r2, _u2, _ = P.open_upstream(_payload, target_realm="cn")
    try:
        _r2.close()
    except Exception:
        pass
finally:
    P.urllib, P.SESSIONS, P.POOL, P.time = _real2
check("不传 only_account 时仍由池挑选（返回池内账号）",
      _u2.uid in ("acc-A", "acc-B"), _u2.uid)

print("[#86] /accounts/test 排队不冷却 + 一次性亲和键")
QUEUE_DETAIL = ('{"code":"10605","isQueued":true,"retryAfterSeconds":30,'
                '"subCode":"RESOURCE_LIMIT"}')
_tmp_h = tempfile.mkdtemp(prefix="i86h-")
_pool_h = A.AccountPool(_tmp_h, log=lambda *a, **k: None)
_accQ = A.Account({"uid": "acc-Q", "realm": "cn", "accessToken": "tok-Q"})
_accQ.enabled = True
_accQ.expires_at = 0
_pool_h.accounts.append(_accQ)


def _mk_handler():
    h = object.__new__(P.Handler)
    out = []

    def _json(code, obj, extra_headers=None):
        out.append((int(code), obj, extra_headers or {}))
        return out[-1]

    def _error(code, message, err_type="server_error", retry_after=None):
        out.append((int(code), {"error": {"message": message,
                                          "type": err_type,
                                          "code": code}},
                    {"Retry-After": retry_after} if retry_after else {}))
        return out[-1]

    h._json = _json
    h._error = _error
    return h, out


def _with_handler(open_impl, aggregate_impl=None):
    real = (P.POOL, P.open_upstream, P.aggregate_stream)
    P.POOL = _pool_h
    P.open_upstream = open_impl
    if aggregate_impl is not None:
        P.aggregate_stream = aggregate_impl
    h, out = _mk_handler()
    return real, h, out


def _restore(real):
    P.POOL, P.open_upstream, P.aggregate_stream = real


# ---- HTTP 层排队：UpstreamQueued -> queued/503，不冷却
_seen = []
_accQ.note_success()
_before_cool = float(getattr(_accQ, "error_until", 0) or 0)
_before_le = getattr(_accQ, "last_error", "")


def _open_queued(payload, **kw):
    _seen.append(kw)
    raise P.UpstreamQueued(30, QUEUE_DETAIL)


_real_h = _with_handler(_open_queued)
try:
    _real_h[1]._handle_accounts("/accounts/test", {"uid": "acc-Q"})
    _out = _real_h[2][-1][1]
finally:
    _restore(_real_h[0])
check("HTTP 层排队：返回 queued=true / status=503 / retry_after=30",
      _out.get("queued") is True and _out.get("status") == 503
      and _out.get("retry_after") == 30, _out)
check("HTTP 层排队：uid 仍是被点的账号",
      _out.get("uid") == "acc-Q", _out.get("uid"))
check("HTTP 层排队：账号不冷却、不写 last_error",
      float(getattr(_accQ, "error_until", 0) or 0) <= _before_cool + 0.001
      and getattr(_accQ, "last_error", "") == _before_le,
      (getattr(_accQ, "error_until", 0), getattr(_accQ, "last_error", "")))
check("测试仍钉住被点账号",
      _seen and getattr(_seen[0].get("only_account"), "uid", "") == "acc-Q",
      _seen[:1])
check("测试使用一次性 session_key（acct-test-*，不是 pfx-*）",
      _seen and _seen[0].get("session_key", "").startswith("acct-test-")
      and not _seen[0].get("session_key", "").startswith("pfx-"),
      _seen[:1])

# ---- 信封层排队：UpstreamStatus(403, 10605) -> queued/503，不冷却
_seen2 = []


def _open_ok(payload, **kw):
    _seen2.append(kw)
    return _Resp(), _accQ, ""


def _aggregate_queue(*a, **k):
    raise P.UpstreamStatus(403, QUEUE_DETAIL)


_before_cool2 = float(getattr(_accQ, "error_until", 0) or 0)
_before_le2 = getattr(_accQ, "last_error", "")
_real_h2 = _with_handler(_open_ok, _aggregate_queue)
try:
    _real_h2[1]._handle_accounts("/accounts/test", {"uid": "acc-Q"})
    _out2 = _real_h2[2][-1][1]
finally:
    _restore(_real_h2[0])
check("信封层排队：返回 queued=true / status=503 / retry_after=30",
      _out2.get("queued") is True and _out2.get("status") == 503
      and _out2.get("retry_after") == 30, _out2)
check("信封层排队：账号不冷却、不写 last_error",
      float(getattr(_accQ, "error_until", 0) or 0) <= _before_cool2 + 0.001
      and getattr(_accQ, "last_error", "") == _before_le2,
      (getattr(_accQ, "error_until", 0), getattr(_accQ, "last_error", "")))
check("两次测试的 session_key 不同",
      len(_seen) == 1 and len(_seen2) == 1
      and _seen[0]["session_key"] != _seen2[0]["session_key"],
      (_seen[:1], _seen2[:1]))

# ---- 非排队信封错误：真实 status，不再 500
_accR = A.Account({"uid": "acc-R", "realm": "cn", "accessToken": "tok-R"})
_accR.enabled = True
_accR.expires_at = 0
_pool_h.accounts.append(_accR)


def _open_ok_r(payload, **kw):
    return _Resp(), _accR, ""


def _aggregate_dead(*a, **k):
    raise P.UpstreamStatus(403, '{"code":"TOKEN_EXPIRE"}')


_real_h3 = _with_handler(_open_ok_r, _aggregate_dead)
try:
    _real_h3[1]._handle_accounts("/accounts/test", {"uid": "acc-R"})
    _out3 = _real_h3[2][-1][1]
finally:
    _restore(_real_h3[0])
check("非排队信封 403 返回真实 status（不是 500）",
      _out3.get("status") == 403 and _out3.get("queued") is not True, _out3)
check("非排队信封 403 仍记录被测账号错误",
      bool(getattr(_accR, "last_error", "")), getattr(_accR, "last_error", ""))

# ---- 面板 queued toast
_dash = io.open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
check("面板对 queued 显示 warn toast，不显示「测试失败」",
      "r.queued" in _dash and "账号未被冷却" in _dash)

finish()
"""非流式排队耗尽：信封 RateLimited 必须 429 + Retry-After，不能 502（F4）。

    python tests/_test_queue_nonstream.py

上游 #87/#99 已覆盖 open_upstream 的 UpstreamQueued 分支；本套件专门钉住
aggregate_with_envelope_retry 抛出的 RateLimited（无 .status）在三个非流式
协议入口与 web tools 上的收尾。
"""
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *          # noqa: F401,F403

P, A = bootstrap()
declare_suite(__file__, ["qoder_proxy.py"])

QUEUE_DETAIL = json.dumps({"code": "10605", "isQueued": True,
                           "retryAfterSeconds": 30})
_FAKE_ACC = types.SimpleNamespace(uid="acc-NS")


class _Resp(object):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def read(self, n=None):
        return b"data: [DONE]\n\n"


class _WFile(object):
    def __init__(self, cap):
        self.cap = cap

    def write(self, data):
        self.cap["body"] += data
        return len(data)


def _mk_handler(path):
    h = object.__new__(P.Handler)
    cap = {"status": None, "headers": {}, "body": b""}
    h.path = path
    h.headers = {}
    h.client_address = ("127.0.0.1", 0)
    h.wfile = _WFile(cap)
    h.realm_used = ""
    h.realm_reason = ""

    def _send_response(code, message=None):
        cap["status"] = int(code)

    def _send_header(k, v):
        cap["headers"][str(k)] = str(v)

    h.send_response = _send_response
    h.send_header = _send_header
    h.end_headers = lambda: None
    h._request_realm = lambda explicit=None: "cn"
    h._model_precheck = lambda *a, **k: ""
    h._key_id_for_usage = lambda: ""
    h._authorized = lambda: True
    h._payload_or_error = lambda allow_list=False: {}
    h._panel_ok = lambda: True
    return h, cap


def _body(cap):
    try:
        return json.loads(cap["body"].decode("utf-8"))
    except Exception:
        return {}


def _run(handler_path, call, open_impl, aggregate_impl):
    _pool_dir = tempfile.mkdtemp(prefix="qns-")
    pool = A.AccountPool(_pool_dir, log=lambda *a, **k: None)
    acc = A.Account({"uid": "acc-NS", "realm": "cn", "accessToken": "tok"})
    acc.enabled = True
    acc.expires_at = 0
    pool.accounts.append(acc)
    real = (P.POOL, P.open_upstream, P.aggregate_with_envelope_retry,
            P.aggregate_stream, P.record_error)
    errors = []
    P.POOL = pool
    P.open_upstream = open_impl
    P.aggregate_with_envelope_retry = aggregate_impl
    P.aggregate_stream = aggregate_impl
    P.record_error = lambda *a, **k: errors.append((a, k))
    h, cap = _mk_handler(handler_path)
    try:
        call(h)
    finally:
        (P.POOL, P.open_upstream, P.aggregate_with_envelope_retry,
         P.aggregate_stream, P.record_error) = real
    return cap, errors


def _open_ok(payload=None, **kw):
    return _Resp(), _FAKE_ACC, ""


def _raise_rate_limited(*a, **k):
    raise P.RateLimited(None, "upstream queued (10605)", wait=30)


def _raise_queue_envelope(*a, **k):
    raise P.UpstreamStatus(403, QUEUE_DETAIL)


print("[queue] 非流式聚合：RateLimited -> 429")


def _anthropic_call(h):
    h._handle_anthropic("/v1/messages", {
        "model": "m", "max_tokens": 16, "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})


def _responses_call(h):
    h._handle_responses({"model": "m", "input": "hi", "stream": False})


_CHAT_PAYLOAD = {"model": "m", "stream": False,
                 "messages": [{"role": "user", "content": "hi"}]}


def _chat_call(h):
    h._payload_or_error = lambda allow_list=False: _CHAT_PAYLOAD
    h.do_POST()


for _name, _path, _call in (
    ("Anthropic", "/v1/messages", _anthropic_call),
    ("Responses", "/v1/responses", _responses_call),
    ("Chat", "/v1/chat/completions", _chat_call),
):
    _cap, _errs = _run(_path, _call, _open_ok, _raise_rate_limited)
    _obj = _body(_cap)
    check("%s 非流式排队耗尽 -> HTTP 429" % _name,
          _cap["status"] == 429, _cap["status"])
    check("%s 非流式排队耗尽 -> Retry-After: 30" % _name,
          _cap["headers"].get("Retry-After") == "30", _cap["headers"])
    check("%s 非流式排队耗尽记 429（不是 502）" % _name,
          _errs and _errs[-1][0][1] == 429, _errs[-1:] if _errs else None)
    check("%s 响应体 code=429" % _name,
          ((_obj.get("error") or {}).get("code") == 429), _obj)

print("[queue] web tools：HTTP 层 503 + 信封层 429")


def _open_queued(*a, **k):
    raise P.UpstreamQueued(30, QUEUE_DETAIL)


_WEB_PAYLOAD = {"model": "m", "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "web_search"}]}


def _web_call(h):
    h._handle_web_tools("/v1/chat/completions", _WEB_PAYLOAD, {"web_search"})


_cap_w, _ = _run("/v1/chat/completions", _web_call, _open_queued,
                 _raise_queue_envelope)
check("web tools HTTP 排队 -> HTTP 503", _cap_w["status"] == 503,
      _cap_w["status"])
check("web tools HTTP 排队 -> Retry-After: 30",
      _cap_w["headers"].get("Retry-After") == "30", _cap_w["headers"])
check("web tools HTTP 排队 -> 中文排队原因",
      "排队" in (((_body(_cap_w).get("error") or {}).get("message") or "")),
      _body(_cap_w))

_cap_w2, _ = _run("/v1/chat/completions", _web_call, _open_ok,
                  _raise_queue_envelope)
check("web tools 信封排队 -> HTTP 429", _cap_w2["status"] == 429,
      _cap_w2["status"])
check("web tools 信封排队 -> Retry-After: 30",
      _cap_w2["headers"].get("Retry-After") == "30", _cap_w2["headers"])

finish()

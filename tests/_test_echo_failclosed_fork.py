# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_anthropic.py"])
# -*- coding: utf-8 -*-
"""fail-closed 回归套件（fork 历史事故样本）。

    python tests/_test_echo_failclosed_fork.py

背景：2026-10-16 生产事故（opencode → ccload → fork / deepseek-flash）：模型把
网关写入历史的「LEAK_MARKER + JSON 数组」文本照抄回吐，**外层数组 JSON 合法、
item[0].arguments 内层转义被复述破坏**（json 错误点第 195 字符、中部）。fork
v1.5.3 曾用 partial salvage 还原 3 条、丢弃 1 条；v1.5.5 按上游 issue #37 改为
**fail-closed**：整批失败、0 个 tool_call 执行，四出口分别返回协议级错误。

本套件钉住：
  [1] 事故样本在 chat 流式 8 种分块 × 连续 3 轮下均 raise、0 tool_calls、
      无 marker/DSML 泄漏、无正常 finish；
  [2] chat 非流式 / Responses / Anthropic 三出口同样 fail-closed；
  [3] 合法条目 + 坏参数混合批次整批失败（禁止部分执行）；
  [4] 合法回声仍正常还原（正例不能丢）；长会话双 marker + DSML 尾恢复；
  [5] 不自动重试、不冷却账号；
  [6] 错误 detail 固定串，不含用户内容。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

_tmp_guard = tempfile.mkdtemp(prefix="qd-echo-failclosed-")
os.environ["QD_PROXY_USAGE_DIR"] = _tmp_guard

P, A = bootstrap()
import qoder_anthropic as AN  # noqa: E402

_M = P.LEAK_MARKER
_ALLOWED = {"bash"}

# 2026-10-16 事故泄漏原文（1273 字符）：散文 + marker + 4 条 bash 调用数组 +
# DSML 残渣尾；item[0] 的 arguments 内层转义损坏、item[1..3] 合法。
_SAMPLE = 'Cosy 检查那次调用漏了参数,补上;同时深入 maskit 最相关的四个提交引擎侧 diff:\n\n[assistant 请求调用工具]\n[{"name": "bash", "arguments": "{\\"command\\":\\"cd /tmp/opencode/competitor-review/CosyRedactGateway && git fetch origin --tags 2>&1 | tail -3; echo \'===NEW===\'; git log --oneline 0e2be2e..origin/HEAD 2>/dev/null | head -30; echo \\"(count: $(git log --oneline 0e2be2e..origin/HEAD 2>/dev/null | wc -l))\\"; git tag --sort=-creatordate | head -5\\",\\"workdir\\":\\"/workspace/private-llm-gateway\\",\\"timeout\\":120000}"}, {"name": "bash", "arguments": "{\\"command\\":\\"cd /tmp/opencode/competitor-review/maskit && git show 3e12145 -- engine/transparent.py | head -230\\",\\"workdir\\":\\"/workspace/private-llm-gateway\\",\\"timeout\\":120000}"}, {"name": "bash", "arguments": "{\\"command\\":\\"cd /tmp/opencode/competitor-review/maskit && git show a87a134 -- engine/upstream_retry.py | head -170; echo \'===transparent.py===\'; git show a87a134 -- engine/transparent.py\\",\\"workdir\\":\\"/workspace/private-llm-gateway\\",\\"timeout\\":120000}"}, {"name": "bash", "arguments": "{\\"command\\":\\"cd /tmp/opencode/competitor-review/maskit && git show f641fec -- engine/connection_policy.py | head -160\\",\\"workdir\\":\\"/workspace/private-llm-gateway\\",\\"timeout\\":120000}"}]</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>'


class _Stream:
    def __init__(self, frames):
        self._frames = list(frames)

    def __iter__(self):
        return iter(self._frames)


def _dc(text=None, fin=None):
    delta = {}
    if text is not None:
        delta["content"] = text
    return ("data: " + json.dumps({
        "id": "c", "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]},
        ensure_ascii=False) + "\n\n").encode("utf-8")


def _envelope(body, status=200):
    return ("data: " + json.dumps({"body": body, "statusCodeValue": status})
            + "\n\n").encode("utf-8")


def _envelope_chunk(text, fin=None):
    return _envelope(json.dumps({
        "id": "c", "choices": [{"index": 0,
                                 "delta": {"role": "assistant",
                                           "content": text},
                                 "finish_reason": fin}]}, ensure_ascii=False))


def _feed_chat(text, size):
    """按 size 分块喂 chat 流式，返回 (text, tool_calls, finish_list, raised)。"""
    if size:
        frames = [_dc(text[i:i + size]) for i in range(0, len(text), size)]
    else:
        frames = [_dc(text)]
    frames.append(_dc("", "stop"))
    out_text, calls, fins, raised = "", [], [], None
    try:
        for raw in P.recover_leaked_tool_calls(frames, _ALLOWED):
            obj = json.loads(raw.decode("utf-8")[6:])
            ch = obj["choices"][0]
            delta = ch.get("delta") or {}
            out_text += delta.get("content") or ""
            calls.extend(delta.get("tool_calls") or [])
            if ch.get("finish_reason"):
                fins.append(ch["finish_reason"])
    except P.InvalidToolArguments as exc:
        raised = exc
    return out_text, calls, fins, raised


def _no_leak(blob):
    return (_M not in blob and "DSML" not in blob and "</｜" not in blob)


def _valid_calls(n=4):
    return [{"name": "bash",
             "arguments": json.dumps({"command": "echo ok %d" % i})}
            for i in range(n)]


try:
    print("[0] 事故样本自校验")
    _prose, _echo_block = P._find_marker_split(_SAMPLE, allow_prefix=False)
    check("样本含散文前缀 + marker", bool(_prose) and _echo_block.startswith(_M))
    _arr = json.loads(P._strip_dsml_tail(_echo_block.strip())
                      .split(_M, 1)[1].strip())
    check("样本外层数组合法、4 条目", isinstance(_arr, list) and len(_arr) == 4)
    _bad = None
    try:
        json.loads(_arr[0].get("arguments"))
    except Exception as exc:  # noqa: BLE001 - 故意全捕
        _bad = exc
    check("item[0].arguments 非法（错误点中部）",
          _bad is not None and getattr(_bad, "pos", -1) == 195, _bad)
    check("item[1..3].arguments 合法",
          all(isinstance(json.loads(x.get("arguments")), dict)
              for x in _arr[1:]))
    check("严格 parse 整体拒绝（钉死不放松）",
          P.parse_leaked_tool_calls(_echo_block, _ALLOWED)[0] is None)

    print()
    print("[1] chat 流式：事故样本 8 种分块 × 3 轮，全部 fail-closed")
    for size in (1, 4, 7, 16, 64, 128, 512, None):
        for round_no in range(3):
            _t, _calls, _fins, _raised = _feed_chat(_SAMPLE, size)
            check("raise / size=%s round=%d" % (
                "整段" if size is None else size, round_no + 1),
                _raised is not None, (_t[:60], len(_calls), _fins))
            check("0 tool_calls（不部分执行）/ size=%s round=%d" % (
                "整段" if size is None else size, round_no + 1),
                not _calls, _calls[:1])
            check("无正常 finish / size=%s round=%d" % (
                "整段" if size is None else size, round_no + 1),
                not _fins, _fins)
            check("零 marker/DSML 泄漏 / size=%s round=%d" % (
                "整段" if size is None else size, round_no + 1),
                _no_leak(_t), _t[:120])

    print()
    print("[2] chat 非流式：事故样本 raise")
    _agg_raised = None
    try:
        P.aggregate_stream(
            _Stream([_envelope_chunk(_SAMPLE[:600]),
                     _envelope_chunk(_SAMPLE[600:]),
                     _envelope_chunk("", fin="stop")]),
            "m", None, holder={}, allowed_names=_ALLOWED)
    except P.InvalidToolArguments as exc:
        _agg_raised = exc
    check("aggregate_stream raise InvalidToolArguments", _agg_raised is not None)

    print()
    print("[3] Responses 流：事故样本 raise 且不泄漏")
    _resp_events = []
    _resp_raised = None
    try:
        for raw in P.stream_responses_events(
                iter([_dc(_SAMPLE[:600]), _dc(_SAMPLE[600:]), _dc("", "stop")]),
                "m", {"usage": None, "custom_names": set(),
                      "allowed_names": _ALLOWED}):
            _resp_events.append(raw.decode("utf-8"))
    except P.InvalidToolArguments as exc:
        _resp_raised = exc
    _resp_joined = "".join(_resp_events)
    check("Responses raise InvalidToolArguments", _resp_raised is not None)
    check("Responses 无 marker/DSML 泄漏", _no_leak(_resp_joined),
          _resp_joined[:160])
    check("Responses 抛出前无 response.completed / response.failed",
          "response.completed" not in _resp_joined
          and "response.failed" not in _resp_joined)

    print()
    print("[4] Anthropic 桥：异常向上抛出，出口映射为 event: error")
    _ant_raised = None
    try:
        for _ in AN.stream_anthropic_events(
                P.recover_leaked_tool_calls(
                    [_dc(_SAMPLE[:600]), _dc(_SAMPLE[600:]), _dc("", "stop")],
                    _ALLOWED),
                "m", {"usage": None, "custom_names": set(),
                      "allowed_names": _ALLOWED}):
            pass
    except P.InvalidToolArguments as exc:
        _ant_raised = exc
    check("Anthropic 桥向上抛 InvalidToolArguments", _ant_raised is not None)
    check("event: error 帧可用",
          AN.anthropic_error_frame("x").startswith(b"event: error"))

    print()
    print("[5] 混合批次：1 合法 + 1 坏参数 -> 整批失败、0 部分执行")
    _mixed = _M + "\n" + json.dumps(
        [{"name": "bash", "arguments": json.dumps({"command": "echo ok"})},
         {"name": "bash", "arguments": '{"command": "echo'}],
        ensure_ascii=False)
    for size in (1, 16, None):
        _t, _calls, _fins, _raised = _feed_chat(_mixed, size)
        check("混合批次 raise / size=%s" % ("整段" if size is None else size),
              _raised is not None, (_t[:60], len(_calls), _fins))
        check("混合批次 0 tool_calls / size=%s" % (
            "整段" if size is None else size), not _calls, _calls[:1])
        check("混合批次无正常 finish / size=%s" % (
            "整段" if size is None else size), not _fins, _fins)

    print()
    print("[6] 正例：合法回声仍还原；长会话双 marker + DSML 尾可恢复")
    _valid = _M + "\n" + json.dumps(_valid_calls(4), ensure_ascii=False)
    _t, _calls, _fins, _raised = _feed_chat(_valid, 7)
    check("合法回声 4 calls、finish=tool_calls、raise=None",
          _raised is None and len(_calls) == 4 and _fins[-1] == "tool_calls",
          (_t[:60], len(_calls), _fins))
    check("合法回声零 marker/DSML 泄漏", _no_leak(_t), _t[:120])

    _long = ("好的。\n\n" + _M + "\n" + _M + "\n"
             + json.dumps(_valid_calls(4), ensure_ascii=False)
             + "\n</｜｜DSML｜｜ calls>")
    _t, _calls, _fins, _raised = _feed_chat(_long, 8)
    check("#49 长会话双 marker + DSML 尾 -> 4 calls、finish=tool_calls",
          _raised is None and len(_calls) == 4 and _fins[-1] == "tool_calls",
          (_t[:80], len(_calls), _fins))
    check("#49 正文不含 marker / DSML", _no_leak(_t), _t[:120])

    print()
    print("[7] 不重试、不冷却账号、错误 detail 固定串")
    check("should_retry_envelope(InvalidToolArguments) is False",
          P.should_retry_envelope(P.InvalidToolArguments(), 0, 0) is False)
    _acc = A.Account({"uid": "failclosed-uid", "realm": "cn",
                      "accessToken": "tok"})
    _before = (_acc.cooldown_until, _acc.last_error)
    P._handle_envelope_account_cooldown(_acc, P.InvalidToolArguments())
    _after = (_acc.cooldown_until, _acc.last_error)
    check("账号 cooldown/last_error 不变", _before == _after,
          (_before, _after))
    _detail = str(P.InvalidToolArguments())
    check("detail 为固定串、不含样本内容",
          "bash" not in _detail and "command" not in _detail
          and "Cosy" not in _detail, _detail)
finally:
    shutil.rmtree(_tmp_guard, ignore_errors=True)

finish()

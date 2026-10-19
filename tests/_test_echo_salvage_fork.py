# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py", "qoder_anthropic.py"])
# -*- coding: utf-8 -*-
"""partial salvage 回归套件（fork 语义）。

    python tests/_test_echo_salvage_fork.py

背景：2026-10-16 生产事故（opencode → ccload → fork / deepseek-flash）：模型把
网关写入历史的「LEAK_MARKER + JSON 数组」文本照抄回吐，外层数组合法但
item[0].arguments 内层转义被复述破坏。v1.5.5 曾按上游 #37 改为 fail-closed；
v1.5.6 按用户决定恢复 fork 方案：

  - 混合批次：保留合法条目、丢弃坏条目（WARN），finish=tool_calls；
  - 0 合法 / 空数组：整块吞掉，只补发 marker 前散文，finish=stop；
  - 不泄漏 marker/DSML/裸 JSON，不返回 error；
  - 合法回声仍正常还原。

本套件钉住三链路、四出口、事故样本与多分帧。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

_tmp_guard = tempfile.mkdtemp(prefix="qd-echo-salvage-fork-")
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
    delta = {} if text is None else {"content": text}
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


def _feed_chat(text, size=None):
    if size:
        frames = [_dc(text[i:i + size]) for i in range(0, len(text), size)]
    else:
        frames = [_dc(text)]
    frames.append(_dc("", "stop"))
    out_text, calls, fins = "", [], []
    for raw in P.recover_leaked_tool_calls(frames, _ALLOWED):
        obj = json.loads(raw.decode("utf-8")[6:])
        ch = obj["choices"][0]
        delta = ch.get("delta") or {}
        out_text += delta.get("content") or ""
        calls.extend(delta.get("tool_calls") or [])
        if ch.get("finish_reason"):
            fins.append(ch["finish_reason"])
    return out_text, calls, fins


def _no_leak(blob):
    return (_M not in blob and "DSML" not in blob and "</｜" not in blob)


def _valid_calls(n=4):
    return [{"name": "bash",
             "arguments": json.dumps({"command": "echo ok %d" % i})}
            for i in range(n)]


def _echo_block(items):
    return _M + "\n" + json.dumps(items, ensure_ascii=False)


def _resp_events(text, size=None):
    if size:
        frames = [_dc(text[i:i + size]) for i in range(0, len(text), size)]
    else:
        frames = [_dc(text)]
    frames.append(_dc("", "stop"))
    return [f.decode("utf-8") for f in P.stream_responses_events(
        iter(frames), "m", {"usage": None, "custom_names": set(),
                            "allowed_names": _ALLOWED})]


try:
    print("[0] 事故样本自校验")
    _prose, _echo_block_src = P._find_marker_split(_SAMPLE, allow_prefix=False)
    check("样本含散文前缀 + marker", bool(_prose) and _echo_block_src.startswith(_M))
    _arr = json.loads(P._strip_dsml_tail(_echo_block_src.strip())
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
    check("严格 parse 整体拒绝", P.parse_leaked_tool_calls(
        _echo_block_src, _ALLOWED)[0] is None)
    _salv = P.salvage_leaked_tool_calls(_echo_block_src, _ALLOWED)
    check("salvage 3 还原 / 1 弃（idx=0:args-not-json）",
          _salv is not None and len(_salv[0]) == 3
          and _salv[1] == [(0, "args-not-json")],
          (_salv[0][:1] if _salv else None,
           _salv[1] if _salv else None))

    print()
    print("[1] chat 流式：事故样本 8 种分块 × 2 轮，3 还原 / 1 弃 / 零泄漏")
    for size in (1, 4, 7, 16, 64, 128, 512, None):
        for round_no in range(2):
            _t, _calls, _fins = _feed_chat(_SAMPLE, size)
            label = "整段" if size is None else str(size)
            check("3 calls / size=%s round=%d" % (label, round_no + 1),
                  len(_calls) == 3, (_t[:60], len(_calls), _fins))
            check("finish=tool_calls / size=%s round=%d" % (label, round_no + 1),
                  _fins and _fins[-1] == "tool_calls", _fins)
            check("正文只留散文、零泄漏 / size=%s round=%d" % (label, round_no + 1),
                  _t == _prose and _no_leak(_t), _t[:120])

    print()
    print("[2] chat 非流式：事故样本 3 tool_calls + finish=tool_calls")
    _agg = P.aggregate_stream(
        _Stream([_envelope_chunk(_SAMPLE[:600]),
                 _envelope_chunk(_SAMPLE[600:]),
                 _envelope_chunk("", fin="stop")]),
        "m", None, holder={}, allowed_names=_ALLOWED)
    _msg = _agg["choices"][0]["message"]
    check("content=散文、3 calls、finish=tool_calls",
          _msg.get("content") == _prose
          and len(_msg.get("tool_calls") or []) == 3
          and _agg["choices"][0]["finish_reason"] == "tool_calls",
          (_msg.get("content") or "")[:80])

    print()
    print("[3] Responses 流：3 个 function_call、零泄漏、正常 completed")
    _evs = _resp_events(_SAMPLE)
    _parsed = [json.loads(_ln[6:]) for _fr in _evs
               for _ln in _fr.splitlines() if _ln.startswith("data: ")]
    _txt = "".join(e.get("delta") or "" for e in _parsed
                   if e.get("type") == "response.output_text.delta")
    _fcs = [e["item"] for e in _parsed
            if e.get("type") == "response.output_item.done"
            and (e.get("item") or {}).get("type") == "function_call"]
    _joined = "".join(_evs)
    check("正文=散文、3 个 function_call",
          _txt == _prose and len(_fcs) == 3
          and all(f.get("name") == "bash" for f in _fcs),
          (_txt[:80], len(_fcs)))
    check("无 response.failed、正常 completed、零泄漏",
          "response.failed" not in _joined
          and "response.completed" in _joined and _no_leak(_joined),
          _joined[:120])

    print()
    print("[4] Anthropic 桥：3 个 tool_use、message_stop、无 error")
    _ant = "".join(f if isinstance(f, str) else f.decode("utf-8", "replace")
                   for f in AN.stream_anthropic_events(
                       P.recover_leaked_tool_calls(
                           [_dc(_SAMPLE[:600]), _dc(_SAMPLE[600:]),
                            _dc("", "stop")], _ALLOWED),
                       "m", {"usage": None, "custom_names": set(),
                             "allowed_names": _ALLOWED}))
    check("tool_use 3 个、message_stop、无 event: error",
          _ant.count("tool_use") >= 3 and "message_stop" in _ant
          and "event: error" not in _ant and _no_leak(_ant),
          _ant[-300:])

    print()
    print("[5] 0 合法 / 空数组 / 未声明：整块吞掉，无 error、零泄漏、finish=stop")
    _bad_only = _echo_block([{"name": "bash",
                              "arguments": '{"command": "echo'}])
    _empty = _M + "\n[]"
    _undeclared = _echo_block([{"name": "other", "arguments": "{}"}])
    for _label, _text in (("0 合法", _bad_only), ("空数组", _empty),
                          ("全未声明", _undeclared)):
        _salv = P.salvage_leaked_tool_calls(_text, _ALLOWED)
        check("%s salvage 返回 calls=[]（不回落 fail-open）" % _label,
              _salv is not None and _salv[0] == [], _salv)
        _t, _calls, _fins = _feed_chat(_text, 7)
        check("%s 流式：0 calls、finish=stop、零泄漏" % _label,
              not _calls and _fins and _fins[-1] == "stop"
              and _t == "" and _no_leak(_t), (_t[:80], _calls, _fins))

    print()
    print("[5b] 0 合法条目在非流式 / Responses / Anthropic 同样吞掉、不报错")
    _agg_bad = P.aggregate_stream(
        _Stream([_envelope_chunk(_bad_only), _envelope_chunk("", fin="stop")]),
        "m", None, holder={}, allowed_names=_ALLOWED)
    _msg_bad = _agg_bad["choices"][0]["message"]
    check("非流式：content 空、0 calls、finish=stop",
          _msg_bad.get("content") == ""
          and not (_msg_bad.get("tool_calls") or [])
          and _agg_bad["choices"][0]["finish_reason"] == "stop",
          (_msg_bad, _agg_bad["choices"][0].get("finish_reason")))
    _evs_bad = _resp_events(_bad_only)
    _joined_bad = "".join(_evs_bad)
    check("Responses：无 failed、正常 completed、无 function_call、零泄漏",
          "response.failed" not in _joined_bad
          and "response.completed" in _joined_bad
          and "function_call" not in _joined_bad and _no_leak(_joined_bad),
          _joined_bad[:160])
    _ant_bad = "".join(f if isinstance(f, str) else f.decode("utf-8", "replace")
                       for f in AN.stream_anthropic_events(
                           P.recover_leaked_tool_calls(
                               [_dc(_bad_only), _dc("", "stop")], _ALLOWED),
                           "m", {"usage": None, "custom_names": set(),
                                 "allowed_names": _ALLOWED}))
    check("Anthropic：无 event: error、message_stop、无 tool_use、零泄漏",
          "event: error" not in _ant_bad and "message_stop" in _ant_bad
          and "tool_use" not in _ant_bad and _no_leak(_ant_bad),
          _ant_bad[-300:])

    print()
    print("[6] 混合合法 + 未声明：1 还原 / 1 弃")
    _mixed_names = _echo_block(
        [{"name": "bash", "arguments": "{}"},
         {"name": "other", "arguments": "{}"}])
    _salv_m = P.salvage_leaked_tool_calls(_mixed_names, _ALLOWED)
    check("salvage 1 还原 / 1 弃（undeclared-name）",
          _salv_m is not None and len(_salv_m[0]) == 1
          and _salv_m[1] == [(1, "undeclared-name")], _salv_m)
    _t, _calls, _fins = _feed_chat(_mixed_names, 7)
    check("流式：1 call、finish=tool_calls、零泄漏",
          len(_calls) == 1 and _fins and _fins[-1] == "tool_calls"
          and _t == "" and _no_leak(_t), (_t[:80], len(_calls), _fins))

    print()
    print("[7] 正例：合法回声 / #49 双 marker + DSML 尾仍正常还原")
    _valid = _echo_block(_valid_calls(4))
    _t, _calls, _fins = _feed_chat(_valid, 7)
    check("合法回声 4 calls、finish=tool_calls、零泄漏",
          len(_calls) == 4 and _fins and _fins[-1] == "tool_calls"
          and _no_leak(_t), (_t[:80], len(_calls), _fins))
    _long = ("好的。\n\n" + _M + "\n" + _M + "\n"
             + json.dumps(_valid_calls(4), ensure_ascii=False)
             + "\n</｜｜DSML｜｜ calls>")
    _t, _calls, _fins = _feed_chat(_long, 8)
    check("#49 长会话双 marker：4 calls、finish=tool_calls、零泄漏",
          len(_calls) == 4 and _fins and _fins[-1] == "tool_calls"
          and _no_leak(_t), (_t[:80], len(_calls), _fins))

    print()
    print("[8] 既有边界：Extra data / 截断")
    _extra = _echo_block(_valid_calls(1)) + " 以上。"
    _t, _calls, _fins = _feed_chat(_extra, 7)
    check("Extra data：原样透传、无 tool_calls、finish=stop",
          _t == _extra and not _calls and _fins and _fins[-1] == "stop",
          (_t[:80], _calls, _fins))
    _trunc = _M + '\n[{"name": "bash", "arguments": '
    _t, _calls, _fins = _feed_chat(_trunc, 7)
    check("截断：整段吞掉、无 tool_calls、finish=stop",
          _t == "" and not _calls and _fins and _fins[-1] == "stop",
          (_t[:80], _calls, _fins))

    print()
    print("[9] 观测：salvage / dropped-all WARN 留痕")
    _logs = P.get_logs(limit=0, level="WARN")
    _msgs = [x.get("msg") or "" for x in (_logs.get("logs") or [])]
    check("含 salvaged leaked tool-call echo（事故样本）",
          any("salvaged leaked tool-call echo" in m
              and "idx=0:args-not-json" in m for m in _msgs), _msgs[:2])
    check("含 dropped all leaked tool-call echo（0 合法/空数组）",
          any("dropped all leaked tool-call echo" in m for m in _msgs),
          _msgs[:4])
finally:
    shutil.rmtree(_tmp_guard, ignore_errors=True)

finish()

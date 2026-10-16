# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py"])
# -*- coding: utf-8 -*-
"""salvage 部分挽救套件（外层合法 + 条目级损坏的回声块）。

    python tests/_test_echo_salvage.py

背景：2026-10-16 生产事故（opencode → ccload → fork / deepseek-flash）：模型把
网关写入历史的「LEAK_MARKER + JSON 数组」文本照抄回吐，**外层数组 JSON 合法、
item[0].arguments 内层转义被复述破坏**（json 错误点第 195 字符、中部）——parse
严格判定整体失败，修复前该形态被 fail-open 当正文透传（客户端把 JSON 当正文、
本轮工具不执行、问答循环终止）。修复：三链路在 parse 与 droppable 之间插入
`salvage_leaked_tool_calls`（≥1 合法条目 -> 还原合法条目、丢弃损坏条目 + WARN）。

本套件钉住三件事：
  [1] 判据：parse 保持严格（整体拒绝）；salvage 对事故样本 3 还原 / 1 弃
      （reason=args-not-json）；0 合法 -> None（零放宽核心守卫）；
  [2] 三链路（chat 流 / 非流式 / responses 流）集成：3 个 tool_calls、
      finish=tool_calls（chat/非流式）、正文零泄漏；另含 7 字符/帧分帧（A4）；
  [3] 零放宽矩阵：0 合法 / 未声明 / 空数组 / Extra data / 截断 / 未闭合中部 /
      讨论散文等「不变」行与修复前逐字节一致（2026-10-16 与 git HEAD 差分实证）。

事故原文为 research/leak_sample.txt 的逐字节副本（以 repr 生成内联，防手工转录
破坏转义）。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

# usage/ 落盘隔离到临时目录：仓库里的 usage/ 在并发编排下是跨进程共享路径，
# 套件不得往里写（见 tests/README.md「迁移期纪律」第 3 条）。
_tmp_guard = tempfile.mkdtemp(prefix="qd-echo-salvage-")
os.environ["QD_PROXY_USAGE_DIR"] = _tmp_guard

P, A = bootstrap()

_M = P.LEAK_MARKER
_ALLOWED = {"bash"}

# 2026-10-16 事故泄漏原文（1273 字符 = research/leak_sample.txt 逐字节副本）：
# 散文 + marker + 4 条 bash 调用数组 + DSML 残渣尾；item[0] 的 arguments 内层
# 转义损坏、item[1..3] 合法。
_SAMPLE = 'Cosy 检查那次调用漏了参数,补上;同时深入 maskit 最相关的四个提交引擎侧 diff:\n\n[assistant 请求调用工具]\n[{"name": "bash", "arguments": "{\\"command\\":\\"cd /tmp/opencode/competitor-review/CosyRedactGateway && git fetch origin --tags 2>&1 | tail -3; echo \'===NEW===\'; git log --oneline 0e2be2e..origin/HEAD 2>/dev/null | head -30; echo \\"(count: $(git log --oneline 0e2be2e..origin/HEAD 2>/dev/null | wc -l))\\"; git tag --sort=-creatordate | head -5\\",\\"workdir\\":\\"/workspace/private-llm-gateway\\",\\"timeout\\":120000}"}, {"name": "bash", "arguments": "{\\"command\\":\\"cd /tmp/opencode/competitor-review/maskit && git show 3e12145 -- engine/transparent.py | head -230\\",\\"workdir\\":\\"/workspace/private-llm-gateway\\",\\"timeout\\":120000}"}, {"name": "bash", "arguments": "{\\"command\\":\\"cd /tmp/opencode/competitor-review/maskit && git show a87a134 -- engine/upstream_retry.py | head -170; echo \'===transparent.py===\'; git show a87a134 -- engine/transparent.py\\",\\"workdir\\":\\"/workspace/private-llm-gateway\\",\\"timeout\\":120000}"}, {"name": "bash", "arguments": "{\\"command\\":\\"cd /tmp/opencode/competitor-review/maskit && git show f641fec -- engine/connection_policy.py | head -160\\",\\"workdir\\":\\"/workspace/private-llm-gateway\\",\\"timeout\\":120000}"}]</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>'


class _Stream:
    def __init__(self, frames):
        self._frames = list(frames)

    def __iter__(self):
        return iter(self._frames)


def _dc(text=None, fin=None):
    """合成 chat.completion.chunk 帧（无信封）。"""
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
    """合成带信封的 chat chunk（aggregate_stream 的输入形态）。"""
    return _envelope(json.dumps({
        "id": "c", "choices": [{"index": 0,
                                 "delta": {"role": "assistant",
                                           "content": text},
                                 "finish_reason": fin}]}, ensure_ascii=False))


def _chat_detail(frames):
    """按客户端视角拼回 (text, tool_calls, finish_reason)。"""
    text, calls, fin = "", [], None
    for f in frames:
        obj = json.loads(f[6:])
        ch = obj["choices"][0]
        delta = ch.get("delta") or {}
        text += delta.get("content") or ""
        calls.extend(delta.get("tool_calls") or [])
        if ch.get("finish_reason"):
            fin = ch["finish_reason"]
    return text, calls, fin


try:
    print("[0] 样本自校验（逐字节副本可用性 + 事故形态成立）")
    # 链路视角的回声块：散文由 _find_marker_split 切走（判据层只接受 marker
    # 起的块），这里与三链路喂给 parse/salvage 的输入保持一致。
    _prose, _echo_block = P._find_marker_split(_SAMPLE, allow_prefix=False)
    check("样本含散文前缀 + marker（链路切分前的原文形态）",
          bool(_prose) and _echo_block.startswith(_M))
    _arr = json.loads(P._strip_dsml_tail(_echo_block.strip())
                      .split(_M, 1)[1].strip())
    check("样本外层数组 JSON 合法、4 条目（事故的定义特征）",
          isinstance(_arr, list) and len(_arr) == 4, len(_arr))
    _e0 = None
    try:
        json.loads(_arr[0].get("arguments"))
    except Exception as exc:                       # noqa: BLE001 - 故意全捕
        _e0 = exc
    check("item[0].arguments 不是合法 JSON（错误点第 195 字符、中部）",
          _e0 is not None and getattr(_e0, "pos", -1) == 195, _e0)
    _ok = []
    for _it in _arr[1:]:
        try:
            json.loads(_it.get("arguments"))
            _ok.append(True)
        except Exception:                          # noqa: BLE001
            _ok.append(False)
    check("item[1..3].arguments 全部合法", _ok == [True, True, True], _ok)
    check("parse（严格判定）对事故回声块整体拒绝（None），钉死不放松",
          P.parse_leaked_tool_calls(_echo_block, _ALLOWED)[0] is None)
    check("修复前 fail-open 路径成立：droppable=False（外层合法即不吞）",
          P._leaked_partial_droppable(_echo_block, _ALLOWED) is False)

    print()
    print("[1] 判据：salvage 对事故样本部分还原（3 还原 / 1 弃）")
    _salv = P.salvage_leaked_tool_calls(_echo_block, _ALLOWED)
    check("salvage -> 3 合法条目 + 1 丢弃（idx=0:args-not-json）",
          _salv is not None and len(_salv[0]) == 3
          and _salv[1] == [(0, "args-not-json")],
          (_salv[0][:1] if _salv else None, _salv[1] if _salv else None))
    check("3 条还原均为 bash 且 arguments 为合法 JSON 字符串",
          _salv is not None
          and all(c["function"]["name"] == "bash"
                  and isinstance(json.loads(c["function"]["arguments"]), dict)
                  for c in _salv[0]))
    check("0 合法条目 -> None（零放宽核心守卫，回落既有链）",
          P.salvage_leaked_tool_calls(
              _M + "\n" + json.dumps([{"name": "bash",
                                       "arguments": "{bad"}]), _ALLOWED)
          is None)
    check("空数组 -> None（[27] 钉死：不因 salvage 改变）",
          P.salvage_leaked_tool_calls(_M + "\n[]", _ALLOWED) is None)
    check("未声明 tools（None / 空集）不改变判据语义（名检跳过）",
          P.salvage_leaked_tool_calls(
              _M + "\n" + json.dumps([{"name": "anything",
                                       "arguments": "{}"}]), None)
          is not None)

    print()
    print("[2] chat 流式：事故样本整块 -> 3 tool_calls + finish=tool_calls + 零泄漏")
    _t, _calls, _fin = _chat_detail(P.recover_leaked_tool_calls(
        [_dc(_SAMPLE), _dc("", "stop")], _ALLOWED))
    check("正文只留 marker 前散文（逐字节）", _t == _prose, _t[:120])
    check("3 条结构化 tool_calls 增量帧、其名均为 bash", len(_calls) == 3
          and all((c.get("function") or {}).get("name") == "bash"
                  for c in _calls), _calls[:1])
    check("finish_reason 改写为 tool_calls", _fin == "tool_calls", _fin)
    _dumped = json.dumps(_calls, ensure_ascii=False)
    check("正文零泄漏：无 marker / 无 DSML 残渣 / 无原始数组转义残渣",
          _M not in _t and "DSML" not in _t and "</｜" not in _t
          and "DSML" not in _dumped and _M not in _dumped)

    print()
    print("[3] chat 流式：7 字符/帧分帧到流末同样挽救（A4，不 mid-block 泄漏）")
    _frames7 = [_dc(_SAMPLE[i:i + 7]) for i in range(0, len(_SAMPLE), 7)]
    _frames7.append(_dc("", "stop"))
    _t7, _calls7, _fin7 = _chat_detail(
        P.recover_leaked_tool_calls(_frames7, _ALLOWED))
    check("分帧：正文 = 散文（逐字节），3 calls，finish=tool_calls，零泄漏",
          _t7 == _prose and len(_calls7) == 3 and _fin7 == "tool_calls"
          and _M not in _t7 and "DSML" not in _t7,
          (_t7[:80], len(_calls7), _fin7))

    print()
    print("[4] 非流式（信封流）：3 tool_calls + finish=tool_calls + 散文保留")
    _agg = P.aggregate_stream(
        _Stream([_envelope_chunk(_SAMPLE[:600]),
                 _envelope_chunk(_SAMPLE[600:]),
                 _envelope_chunk("", fin="stop")]),
        "m", None, holder={}, allowed_names=_ALLOWED)
    _msg = _agg["choices"][0]["message"]
    _fcalls = _msg.get("tool_calls") or []
    check("content 只留 marker 前散文；3 条 bash 调用；finish=tool_calls",
          _msg.get("content") == _prose and len(_fcalls) == 3
          and _agg["choices"][0]["finish_reason"] == "tool_calls"
          and all(c["function"]["name"] == "bash" for c in _fcalls),
          (_msg.get("content") or "")[:80])

    print()
    print("[5] Responses 流：3 个 function_call 项、零文本泄漏")
    _evs = [f.decode("utf-8") for f in P.stream_responses_events(
        iter([_dc(_SAMPLE[:600]), _dc(_SAMPLE[600:]), _dc("", "stop")]), "m",
        {"usage": None, "custom_names": set(), "allowed_names": _ALLOWED})]
    _parsed = [json.loads(_ln[6:]) for _fr in _evs
               for _ln in _fr.splitlines() if _ln.startswith("data: ")]
    _txt = "".join(e.get("delta") or "" for e in _parsed
                   if e.get("type") == "response.output_text.delta")
    _fcs = [e["item"] for e in _parsed
            if e.get("type") == "response.output_item.done"
            and (e.get("item") or {}).get("type") == "function_call"]
    _joined = "".join(_evs)
    check("正文增量 = 散文（逐字节）；3 个 function_call（name=bash）",
          _txt == _prose and len(_fcs) == 3
          and all(f.get("name") == "bash" for f in _fcs),
          (_txt[:80], len(_fcs)))
    check("事件流零泄漏：无 marker / 无 DSML / 无数组残渣",
          _M not in _joined and "DSML" not in _joined
          and "</｜" not in _joined)

    print()
    print("[6] 零放宽矩阵（design §2.4「不变」行：与修复前逐字节一致）")
    _rows_failopen = [
        ("单条目 arguments 非法（0 合法）",
         _M + "\n" + json.dumps([{"name": "bash",
                                  "arguments": "{not-json"}])),
        ("未声明工具名单条目数组（0 合法）",
         _M + "\n" + json.dumps([{"name": "other", "arguments": "{}"}])),
        ("空数组 []", _M + "\n[]"),
        ("合法数组 + 尾随散文（Extra data）",
         _M + "\n" + json.dumps([{"name": "bash", "arguments": "{}"}])
         + " 以上。"),
        ("未闭合 + 中部错误（裸 token）", _M + '\n[{"name": shell'),
        ("marker + 散文（讨论形态）", _M + "\n我打算调用 shell。"),
        ("正文中的 marker（不在开头）",
         "网关会写入 " + _M + " 这样的提示。"),
    ]
    for _label, _text in _rows_failopen:
        _tq, _cq, _fq = _chat_detail(P.recover_leaked_tool_calls(
            [_dc(_text), _dc("", "stop")], _ALLOWED))
        check("%s -> fail-open 原文逐字节补发、无 tool_calls、finish=stop"
              % _label,
              _tq == _text and not _cq and _fq == "stop",
              (_tq[:60], bool(_cq), _fq))
        check("%s -> salvage 无挽救（None，不改既有链）" % _label,
              P.salvage_leaked_tool_calls(_text, _ALLOWED) is None)

    _trunc = (_M + '\n[{"name": "bash", "arguments": '
              '"{\\"command\\": "echo probe"}]')
    check("截断/闭合非法 + 尾 ]：droppable 仍 True（[9] 吞掉面不动）",
          P._leaked_partial_droppable(_trunc, _ALLOWED) is True)
    _tt, _ct, _ft = _chat_detail(P.recover_leaked_tool_calls(
        [_dc(_trunc), _dc("", "stop")], _ALLOWED))
    check("截断回声流式仍吞掉：零正文、无 tool_calls、finish=stop",
          _tt == "" and not _ct and _ft == "stop", (_tt[:60], _ft))
    check("截断 -> salvage 无挽救（json 失败 -> None）",
          P.salvage_leaked_tool_calls(_trunc, _ALLOWED) is None)

    print()
    print("[7] 混合：合法条目 + 未声明名条目 -> 合法还原、未声明弃")
    _mixed = _M + "\n" + json.dumps(
        [{"name": "bash", "arguments": "{}"},
         {"name": "other", "arguments": "{}"}], ensure_ascii=False)
    _salv_m = P.salvage_leaked_tool_calls(_mixed, _ALLOWED)
    check("salvage -> 1 还原（bash）+ 1 弃（idx=1:undeclared-name）",
          _salv_m is not None and len(_salv_m[0]) == 1
          and _salv_m[1] == [(1, "undeclared-name")],
          (_salv_m[1] if _salv_m else None))
    _tm, _cm, _fm = _chat_detail(P.recover_leaked_tool_calls(
        [_dc(_mixed), _dc("", "stop")], _ALLOWED))
    check("chat 流：1 tool_call（bash）、正文零泄漏、finish=tool_calls",
          _tm == "" and len(_cm) == 1
          and (_cm[0].get("function") or {}).get("name") == "bash"
          and _fm == "tool_calls", (_tm, _fm))

    print()
    print("[8] 观测：salvage 留痕 WARN（含 restored/dropped 明细与原因）")
    _logs = P.get_logs(limit=0, level="WARN",
                       search="salvaged leaked tool-call echo")
    _msgs = [x.get("msg") or "" for x in (_logs.get("logs") or [])]
    check("WARN 含 3 call(s) restored + idx=0:args-not-json（事故样本）",
          any("3 call(s) restored" in m and "idx=0:args-not-json" in m
              for m in _msgs), _msgs[:2])
    check("WARN 含 1 call(s) restored + idx=1:undeclared-name（混合样本）",
          any("1 call(s) restored" in m and "idx=1:undeclared-name" in m
              for m in _msgs), _msgs[:3])
finally:
    shutil.rmtree(_tmp_guard, ignore_errors=True)

finish()

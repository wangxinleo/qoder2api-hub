# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py"])
# -*- coding: utf-8 -*-
"""回声吞掉判据守卫套件（坏 JSON 的 marker+数组回声不得透传）。

    python tests/_test_echo_recovery.py

背景：2026-10-16 切换模型战役（E2E）实测——模型复述网关写入历史的 JSON 数组
块时把 arguments 的内层转义破坏掉（`"arguments": "{\\"command\\": "echo probe"}]`），
json 错误点落在中部 -> _json_array_prefix_ok 判 False -> issue #9 的吞掉判定
失配 -> 整段内部协议文本透传给客户端。本套件钉住「闭合的 ] 定稿但解析非法
-> 吞」，同时验证既有防线不被误伤（合法数组 fail-open 透传 / Extra data 混排 /
散文 / 未声明 tools）。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

# usage/ 落盘隔离到临时目录：仓库里的 usage/ 在并发编排下是跨进程共享路径，
# 套件不得往里写（见 tests/README.md「迁移期纪律」第 3 条）。
_tmp_guard = tempfile.mkdtemp(prefix="qd-echo-recovery-")
os.environ["QD_PROXY_USAGE_DIR"] = _tmp_guard

P, A = bootstrap()

_M = P.LEAK_MARKER
_ALLOWED = {"shell"}

# E2E 泄漏原文（109 字符，逐字节照抄）：注意 "{\\"command\\": "echo 处
# 内层引号未被复述转义 —— JSON 解析在 echo 的 e 处失败（错误点在中部）。
_SAMPLE = ('[assistant 请求调用工具]\n'
           '[{"name": "shell", "arguments": '
           '"{\\"command\\": "echo probe-$(date +%s)-$(hostname) 2>&1"}]')


class _Stream:
    def __init__(self, frames):
        self._frames = list(frames)

    def __iter__(self):
        return iter(self._frames)


def _envelope(body, status=200):
    return ("data: " + json.dumps({"body": body, "statusCodeValue": status})
            + "\n\n").encode("utf-8")


def _dc(text, fin=None):
    return ("data: " + json.dumps({
        "id": "c", "model": "m",
        "choices": [{"index": 0, "delta": {"content": text},
                     "finish_reason": fin}]}) + "\n\n").encode("utf-8")


try:
    print("[0] 样本自校验：_SAMPLE 确实是坏 JSON（错误点在中部）")
    _rest = _SAMPLE.split(_M, 1)[1].strip()
    _exc = None
    try:
        json.loads(_rest)
    except Exception as exc:                       # noqa: BLE001 - 故意全捕
        _exc = exc
    check("样本 json.loads 失败且错误点不在末尾", _exc is not None
          and not str(_exc).startswith(("Unterminated", "Extra data")), _exc)

    print()
    print("[1] 实证样本：走吞掉判据（修复的非回归锚点）")
    check("_leaked_partial_droppable(实测样本) -> True（吞）",
          P._leaked_partial_droppable(_SAMPLE, _ALLOWED) is True)

    print()
    print("[2] 闭合但非法的变体：一律吞")
    _bad = {
        "参数值裸 token": _M + '\n[{"name": shell, "arguments": "{}"}]',
        "缺逗号分隔": _M + '\n[{"name": "shell" "arguments": "{}"}]',
        "缺冒号": _M + '\n[{"name" "shell"}]',
        "尾部多逗号+反序键": _M + '\n[{"arguments": "{}",,}]',
    }
    for _name, _txt in _bad.items():
        _exc2 = None
        try:
            json.loads(_txt.split(_M, 1)[1].strip())
        except Exception as exc:                   # noqa: BLE001
            _exc2 = exc
        check("%s：json 确实非法" % _name, _exc2 is not None)
        check("%s -> 吞（True）" % _name,
              P._leaked_partial_droppable(_txt, _ALLOWED) is True)

    print()
    print("[3] 防线：Extra data（合法数组 + 尾随文本）不吞")
    _extra = [
        _M + '\n[{"name": "shell", "arguments": "{}"}] 以上就是这次调用。',
        _M + '\n[1, 2, 3] 他说过。',
    ]
    for _txt in _extra:
        check("尾随散文不吞（False）：%r" % _txt[-18:],
              P._leaked_partial_droppable(_txt, _ALLOWED) is False)

    print()
    print("[4] 防线：完整合法数组不吞（fail-open 语义，交给 parse 层定夺）")
    _ok_arr = (_M + '\n[{"name": "shell", "arguments": '
               '"{\\"command\\": \\"pwd\\"}"}]')
    _exc4 = None
    try:
        json.loads(_ok_arr.split(_M, 1)[1].strip())
    except Exception as exc:                       # noqa: BLE001
        _exc4 = exc
    check("对照样本 json 合法（自校验）", _exc4 is None, _exc4)
    check("合法数组 -> 不吞（False）",
          P._leaked_partial_droppable(_ok_arr, _ALLOWED) is False)

    print()
    print("[5] 防线：形态/前置不满足不吞")
    check("marker 不在开头（讨论散文）-> False",
          P._leaked_partial_droppable(
              '用户说 [assistant 请求调用工具] 表示工具调用', _ALLOWED)
          is False)
    check("marker 后是散文 -> False",
          P._leaked_partial_droppable(_M + '\n我打算调用 shell。', _ALLOWED)
          is False)
    check("纯 marker -> True（退化回声）",
          P._leaked_partial_droppable(_M, _ALLOWED) is True)
    check("marker 后只有空白 -> True",
          P._leaked_partial_droppable(_M + ' \n ', _ALLOWED) is True)
    check("未声明 tools（None）-> False",
          P._leaked_partial_droppable(_SAMPLE, None) is False)
    check("未声明 tools（空集）-> False",
          P._leaked_partial_droppable(_SAMPLE, set()) is False)

    print()
    print("[6] 既有语义：截断形态与 fail-open 界线")
    check("未闭合字符串截断 -> 吞（True）",
          P._leaked_partial_droppable(
              _M + '\n[{"name": "shell", "arguments": "{\\"command\\": \\"pw',
              _ALLOWED) is True)
    check("未闭合 + 错误点中部（裸 token）-> 不吞（False，fail-open 界线）",
          P._leaked_partial_droppable(
              _M + '\n[{"name": shell', _ALLOWED) is False)

    print()
    print("[7] nonstream 集成：aggregate_stream 收尾吞掉（信封流）")
    _c1 = json.dumps({"id": "c", "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": _SAMPLE},
         "finish_reason": None}]})
    _c2 = json.dumps({"id": "c", "choices": [
        {"index": 0, "delta": {}, "finish_reason": "stop"}]})
    _agg = P.aggregate_stream(_Stream([_envelope(_c1), _envelope(_c2)]),
                              "m", None, holder={}, allowed_names=_ALLOWED)
    _msg = _agg["choices"][0]["message"]
    check("坏数组回声被吞：content 清空、无 tool_calls、finish=stop",
          _msg.get("content") == "" and not _msg.get("tool_calls")
          and _agg["choices"][0]["finish_reason"] == "stop", _msg)
    _prose = "先看下目录。\n\n" + _SAMPLE
    _c1b = json.dumps({"id": "c", "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": _prose},
         "finish_reason": None}]})
    _agg2 = P.aggregate_stream(_Stream([_envelope(_c1b), _envelope(_c2)]),
                               "m", None, holder={}, allowed_names=_ALLOWED)
    _msg2 = _agg2["choices"][0]["message"]
    check("散文 + 坏数组：散文保留（含标记前换行原样）、回声块被吞",
          _msg2.get("content") == "先看下目录。\n\n"
          and not _msg2.get("tool_calls"), _msg2)

    print()
    print("[8] 流式集成：坏数组回声整块/分帧都撑到流末，收尾吞掉")
    _out8 = b"".join(P.recover_leaked_tool_calls(
        [_dc(_M + "\n" + _SAMPLE.split(_M, 1)[1].lstrip())],
        allowed_names=_ALLOWED)).decode("utf-8")
    check("整块单帧：收尾判定吞掉，零输出（无 marker、无数组残渣）",
          _out8 == "", _out8[:200])
    # 残余 #3 修复（2026-10-16）：分帧到达的坏转义块此前在坏字符帧即被
    # _still_leak_candidate 证伪、mid-block 补发为正文；现在结构性非法的
    # 数组体保持 hold，与整块单帧同判：流末按 issue #9 吞掉。
    _block = _M + "\n" + _SAMPLE.split(_M, 1)[1].lstrip()
    _split_frames = [_dc(_block[i:i + 7]) for i in range(0, len(_block), 7)]
    _frames8b = [json.loads(f[6:]) for f in P.recover_leaked_tool_calls(
        _split_frames + [_dc("", "stop")], allowed_names=_ALLOWED)]
    _txt8b = "".join(f["choices"][0]["delta"].get("content") or ""
                     for f in _frames8b)
    check("分帧到达（7 字符/帧）：零正文、无 marker、无 tool_calls",
          _txt8b == "" and P.LEAK_MARKER not in _txt8b
          and not any(f["choices"][0]["delta"].get("tool_calls")
                      for f in _frames8b), _txt8b[:150])
    check("分帧到达：finish 保持 stop（吞掉而非还原）",
          _frames8b[-1]["choices"][0]["finish_reason"] == "stop",
          _frames8b[-1])

    print()
    print("[9] 边界：Extra data 仍即时证伪；未闭合截断维持 fail-open 补发")
    _frames9a = [json.loads(f[6:]) for f in P.recover_leaked_tool_calls(
        [_dc(_M + "\n[1, 2"), _dc(", 3] 这是正文"), _dc("", "stop")],
        allowed_names=_ALLOWED)]
    _txt9a = "".join(f["choices"][0]["delta"].get("content") or ""
                     for f in _frames9a)
    check("Extra data（合法数组+散文）：即时证伪、正文逐字节补发",
          _txt9a == _M + "\n[1, 2, 3] 这是正文"
          and _frames9a[-1]["choices"][0]["finish_reason"] == "stop",
          _txt9a)
    _frames9b = [json.loads(f[6:]) for f in P.recover_leaked_tool_calls(
        [_dc(_M + '\n[{"name": shell'), _dc("", "stop")],
        allowed_names=_ALLOWED)]
    _txt9b = "".join(f["choices"][0]["delta"].get("content") or ""
                     for f in _frames9b)
    check("未闭合 + 中部错误：流末不吞（fail-open 界线），整段补发",
          _txt9b == _M + '\n[{"name": shell', _txt9b)
    check("单元：结构性非法数组体保持 hold（True）",
          P._still_leak_candidate(
              _M + "\n" + _SAMPLE.split(_M, 1)[1].strip()) is True)
    check("单元：Extra data 即时证伪（False）",
          P._still_leak_candidate(_M + "\n[1, 2, 3] 这是正文") is False)
    check("单元：marker 后散文证伪（False）",
          P._still_leak_candidate(_M + "\n我打算调用 shell。") is False)
finally:
    shutil.rmtree(_tmp_guard, ignore_errors=True)

finish()

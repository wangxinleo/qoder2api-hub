# ---- 指纹行（与前端 _test_dashboard_*.js 同格式；实现见 tests/_suite_head.py）----
import os as _os_fp
import sys as _sys_fp
_sys_fp.path.insert(0, _os_fp.path.dirname(_os_fp.path.abspath(__file__)))
from _suite_head import declare_suite
declare_suite(__file__, ["qoder_proxy.py"])
# -*- coding: utf-8 -*-
"""工具历史清洁守卫套件（裸空 assistant 不得送达上游）。

    python tests/_test_flatten_guard.py

背景：切换模型战役（2026-10-16 E2E 实测）——同一上下文里若历史含 content 为空的
assistant 消息（reasoning-only 回合、流中断残留都会留下它），Kimi 族上游以 400
"the message at position N with role 'assistant' must not be empty" 拒绝**整条**
请求（DeepSeek 族则接受）。守卫：flatten_messages 丢弃「无 tool_calls + content
判空」的 assistant；DeepSeek 族 keep_reasoning 且 reasoning_content 非空的形态
维持原样（多轮一致性依赖该字段，见 flatten_messages docstring）。
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

# usage/ 落盘隔离到临时目录：仓库里的 usage/ 在并发编排下是跨进程共享路径，
# 套件不得往里写（见 tests/README.md「迁移期纪律」第 3 条）。
_tmp_guard = tempfile.mkdtemp(prefix="qd-flatten-guard-")
os.environ["QD_PROXY_USAGE_DIR"] = _tmp_guard

P, A = bootstrap()


def _flat(msgs, **kw):
    return P.flatten_messages(msgs, **kw)[1]


def _roles(flat):
    return [m["role"] for m in flat]


try:
    print("[1] 裸空 assistant 一律丢弃（structured 两态）")
    _base = [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": ""},
             {"role": "user", "content": "继续"}]
    for _st in (True, False):
        _f = _flat(_base, structured=_st)
        check("structured=%s：空 assistant 不出现在 flat" % _st,
              _roles(_f) == ["user", "user"], _roles(_f))

    print()
    print("[1b] 判空的三种写法：缺省键 / 纯空白 / None")
    _variants = {
        "content 缺省": {"role": "assistant"},
        "content 纯空白": {"role": "assistant", "content": "  \n\t "},
        "content=None": {"role": "assistant", "content": None},
    }
    for _name, _m in _variants.items():
        _f = _flat([{"role": "user", "content": "hi"}, _m,
                    {"role": "user", "content": "继续"}])
        check("%s -> 丢弃（roles=%s）" % (_name, _roles(_f)),
              _roles(_f) == ["user", "user"], _roles(_f))

    print()
    print("[2] DeepSeek 族（keep_reasoning）不受误伤")
    _ds = {"role": "assistant", "content": "",
           "reasoning_content": "用户打招呼，我应该回应。"}
    _f_on = _flat([{"role": "user", "content": "hi"}, _ds],
                  keep_reasoning=True)
    check("reasoning 非空 + keep_reasoning=True -> 保留且带字段",
          len(_f_on) == 2 and _f_on[1].get("reasoning_content")
          == "用户打招呼，我应该回应。", _f_on)
    _f_on_blank = _flat(
        [{"role": "user", "content": "hi"},
         {"role": "assistant", "content": "", "reasoning_content": "   "}],
        keep_reasoning=True)
    check("reasoning 纯空白 + keep_reasoning=True -> 丢弃",
          _roles(_f_on_blank) == ["user"], _roles(_f_on_blank))
    _f_off = _flat([{"role": "user", "content": "hi"}, _ds],
                   keep_reasoning=False)
    check("同一消息 keep_reasoning=False（Kimi 族）-> 丢弃",
          _roles(_f_off) == ["user"], _roles(_f_off))

    print()
    print("[3] 不误伤：带 tool_calls 的空 content assistant 保留（两态）")
    _tc = [{"role": "user", "content": "hi"},
           {"role": "assistant", "content": "", "tool_calls": [
               {"id": "c1", "type": "function",
                "function": {"name": "shell",
                             "arguments": '{"command": "pwd"}'}}]},
           {"role": "tool", "tool_call_id": "c1", "content": "/x"}]
    _f_s = _flat(_tc, structured=True)
    check("structured=True：assistant+tool 配对保留",
          _roles(_f_s) == ["user", "assistant", "tool"]
          and _f_s[1]["tool_calls"][0]["function"]["name"] == "shell",
          _f_s)
    _f_t = _flat(_tc, structured=False)
    check("structured=False：文本化块（LEAK_MARKER）保留",
          _roles(_f_t) == ["user", "assistant", "user"]
          and P.LEAK_MARKER in _f_t[1]["content"], _f_t)

    print()
    print("[4] 不误伤：空 user 与有正文的 assistant 原样保留")
    _f_u = _flat([{"role": "user", "content": ""},
                  {"role": "user", "content": "继续"}])
    check("空 user 不丢（本守卫只针对 assistant）", _roles(_f_u) == ["user", "user"],
          _roles(_f_u))
    _f_a = _flat([{"role": "user", "content": "hi"},
                  {"role": "assistant", "content": "往上看"}])
    check("有正文的 assistant 原样保留",
          _roles(_f_a) == ["user", "assistant"]
          and _f_a[1]["content"] == "往上看", _f_a)

    print()
    print("[5] 混合序列：空 assistant 夹在配对工具轮次之间不破坏对齐")
    _mix = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": "先看目录"},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c9", "type": "function",
                 "function": {"name": "shell",
                              "arguments": '{"command": "ls"}'}}]},
            {"role": "tool", "tool_call_id": "c9", "content": "a b c"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "继续"}]
    _f_mix = _flat(_mix, structured=True)
    check("混合序列仅裸空被摘除、配对不断裂",
          _roles(_f_mix) == ["user", "assistant", "assistant", "tool", "user"],
          _roles(_f_mix))
finally:
    finish()

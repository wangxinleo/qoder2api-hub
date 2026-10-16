"""账户分配公平性套件（R3：代表号 pick 白耗轮询 cursor）。

    python tests/_test_alloc_parity.py

背景（task 10-16 · research/rotation-cursor-parity-bug.md）：open_upstream 每次请求
都先做一次 `representative = POOL.pick(realm=realm)`（结果只进请求体占位的
aliyun_user_type，发送前被真实服务号覆写）→ 白耗一格**全局轮询 cursor** →
偶数账号池下"服务号"永远落在固定奇偶半边、一半账号服务数恒 0（N=4 -> [0,500,0,500]）。

本套件钉两件事：
  t1 池级：偶数 N∈{2,4,6,8}、K=400 次新会话 pick_for_session -> 各号计数偏差 <= 1；
  t2 代理级（open_upstream 完整链路，桩 urlopen 恒成功）：
     (a) POOL.pick 桩为 raise：存量会话（已绑定、不需要新选号）路径下
         open_upstream 不得再直接调 pick —— 4426 回退即必红；
     (b) N=4 池、400 个互异新会话走完整 open_upstream -> 各号计数偏差 <= 1。

夹具纪律（tests/README.md）：零出网（urlopen 桩恒成功）；ACCOUNTS_DIR /
QD_PROXY_USAGE_DIR 指向 tempdir；P.POOL / urlopen 改完全部 try/finally 还原。
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_TMP = tempfile.mkdtemp(prefix="qd-alloc-parity-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_TMP, "usage")
os.environ["QD_NATIVE_IDENTITY"] = "0"

from _suite_head import *          # noqa: E402,F401,F403
P, A = bootstrap()

declare_suite(__file__, ["qoder_accounts.py", "qoder_proxy.py"])
print("[alloc-parity] R3：代表号 pick 白耗 cursor 的公平性回归")


def _pool(uids):
    pool = A.AccountPool(os.path.join(_TMP, "acc-" + "-".join(uids)))
    for uid in uids:
        pool.add(A.Account({"uid": uid, "realm": "cn",
                            "accessToken": "dt-test-" + uid,
                            "refreshToken": "drt-test",
                            "expiresAt": 9999999999}))
    return pool


def _skew(counts):
    return max(counts.values()) - min(counts.values())


class _FakeResp(object):
    """urlopen 桩：与 _test_qoder.py:425-428 同款（进入/退出/read 全成功）。"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return b"{}"


def _urlopen_ok(req, timeout=None):
    return _FakeResp()


_ORIG_URLOPEN = P.urllib.request.urlopen
_ORIG_POOL = P.POOL

try:
    # ------------------------------------------------------------------ t1
    # 池级公平：只走 pick_for_session（互异 session_key = 互异新会话）。
    for n in (2, 4, 6, 8):
        pool = _pool(["par-t1-%d-%d" % (n, i) for i in range(n)])
        counts = {a.uid: 0 for a in pool.accounts}
        for i in range(400):
            account = pool.pick_for_session(realm="cn",
                                            session_key="t1-%d-%d" % (n, i),
                                            model="m")
            check_uid = getattr(account, "uid", None)
            if check_uid in counts:
                counts[check_uid] += 1
        check("t1 N=%d：400 次新会话 pick_for_session -> 各号计数偏差 <= 1"
              % n,
              sum(counts.values()) == 400 and _skew(counts) <= 1, counts)

    # ------------------------------------------------------------------ t2
    # 代理级 (a)：存量会话 + pick 桩为 raise -> open_upstream 不得再直接调 pick。
    pool = _pool(["par-t2a-0", "par-t2a-1"])
    pool.affinity.bind("t2a-session", pool.accounts[0].uid)
    pick_calls = []

    def _pick_guard(*args, **kwargs):
        pick_calls.append((args, kwargs))
        raise AssertionError("open_upstream 不得直接调用 POOL.pick（R3 回归）")

    pool.pick = _pick_guard
    P.POOL = pool
    P.urllib.request.urlopen = _urlopen_ok
    err = None
    used = None
    try:
        _resp, used, _encoded = P.open_upstream(
            {"model": "qfmodel",
             "messages": [{"role": "user", "content": "hello"}],
             "stream": False}, session_key="t2a-session", target_realm="cn")
    except Exception as exc:            # noqa: BLE001 - 红/绿都归到断言里
        err = exc
    check("t2a 存量会话（已绑定）：POOL.pick 桩为 raise 时 open_upstream 不炸、"
          "不再直接调 pick（4426 回退即必红）",
          err is None and getattr(used, "uid", None) == "par-t2a-0"
          and not pick_calls,
          (repr(err), getattr(used, "uid", None), len(pick_calls)))

    # 代理级 (b)：N=4、400 个互异新会话走完整 open_upstream -> 计数偏差 <= 1。
    pool = _pool(["par-t2b-%d" % i for i in range(4)])
    P.POOL = pool
    served = []
    err = None
    try:
        for i in range(400):
            _resp, used, _encoded = P.open_upstream(
                {"model": "qfmodel",
                 "messages": [{"role": "user", "content": "hi-%d" % i}],
                 "stream": False}, target_realm="cn")
            served.append(getattr(used, "uid", None))
    except Exception as exc:            # noqa: BLE001
        err = exc
    counts = {"par-t2b-%d" % i: served.count("par-t2b-%d" % i)
              for i in range(4)}
    check("t2b N=4：400 次互异新会话走完整 open_upstream -> 各号计数偏差 <= 1"
          "（修复前为 [0,200,0,200]）",
          err is None and len(served) == 400 and _skew(counts) <= 1,
          (repr(err), counts))
finally:
    P.urllib.request.urlopen = _ORIG_URLOPEN
    P.POOL = _ORIG_POOL
    shutil.rmtree(_TMP, ignore_errors=True)

finish()

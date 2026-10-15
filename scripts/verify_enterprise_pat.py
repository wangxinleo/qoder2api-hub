#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业版 PAT 独立验证脚本（Qoder CN 私有化 VPC）

不启动网关服务，直接用网关同款代码路径验证一个企业 PAT 是否可用：
  1) 企业域名归一化与三类端点派生（qoder_enterprise）
  2) PAT -> jobToken 兑换 + userinfo（AccountPool.import_pat，账号保持纯内存）
  3) 配额查询（Account.fetch_credits -> {instance}-openapi.vpc.qoder.com.cn）
  4) 可选 --chat：经网关同款 open_upstream 向 {instance}-gateway 发一条最小对话

用法：
  python scripts/verify_enterprise_pat.py --domain <实例名或企业域名> [--pat <PAT>] [--chat]

  --domain   也可通过环境变量 QODER_ENTERPRISE_DOMAIN 提供（如 acme.vpc.qoder.com.cn）
  --pat      缺省时隐藏交互输入；PAT 不回显、不打印全文
  --chat     额外做一次最小对话调用（推荐最终验收时使用）
  --model    --chat 使用的模型（默认 lite）
  --timeout  单次等待超时秒数（默认 30；对话为整体等待上限）
  --no-proxy 忽略 shell 的 HTTP(S)_PROXY 环境变量直连（代理干扰 TLS 时使用）

PAT 全程不落盘：账号对象保持纯内存（Account.save 空转）；ACCOUNTS_DIR 指向临时目录仅作防御性兜底，退出前整目录删除。
退出码：0 = 必需环节全过；1 = 任一失败（错误信息含具体 host / HTTP 状态）。
"""
from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys
import tempfile
import threading

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# ACCOUNTS_DIR 必须在导入 qoder_accounts 之前定向到临时目录：作为 PAT 不落盘
# 的防御性双保险（正式拦截见下方 Account.save 空转），且绝不碰真实账号库
_TMP = tempfile.mkdtemp(prefix="qd-verify-enterprise-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")

from qoder_enterprise import enterprise_origins, normalize_vpc_domain  # noqa: E402
import qoder_accounts as A                                            # noqa: E402
import qoder_proxy as P                                               # noqa: E402

# PAT 不落盘（同参考脚本）：import_pat -> add 的唯一落盘点是 Account.save；
# 空转它，账号对象全程纯内存。ACCOUNTS_DIR 指向临时目录仅作防御性双保险。
A.Account.save = lambda self, directory=None: None


def _mask(value):
    value = str(value or "")
    return "%s…(%d chars)" % (value[:6], len(value)) if value else "(空)"


def chat_smoke(model, timeout):
    """经 open_upstream（网关生产对话链路）向企业 gateway 发一条最小请求。"""
    out = {}

    def _worker():
        try:
            resp, _acc, _enc = P.open_upstream(
                {"model": model, "stream": True,
                 "messages": [{"role": "user", "content": "请只回复两个字符：OK"}]},
                target_realm="cn")
            out["chunk"] = resp.read(4096) or b""
        except Exception as exc:
            out["error"] = exc

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return False, "等待 %gs 超时（上游首包未达）" % timeout
    if "error" in out:
        return False, "对话请求失败：%r" % (out["error"],)
    chunk = out.get("chunk") or b""
    head = chunk.decode("utf-8", "replace").strip().replace("\n", " ")[:120]
    if not chunk:
        return False, "上游未返回数据"
    return True, "收到 %d 字节；片段: %r" % (len(chunk), head)


def main():
    parser = argparse.ArgumentParser(description="Qoder CN 企业版 PAT 独立验证")
    parser.add_argument("--domain", default="",
                        help="企业实例名或域名（如 acme 或 acme.vpc.qoder.com.cn）")
    parser.add_argument("--pat", default="", help="企业 PAT；缺省时隐藏输入")
    parser.add_argument("--chat", action="store_true",
                        help="额外发送一条最小对话验证 gateway 链路")
    parser.add_argument("--model", default="lite", help="--chat 使用的模型（默认 lite）")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="单次等待超时秒数（默认 30）")
    parser.add_argument("--no-proxy", action="store_true",
                        help="忽略 HTTP(S)_PROXY 直连（代理干扰 TLS 时使用）")
    args = parser.parse_args()

    if args.no_proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                    "ALL_PROXY", "all_proxy"):
            os.environ.pop(key, None)

    domain_input = (args.domain or os.getenv("QODER_ENTERPRISE_DOMAIN", "")).strip()
    if not domain_input:
        print("[FAIL] 缺少企业域名：--domain <域名> 或环境变量 QODER_ENTERPRISE_DOMAIN")
        return 1
    try:
        instance = normalize_vpc_domain(domain_input)
    except ValueError as exc:
        print("[FAIL] 企业域名无效：%s" % exc)
        return 1
    origins = enterprise_origins(instance)
    print("[OK] 域名解析：%s -> 实例名 %s" % (domain_input, instance))
    print("     openapi = %s" % origins["openapi"])
    print("     gateway = %s" % origins["gateway"])
    print("     portal  = %s（仅 PAT 获取页文案，不入请求链路）" % origins["portal"])

    pat = (args.pat or getpass.getpass("企业 PAT（输入不回显）: ")).strip()
    if not pat:
        print("[FAIL] PAT 为空")
        return 1
    if not pat.startswith("pt-"):
        print("[FAIL] PAT 必须以 pt- 开头（Personal Access Token）")
        return 1

    pool = A.AccountPool(os.path.join(_TMP, "accounts"))
    try:
        account = pool.import_pat(pat, enterprise_domain=instance)
    except Exception as exc:
        print("[FAIL] PAT 兑换失败：%s" % exc)
        return 1
    print("[OK] PAT 兑换成功（jobToken/exchange + userinfo，host=%s）" % origins["openapi"])
    print("     uid=%s  nickname=%s  realm=%s  enterprise=%s"
          % (account.uid, account.nickname or "(无)", account.realm,
             account.enterprise_domain))
    print("     token=%s  refresh_token=%s"
          % (_mask(account.access_token), _mask(account.refresh_token)))
    if not account.refresh_token:
        print("[WARN] 未返回 refresh_token：令牌过期后无法自动刷新")

    cred = account.fetch_credits()
    if cred.get("ok"):
        c = account.credits or {}
        print("[OK] 配额查询：剩余 %s / 总量 %s（used=%s, exhausted=%s）"
              % (c.get("remain"), c.get("size"), c.get("used"),
                 bool(c.get("exceeded"))))
    else:
        print("[FAIL] 配额查询失败：%s（host=%s；企业端点需要 Cosy-Version/Cosy-ClientType，"
              "若为 HTTP 4xx 请核对实例状态）"
              % (cred.get("error"), origins["openapi"]))
        return 1

    if args.chat:
        _orig_pool = P.POOL
        P.POOL = pool
        try:
            chat_ok, chat_msg = chat_smoke(args.model, args.timeout)
        finally:
            P.POOL = _orig_pool
        print(("[OK] " if chat_ok else "[FAIL] ") + "对话验证（%s via %s）：%s"
              % (args.model, origins["gateway"], chat_msg))
        if not chat_ok:
            return 1
    else:
        print("[SKIP] 对话验证未执行（加 --chat 开启；对话经 %s 的 COSY SSE）"
              % origins["gateway"])

    print("")
    print("结论：企业 PAT 可用。")
    if not args.chat:
        print("建议追加 --chat 完成端到端验证（兑换 -> 配额 -> 对话全链路）。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        # PAT 永不留在磁盘上：临时账号目录整目录删除
        shutil.rmtree(_TMP, ignore_errors=True)

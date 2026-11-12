"""企业 VPC 账号跳过签到/活动；token 保活保持正常。"""
import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import ROOT, bootstrap, check, declare_suite, finish

os.environ.pop("QD_CN_VPC_ENDPOINT", None)
P, A = bootstrap()
declare_suite(__file__, ["qoder_accounts.py", "qoder_tasks.py",
                         "qoder_scheduler.py"])

import qoder_scheduler  # noqa: E402
import qoder_tasks  # noqa: E402


def account(uid="vpc-a", endpoint="fixture-a", **extra):
    data = {"uid": uid, "realm": "cn", "accessToken": "dt-fixture",
            "refreshToken": "drt-fixture"}
    if endpoint is not None:
        data["vpcEndpoint"] = endpoint
    data.update(extra)
    return A.Account(data)


def rejected(call):
    try:
        call()
    except ValueError:
        return True
    return False


vpc = account()
public = account("public", "")
legacy = A.Account({"uid": "legacy", "realm": "cn",
                    "domain": "fixture-a.vpc.qoder.com.cn",
                    "accessToken": "dt-legacy", "refreshToken": "drt-legacy"})

print("\n[vpc-skip] 1) 判定与上游语义")
check("显式实例判定为 VPC", vpc.is_vpc)
check("旧 domain 迁移判定为 VPC", legacy.is_vpc)
check("显式公网不是 VPC", not public.is_vpc)
check("checkin_eligible 保持上游语义", vpc.checkin_eligible())
check("公网 checkin_eligible 为真", public.checkin_eligible())

print("\n[vpc-skip] 2) 签到/活动/Pro 全链路零出站")
with mock.patch.object(A, "http_json",
                       side_effect=AssertionError("VPC must not egress")):
    for label, acc in (("explicit", vpc), ("legacy", legacy)):
        check(label + " can_checkin false", acc.can_checkin() is False)
        ok, st = acc.checkin_status()
        check(label + " status unavailable",
              ok is False and st.get("reason") == A.VPC_SKIP_REASON)
        res = acc.checkin()
        check(label + " checkin skipped",
              bool(res.get("ok")) and res.get("skipped_vpc") is True)
        check(label + " no lastCheckin stamp", not acc.last_checkin)
        camp = acc.campaigns(force=True)
        check(label + " campaigns skipped",
              camp.get("skipped_vpc") is True and camp.get("campaigns") == [])
        check(label + " reward skipped",
              acc.campaign_reward("c1").get("skipped_vpc") is True)
        check(label + " claim skipped",
              acc.claim_campaign("c1").get("skipped_vpc") is True)
        cc = acc.campaign_checkin()
        check(label + " campaign checkin skipped",
              cc.get("skipped_vpc") is True and cc.get("earned") == 0)
        ach = acc.fetch_achievements(force=True)
        check(label + " achievements skipped",
              ach.get("skipped_vpc") is True and ach.get("keys") == [])
        ok_e, elig = acc.pro_eligibility()
        check(label + " pro not eligible", ok_e is True and elig is False)
        check(label + " pro claim skipped",
              acc.pro_claim().get("skipped_vpc") is True)
        check(label + " run_checkin skipped",
              qoder_tasks.run_checkin(acc, gap=0).get("skipped_vpc") is True)
        check(label + " run_pro_claim skipped",
              qoder_tasks.run_pro_claim(acc).get("skipped_vpc") is True)

print("\n[vpc-skip] 3) 公网正向对照")
with mock.patch.object(A, "http_json",
                       side_effect=RuntimeError("network attempted")) as http:
    public.campaigns(force=True)
check("公网 campaigns 仍尝试网络", http.call_count > 0)

print("\n[vpc-skip] 4) 部署默认实例继承")
old_cn = A.REALM_CONFIGS["cn"]
try:
    parsed = A.parse_vpc_endpoint("fixture-a")
    cfg = dict(old_cn)
    cfg.update({"openapi": parsed["openapi"], "gateway": parsed["gateway"],
                "website": parsed["website"], "domain": parsed["domain"],
                "vpc_instance": parsed["instance"]})
    A.REALM_CONFIGS["cn"] = cfg
    default_acc = account("default-vpc", None)
    check("部署默认实例判定为 VPC", default_acc.is_vpc)
    check("部署默认实例来源", default_acc.vpc_source == "default")
finally:
    A.REALM_CONFIGS["cn"] = old_cn

print("\n[vpc-skip] 5) Token 保活不受影响")
vpc.expires_at = time.time() + 60
public.expires_at = time.time() + 60
pool = type("Pool", (), {"accounts": [vpc, public], "dir": ""})()
with mock.patch.object(vpc, "refresh", return_value=True) as vpc_refresh, \
        mock.patch.object(public, "refresh", return_value=True) as pub_refresh, \
        mock.patch.object(qoder_tasks.time, "sleep", lambda *_: None):
    ka = qoder_tasks.run_keepalive(pool)
check("保活刷新 VPC", vpc_refresh.called)
check("保活刷新公网", pub_refresh.called)
check("保活计数为 2", ka.get("refreshed") == 2)

print("\n[vpc-skip] 6) 严格输入")
check("拒绝 scheme", rejected(
    lambda: A.parse_vpc_endpoint("https://fixture-a.vpc.qoder.com.cn")))
check("拒绝 scheme + slash", rejected(
    lambda: A.parse_vpc_endpoint("https://fixture-a.vpc.qoder.com.cn/")))
check("拒绝 path", rejected(lambda: A.parse_vpc_endpoint("fixture-a/path")))
check("拒绝 query", rejected(lambda: A.parse_vpc_endpoint("fixture-a?x=1")))
check("拒绝 port", rejected(lambda: A.parse_vpc_endpoint("fixture-a:8443")))
check("接受裸实例名",
      A.parse_vpc_endpoint("fixture-a")["instance"] == "fixture-a")
check("接受裸域名",
      A.parse_vpc_endpoint("fixture-a.vpc.qoder.com.cn")["instance"]
      == "fixture-a")
check("Account 拒绝 scheme", rejected(
    lambda: account(endpoint="https://fixture-a.vpc.qoder.com.cn")))

print("\n[vpc-skip] 7) 余额快照刷新开关")
with mock.patch.dict(os.environ, {}, clear=False):
    os.environ.pop("QD_VPC_CREDITS_REFRESH", None)
    check("余额刷新默认开启",
          qoder_scheduler.vpc_credits_refresh_enabled() is True)
with mock.patch.dict(os.environ, {"QD_VPC_CREDITS_REFRESH": "0"}):
    check("余额刷新可关闭",
          qoder_scheduler.vpc_credits_refresh_enabled() is False)
with mock.patch.dict(os.environ, {"QD_VPC_CREDITS_REFRESH": "1"}):
    check("余额刷新可开启",
          qoder_scheduler.vpc_credits_refresh_enabled() is True)

finish()

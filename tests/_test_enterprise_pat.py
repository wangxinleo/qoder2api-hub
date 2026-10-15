"""企业 PAT（Qoder CN 私有化 VPC）套件。

    python tests/_test_enterprise_pat.py

判据（PRD A1-A6/A8，全部拦在 http_json 这一层）：
  · A2 归一化表：收裸实例（大小写不敏感）/VPC 域/端点前缀/完整 URL，拒空/纯 zone/非 vpc/非法实例名
  · A1 企业导入：兑换 + userinfo 只走 {instance}-openapi，落盘含 enterpriseDomain + realm=cn
  · A3 无回退：兑换失败错误文本含企业 host，公共 openapi 零调用、不落库
  · A4 刷新 host：jrt- 刷新与 pt- 兜底重交换都走企业 openapi；公共 cn/intl 对照原 host
  · A5 对话与模型清单：企业账号只尝试 {instance}-gateway 单 host（无 api2/api3 类回退）
  · A6 签到/活动族：can_checkin False、run_checkin 明确跳过、/sash/ 零出站；
      配额/套餐走企业 openapi 且叠加 Cosy-Version/Cosy-ClientType
  · A8 JSON 导入白名单/非法拒收、导出 round-trip、旧文件（无键）加载、重导入合并保留

夹具纪律（tests/README.md）：http_json 第一优先桩掉并记录 URL；validate_public_http_url
一并桩掉（它内部会做 DNS 解析，夹具里不许真出网）；native_machine_identity 置空；
ACCOUNTS_DIR / QD_PROXY_USAGE_DIR 指向 tempdir；QD_NATIVE_IDENTITY=0；结尾打印出网计数。
"""
import os
import shutil
import socket
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_TMP = tempfile.mkdtemp(prefix="qd-enterprise-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["QD_PROXY_USAGE_DIR"] = os.path.join(_TMP, "usage")
os.environ["QD_NATIVE_IDENTITY"] = "0"

from _suite_head import *          # noqa: E402,F401,F403
P, A = bootstrap()
import qoder_tasks as T            # noqa: E402

declare_suite(__file__, ["qoder_enterprise.py", "qoder_accounts.py",
                         "qoder_proxy.py", "qoder_tasks.py"])
print("[enterprise-pat] 企业 VPC 专用端点 + 签到排除 + 导入导出")

# ---------------------------------------------------------------------------
# 夹具：桩 http_json（唯一网络出口）+ 记录；DNS/urlopen 泄漏计数器
# ---------------------------------------------------------------------------
CALLS = []      # 每次 http_json 的 {url, method, data, headers}
ROUTES = {}     # URL 后缀 -> 响应 dict | 要抛的异常实例
LEAK = {"urlopen": 0, "getaddrinfo": 0}

CN_OPENAPI = A.REALM_CONFIGS["cn"]["openapi"]
INTL_OPENAPI = A.REALM_CONFIGS["intl"]["openapi"]
ENT_OPENAPI = "https://acme-openapi.vpc.qoder.com.cn"
ENT_GATEWAY = "https://acme-gateway.vpc.qoder.com.cn"


def _route(suffix, val):
    ROUTES[suffix] = val


def _reset():
    del CALLS[:]


def _urls():
    return [c["url"] for c in CALLS]


def _http(url, data=None, method="GET", headers=None, timeout=None,
          retries=None, **kw):
    """http_json 桩：记录一切调用；未登记的 URL 直接抛错（漏桩立刻显形）。"""
    CALLS.append({"url": url, "method": method, "data": data,
                  "headers": dict(headers or {})})
    for suffix, val in ROUTES.items():
        if url.endswith(suffix):
            if isinstance(val, Exception):
                raise val
            return val
    raise AssertionError("unexpected http_json outbound: %s" % url)


A.http_json = _http
A.native_machine_identity = lambda *a, **k: {}
_ORIG_URLOPEN = P.urllib.request.urlopen
_ORIG_GETADDRINFO = socket.getaddrinfo


def _leak_urlopen(*a, **k):
    LEAK["urlopen"] += 1
    raise AssertionError("fixture leak: real urlopen reached")


def _leak_getaddrinfo(*a, **k):
    LEAK["getaddrinfo"] += 1
    raise AssertionError("fixture leak: real DNS lookup reached")


P.urllib.request.urlopen = _leak_urlopen
socket.getaddrinfo = _leak_getaddrinfo
P.validate_public_http_url = lambda url, allow_local=False: url


def _ent(uid, instance="acme", **extra):
    d = {"uid": uid, "realm": "cn", "enterpriseDomain": instance,
         "accessToken": "jt-x", "source": "pat"}
    d.update(extra)
    return A.Account(d)


def _pub(uid, realm="cn", **extra):
    d = {"uid": uid, "realm": realm, "accessToken": "jt-pub", "source": "oauth"}
    if realm == "intl":
        d["domain"] = "qoder.com"
    d.update(extra)
    return A.Account(d)


def _cjk(s):
    return bool(re.search(r"[\u4e00-\u9fff]", str(s or "")))


# ---------------------------------------------------------------- A2 归一化表
ACCEPT = [
    ("acme", "acme"),
    ("acme.vpc.qoder.com.cn", "acme"),
    ("acme-openapi.vpc.qoder.com.cn", "acme"),
    ("acme-gateway.vpc.qoder.com.cn", "acme"),
    ("https://acme.vpc.qoder.com.cn/account/integrations", "acme"),
    ("  acme.vpc.qoder.com.cn  ", "acme"),
    ("ACME", "acme"),
    ("  ACME.VPC.QODER.COM.CN  ", "acme"),
]
for raw, want in ACCEPT:
    try:
        got = A.normalize_vpc_domain(raw)
        err = None
    except Exception as exc:
        got, err = None, exc
    check("A2 归一化接受 %r -> %r" % (raw, want),
          err is None and got == want, (raw, got, err))

REJECT = ["", "vpc.qoder.com.cn", "foo.bar.com", "acme-", "_bad"]
for raw in REJECT:
    try:
        got = A.normalize_vpc_domain(raw)
        err = None
    except ValueError as exc:
        got, err = None, exc
    except Exception as exc:
        got, err = None, exc
    check("A2 归一化拒绝 %r（中文 ValueError）" % raw,
          isinstance(err, ValueError) and _cjk(err), (raw, got, err))

origins = A.enterprise_origins("acme")
check("A2 三类 origin 派生（openapi/gateway/portal）",
      origins == {"openapi": ENT_OPENAPI, "gateway": ENT_GATEWAY,
                  "portal": "https://acme.vpc.qoder.com.cn"}, origins)
check("A2 公共账号 gateway_candidates_for == gateway_candidates（回归对照）",
      A.gateway_candidates_for(_pub("g-cn")) == A.gateway_candidates("cn")
      and A.gateway_candidates_for(_pub("g-intl", "intl"))
      == A.gateway_candidates("intl"))
check("A2 企业账号 gateway_candidates_for 仅单 host",
      A.gateway_candidates_for(_ent("g-ent")) == [ENT_GATEWAY],
      A.gateway_candidates_for(_ent("g-ent")))

# ------------------------------------------------------------------ A1 企业导入
_route(A.PATH_JOB_EXCHANGE, {"token": "jt-ent-1", "refresh_token": "jrt-ent-1",
                             "expires_in": 3600000})
_route(A.PATH_USERINFO, {"id": "ent-uid-1", "name": "Ent One",
                         "organization_id": "org-1",
                         "organization_name": "Acme Corp"})
P1 = os.path.join(_TMP, "p1")
pool1 = A.AccountPool(P1)
_reset()
acc1 = pool1.import_pat("pt-test-1", realm="intl",
                        enterprise_domain="acme.vpc.qoder.com.cn")
check("A1 兑换出站为企业 openapi + POST + body 原样",
      len(CALLS) >= 1 and CALLS[0]["url"] == ENT_OPENAPI + A.PATH_JOB_EXCHANGE
      and CALLS[0]["method"] == "POST"
      and CALLS[0]["data"] == json.dumps(
          {"personal_token": "pt-test-1"}).encode(), CALLS[:1])
check("A1 userinfo 同 base + Bearer jt-",
      len(CALLS) == 2 and CALLS[1]["url"] == ENT_OPENAPI + A.PATH_USERINFO
      and CALLS[1]["headers"].get("Authorization") == "Bearer jt-ent-1",
      CALLS[1:2])
check("A1 带域名 realm 强制 cn（入参 intl 被忽略）",
      acc1.realm == "cn" and acc1.enterprise_domain == "acme"
      and acc1.is_enterprise(), (acc1.realm, acc1.enterprise_domain))
check("A1 openapi_base/gateway 均为企业域",
      acc1.openapi_base() == ENT_OPENAPI,
      acc1.openapi_base())
with open(os.path.join(P1, "ent-uid-1.json"), encoding="utf-8") as fh:
    disk1 = json.load(fh)
check("A1 落盘 JSON：enterpriseDomain=acme 且 realm=cn",
      disk1.get("enterpriseDomain") == "acme" and disk1.get("realm") == "cn",
      {"enterpriseDomain": disk1.get("enterpriseDomain"),
       "realm": disk1.get("realm")})
check("A1 public() 暴露 enterpriseDomain",
      acc1.public().get("enterpriseDomain") == "acme",
      acc1.public().get("enterpriseDomain"))

# ------------------------------------------------------------- A3 无回退（企业）
P3 = os.path.join(_TMP, "p3")
pool3 = A.AccountPool(P3)
_reset()
_route(A.PATH_JOB_EXCHANGE, RuntimeError("simulated exchange failure"))
try:
    pool3.import_pat("pt-test-3", enterprise_domain="acme")
    err3 = None
except Exception as exc:
    err3 = str(exc)
check("A3 兑换失败 -> RuntimeError 文本含企业 host",
      err3 is not None and "acme-openapi.vpc.qoder.com.cn" in err3
      and "企业版 PAT 验证失败" in err3, err3)
check("A3 公共 openapi 零调用（企业分支绝不回退）",
      not any(u.startswith(CN_OPENAPI) or u.startswith(INTL_OPENAPI)
              for u in _urls()), _urls())
check("A3 失败不落库（账号池与目录均为空）",
      pool3.accounts == [] and not os.path.exists(
          os.path.join(P3, "ent-uid-1.json")))

# 域名非法：import_pat 在归一化阶段就抛中文 ValueError（路由层据此回 400 且不落库），
# 此时一个字节都不该出网（先算域名，再谈请求）。
P3B = os.path.join(_TMP, "p3b")
pool3b = A.AccountPool(P3B)
_reset()
try:
    pool3b.import_pat("pt-bad-domain", enterprise_domain="foo.bar.com")
    err3b = None
except Exception as exc:
    err3b = exc
check("A3 非法企业域名：中文 ValueError + 零出站 + 不落库",
      isinstance(err3b, ValueError) and _cjk(err3b) and CALLS == []
      and pool3b.accounts == [], (err3b, _urls(), pool3b.accounts))

# -------------------------------------------------------------- A4 刷新 host
e4 = _ent("r-ent-1", refreshToken="jrt-old", personalToken="pt-fb")
_reset()
_route(A.PATH_JOB_REFRESH, {"token": "jt-new", "refresh_token": "jrt-new"})
ok4 = e4.refresh()
check("A4 jrt- 刷新走企业 openapi",
      ok4 is True and _urls() == [ENT_OPENAPI + A.PATH_JOB_REFRESH],
      (ok4, _urls()))

e4b = _ent("r-ent-2", refreshToken="jrt-dead", personalToken="pt-fb2")
_reset()
_route(A.PATH_JOB_REFRESH, RuntimeError("refresh rejected"))
_route(A.PATH_JOB_EXCHANGE, {"token": "jt-via-pat",
                             "refresh_token": "jrt-via-pat"})
ok4b = e4b.refresh()
check("A4 jrt- 失败 -> pt- 兜底重交换同为该 host",
      ok4b is True and _urls() == [ENT_OPENAPI + A.PATH_JOB_REFRESH,
                                   ENT_OPENAPI + A.PATH_JOB_EXCHANGE],
      (ok4b, _urls()))

pubcn = _pub("r-cn", refreshToken="jrt-cn")
_reset()
_route(A.PATH_JOB_REFRESH, {"token": "jt-cn-2", "refresh_token": "jrt-cn-2"})
pubcn.refresh()
check("A4 对照：公共 cn 账号仍走 realm openapi",
      _urls() == [CN_OPENAPI + A.PATH_JOB_REFRESH], _urls())

pubintl = _pub("r-intl", "intl", refreshToken="jrt-intl")
_reset()
_route(A.PATH_JOB_REFRESH, {"token": "jt-intl-2", "refresh_token": "jrt-intl-2"})
pubintl.refresh()
check("A4 对照：公共 intl 账号仍走 realm openapi",
      _urls() == [INTL_OPENAPI + A.PATH_JOB_REFRESH], _urls())

# ------------------------------------------------------ A5 对话 / 模型清单 host
class _Resp(object):
    def __init__(self, body=b"", status=200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def close(self):
        pass

    # 模型清单路径用 `with urllib.request.urlopen(...) as resp:`，桩需上下文协议
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


HITS = []


def _recorder(body):
    def _fake(req, timeout=None, *a, **k):
        HITS.append(getattr(req, "full_url", str(req)))
        return _Resp(body)
    return _fake


pool5 = A.AccountPool(os.path.join(_TMP, "p5"))
ent5 = _ent("chat-ent")
pool5.accounts = [ent5]
_orig_pool = P.POOL
P.POOL = pool5
del HITS[:]
P.urllib.request.urlopen = _recorder(b"")
err5 = None
try:
    resp5, acc5, _enc5 = P.open_upstream(
        {"model": "qmodel", "stream": True,
         "messages": [{"role": "user", "content": "hi"}]},
        target_realm="cn")
except Exception as exc:
    resp5 = acc5 = None
    err5 = exc
finally:
    P.urllib.request.urlopen = _leak_urlopen
    P.POOL = _orig_pool
check("A5 对话：企业账号走企业 gateway，且只尝试单 host",
      err5 is None and acc5 is not None and acc5.uid == "chat-ent"
      and HITS == [ENT_GATEWAY + P.CHAT_PATH], (err5, HITS))

P.POOL = pool5
del HITS[:]
P.urllib.request.urlopen = _recorder(
    json.dumps({"chat": [{"key": "m1", "display_name": "M1"}]}).encode())
try:
    models5 = P.read_dynamic_models(realm="cn")
finally:
    P.urllib.request.urlopen = _leak_urlopen
    P.POOL = _orig_pool
check("A5 模型清单：企业 host + 仅单 host",
      HITS == [ENT_GATEWAY + P.MODELS_PATH], HITS)
check("A5 模型清单解析正常（单 host 成功即返回）",
      isinstance(models5, list) and models5 and models5[0][0] == "m1", models5)

# ------------------------------------------------- A6 签到/活动排除 + 配额 host
ent6 = _ent("ent6")
_reset()
check("A6 can_checkin 企业恒 False", ent6.can_checkin() is False, ent6.can_checkin())
sk6 = T.run_checkin(ent6)
check("A6 run_checkin 明确跳过（ok + skipped_enterprise + 日志说明）",
      sk6.get("ok") is True and sk6.get("skipped_enterprise") is True
      and any("企业版" in ln for ln in sk6.get("logs") or []), sk6)
ok6s, st6 = ent6.checkin_status()
check("A6 checkin_status 零出站 + enterprise 原因",
      ok6s is False and st6.get("reason") == A.CHECKIN_REASON_ENTERPRISE
      and _cjk(st6.get("error")), (ok6s, st6))
ci6 = ent6.checkin()
check("A6 checkin 返回 unavailable/skip 且零出站",
      ci6.get("ok") is True and ci6.get("unavailable") is True
      and ci6.get("reason") == A.CHECKIN_REASON_ENTERPRISE, ci6)
cget6 = ent6._campaigns_get()
check("A6 _campaigns_get 企业零出站",
      cget6[0] is None and cget6[1] == 0 and _cjk(cget6[2]), cget6)
camp6 = ent6.campaign_checkin(only_kinds=("", "CREDITS"))
check("A6 campaign_checkin 跳过（earned=0、无 claimed）",
      camp6.get("ok") is True and camp6.get("skipped_enterprise") is True
      and camp6.get("earned") == 0 and camp6.get("claimed") == [], camp6)
# 活动族其它入口一并扫进出网断言（campaigns 是 _campaigns_get 的缓存包装；
# campaign_reward 只读发放状态）——任一路径漏判企业都会让下面的 /sash/ 零出站断言红
ent6.campaigns()
ent6.campaign_reward("cid-not-used")
check("A6 Pro 包族企业零出站",
      ent6.pro_eligibility() == (False, A.ENTERPRISE_ACTIVITY_MSG)
      and ent6.pro_claim().get("ok") is False, (ent6.pro_eligibility(),
                                                ent6.pro_claim()))
check("A6 企业签到/活动族全程 /sash/ 零出站（http_json 未被调用）",
      CALLS == [], CALLS)

_reset()
_route(A.PATH_QUOTA, {"userQuota": {"total": 100, "used": 40, "remaining": 60},
                      "addOnQuota": {"total": 0, "used": 0, "remaining": 0}})
r6 = ent6.fetch_credits()
check("A6 fetch_credits 出站为企业 openapi + 正常解析",
      r6.get("ok") is True and (ent6.credits or {}).get("remain") == 60
      and _urls() == [ENT_OPENAPI + A.PATH_QUOTA], (r6, _urls()))
check("A6 企业配额请求叠加 Cosy-Version/Cosy-ClientType",
      CALLS[0]["headers"].get("Cosy-Version") == "1.0.1"
      and CALLS[0]["headers"].get("Cosy-ClientType") == "5",
      {k: v for k, v in CALLS[0]["headers"].items() if k.startswith("Cosy")})

_reset()
_route(A.PATH_PLAN, {"plan_tier_name": "Pro Enterprise"})
ent6.fetch_plan()
check("A6 fetch_plan 出站为企业 openapi",
      _urls() == [ENT_OPENAPI + A.PATH_PLAN], _urls())

pub6 = _pub("pub6")
_reset()
_route(A.PATH_QUOTA, {"userQuota": {"total": 10, "used": 1, "remaining": 9},
                      "addOnQuota": {"total": 0, "used": 0, "remaining": 0}})
pub6.fetch_credits()
check("A6 对照：公共账号配额仍走原 host 且无 Cosy 两头",
      _urls() == [CN_OPENAPI + A.PATH_QUOTA]
      and "Cosy-Version" not in CALLS[0]["headers"]
      and "Cosy-ClientType" not in CALLS[0]["headers"],
      ({k: v for k, v in CALLS[0]["headers"].items() if k.startswith("Cosy")},
       _urls()))

# -------------------------------------------- A8 JSON 导入/导出 + 旧文件 + 合并
kw8 = A.normalise_import_row({"accessToken": "kt-x", "uid": "row-ent",
                              "realm": "intl",
                              "enterpriseDomain": "acme.vpc.qoder.com.cn"})
check("A8 行导入：enterpriseDomain 归一化 + realm 强制 cn",
      kw8["enterpriseDomain"] == "acme" and kw8["realm"] == "cn",
      (kw8.get("enterpriseDomain"), kw8.get("realm")))
try:
    A.normalise_import_row({"accessToken": "kt-x",
                            "enterpriseDomain": "foo.bar.com"})
    err8 = None
except ValueError as exc:
    err8 = str(exc)
check("A8 非法域名该行 ValueError（中文拒收）", _cjk(err8), err8)

doc8 = A.build_export_document([acc1])
check("A8 导出文档携带 enterpriseDomain（格式不变）",
      doc8.get("format") == A.EXPORT_FORMAT and doc8.get("version") == 1
      and doc8["accounts"][0].get("enterpriseDomain") == "acme",
      doc8["accounts"][0].get("enterpriseDomain"))

pool8 = A.AccountPool(os.path.join(_TMP, "p8"))
rep8 = pool8.import_rows(doc8["accounts"])
imported8 = pool8.get("ent-uid-1")
check("A8 导出再导入 round-trip 不丢企业字段",
      rep8["added"] == ["ent-uid-1"] and imported8 is not None
      and imported8.enterprise_domain == "acme", (rep8, imported8))
again8 = A.build_export_document([imported8])["accounts"][0]
check("A8 二次导出字段仍在", again8.get("enterpriseDomain") == "acme", again8)

OLD_DIR = os.path.join(_TMP, "p_old")
os.makedirs(OLD_DIR, exist_ok=True)
with open(os.path.join(OLD_DIR, "old1.json"), "w", encoding="utf-8") as fh:
    json.dump({"uid": "old1", "realm": "cn", "accessToken": "jt-old",
               "refreshToken": "jrt-old", "enabled": True,
               "source": "oauth"}, fh)
pool_old = A.AccountPool(OLD_DIR)
pool_old.load()
old = pool_old.get("old1")
check("A8 旧格式（无 enterpriseDomain）加载不变：空串 + 非企业",
      old is not None and old.enterprise_domain == ""
      and old.is_enterprise() is False, old and old.enterprise_domain)
_reset()
_route(A.PATH_JOB_REFRESH, {"token": "jt-old-2", "refresh_token": "jrt-old-2"})
old.refresh()
check("A8 旧账号使用不受影响：刷新仍走公共 cn openapi",
      _urls() == [CN_OPENAPI + A.PATH_JOB_REFRESH], _urls())

PM = os.path.join(_TMP, "p_merge")
poolM = A.AccountPool(PM)
poolM.add(_ent("m1", accessToken="jt-a"))
poolM.add(A.Account({"uid": "m1", "realm": "cn", "accessToken": "jt-b"}))
check("A8 同 uid 重导入无域名 -> 保留既有 enterpriseDomain",
      poolM.get("m1").enterprise_domain == "acme",
      poolM.get("m1").enterprise_domain)
poolM.add(A.Account({"uid": "m1", "realm": "cn", "accessToken": "jt-c",
                     "enterpriseDomain": "other"}))
check("A8 同 uid 重导入显式新域名 -> 采用新值（不盲目保留）",
      poolM.get("m1").enterprise_domain == "other",
      poolM.get("m1").enterprise_domain)
poolM.add(A.Account({"uid": "pub-m", "realm": "cn", "accessToken": "jt-p"}))
check("A8 公共账号合并路径不变（空串保持空串）",
      poolM.get("pub-m").enterprise_domain == "", None)

# ---------------------------------------------------------------- 收尾自证
P.urllib.request.urlopen = _ORIG_URLOPEN
socket.getaddrinfo = _ORIG_GETADDRINFO
shutil.rmtree(_TMP, ignore_errors=True)
print("出网计数: 真实 urlopen=%d, 真实 DNS(getaddrinfo)=%d（期望 0/0）；"
      "http_json 桩内记录 %d 次" % (LEAK["urlopen"], LEAK["getaddrinfo"],
                                    len(CALLS)))
check("自证：夹具零真实出网（urlopen + DNS 均 0）",
      LEAK["urlopen"] == 0 and LEAK["getaddrinfo"] == 0, LEAK)

finish()

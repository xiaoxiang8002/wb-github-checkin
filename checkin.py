# -*- coding: utf-8 -*-
"""WorkBuddy 多账号每日签到 + 猫猫旅行闭环，可在任意环境运行（GitHub Actions / 本地）。

纯标准库实现，零第三方依赖。凭证来源（按优先级）：
  1. 环境变量 WB_ACCOUNTS：JSON 数组
     [{"name":"主号","token":"...","user_id":"...","endpoint":"https://www.workbuddy.cn"}, ...]
  2. 同目录 accounts.json（仅本地调试用，已被 .gitignore 忽略，禁止提交到仓库）

可选环境变量：
  WB_NOTIFY_URL         企业微信机器人或自定义 webhook，用于告警
  WB_TIMEOUT            单次 HTTP 超时秒数，默认 12
  WB_RETRY              网络错误 / 5xx 重试次数，默认 2
  WB_WARN_DAYS          凭证剩余天数低于此值时告警，默认 7
  WB_NOTIFY_ON_SUCCESS  "1" 时成功也推送简报
  WB_FAIL_ON_ERROR      "1" 时存在任何账号失败则以非 0 退出（让 Actions 标红）

每个账号独立执行完整闭环，单个账号失败不影响其它账号。
"""
import base64
import datetime as dt
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request

LOG = logging.getLogger("wb-claim")
LOG.setLevel(logging.INFO)
if not LOG.handlers:
    LOG.addHandler(logging.StreamHandler())

P_CHECKIN_STATUS = "/v2/billing/meter/checkin-activity-status"
P_CHECKIN_DO = "/v2/billing/meter/daily-checkin"
P_BUDDY_INFO = "/activity/growth/buddy/info"
P_TRAVEL_STATUS = "/activity/growth/buddy/travel/status"
P_TRAVEL_DEPART = "/activity/growth/buddy/travel/depart"
P_TRAVEL_CLAIM = "/activity/growth/buddy/travel/claim"

TZ8 = dt.timezone(dt.timedelta(hours=8))
LOCATIONS = {1: "咖啡馆", 2: "商场店铺", 3: "健身房", 4: "古镇客栈"}
DEFAULT_ENDPOINT = "https://www.workbuddy.cn"
ACCOUNTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.json")


class ClaimError(Exception):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def as_dict(self):
        return {"code": self.code, "message": self.message, "detail": str(self.detail)[:400]}


def _env(name, default=""):
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _jwt_payload(token):
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    p = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(p.encode("ascii")))
    except Exception:
        return {}


def _normalize(arr):
    out = []
    for i, a in enumerate(arr):
        if not isinstance(a, dict):
            continue
        token = str(a.get("token") or a.get("WB_ACCESS_TOKEN") or "")
        if not token:
            continue
        uid = str(a.get("user_id") or a.get("WB_USER_ID")
                  or _jwt_payload(token).get("sub") or "")
        out.append({
            "name": str(a.get("name") or ("账号%d" % (i + 1))),
            "token": token,
            "user_id": uid,
            "endpoint": str(a.get("endpoint") or DEFAULT_ENDPOINT),
        })
    return out


def load_accounts():
    raw = _env("WB_ACCOUNTS")
    if raw:
        try:
            arr = json.loads(raw)
            got = _normalize(arr if isinstance(arr, list) else arr.get("accounts", []))
            if got:
                return got
        except Exception as e:
            LOG.warning("WB_ACCOUNTS 解析失败：%r", e)
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, encoding="utf-8") as f:
                arr = json.load(f)
            got = _normalize(arr if isinstance(arr, list) else arr.get("accounts", []))
            if got:
                return got
        except Exception as e:
            LOG.warning("accounts.json 解析失败：%r", e)
    raise ClaimError("NO_ACCOUNTS", "未找到任何账号凭证（WB_ACCOUNTS / accounts.json）")


def _parse(raw):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"_raw": raw.decode("utf-8", "replace")[:300]}


def _http(method, path, account, body=None):
    """返回 (http_status, body_dict)。4xx 正常返回交由业务层判断，网络错误/5xx 重试。"""
    timeout = int(_env("WB_TIMEOUT", "12"))
    retry = int(_env("WB_RETRY", "2"))
    url = (account.get("endpoint") or DEFAULT_ENDPOINT).rstrip("/") + path
    headers = {
        "authorization": "Bearer " + account["token"],
        "x-user-id": account.get("user_id") or "",
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": "WorkBuddy-GitHub-Checkin/1.0",
    }
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    last = None
    for attempt in range(retry + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, _parse(r.read())
        except urllib.error.HTTPError as e:
            obj = _parse(e.read())
            if 500 <= e.code < 600 and attempt < retry:
                time.sleep(1 + attempt)
                continue
            return e.code, obj
        except Exception as e:  # URLError / timeout / socket
            last = e
            if attempt < retry:
                time.sleep(1 + attempt)
                continue
            raise ClaimError("NETWORK", "请求 %s %s 失败" % (method, path), repr(e))
    raise ClaimError("NETWORK", "请求 %s %s 失败" % (method, path), repr(last))


def _data(resp):
    if not isinstance(resp, dict):
        return {}
    d = resp.get("data")
    return d if isinstance(d, dict) else {}


def _fmt_ts(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts), TZ8).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def checkin(account):
    """幂等：code=0 领取成功；code=10001 / 含「已签到」视为今日已完成。"""
    status, resp = _http("POST", P_CHECKIN_DO, account)
    code = resp.get("code") if isinstance(resp, dict) else None
    msg = str(resp.get("msg", "")) if isinstance(resp, dict) else ""
    already = code == 10001 or "已签到" in msg
    if code != 0 and not already:
        raise ClaimError("CHECKIN_FAILED", "签到接口返回异常", {"http": status, "body": resp})

    st2, st_resp = _http("POST", P_CHECKIN_STATUS, account)
    d = _data(st_resp)
    if st_resp.get("code") not in (0, None):
        raise ClaimError("CHECKIN_STATUS_FAILED", "签到状态查询失败", {"http": st2, "body": st_resp})
    return {
        "ok": True,
        "already_checked_in": already,
        "streak_days": d.get("streak_days"),
        "today_credit": d.get("today_credit"),
        "total_credits": d.get("total_credits"),
        "active": d.get("active"),
        "msg": msg if already else "签到成功",
    }


def no_active_buddy(account, status_data):
    """travel/status 的 buddy_id=0 时，用只读的 buddy/info 确认该账号尚未领养猫猫。

    新账号没有猫猫时 depart 会返回 403 `no active buddy`，这是账号状态而非故障，
    应跳过派遣而不是判为失败。buddy/info 查询异常时返回 False，保持原有派遣行为。
    """
    if status_data.get("buddy_id"):
        return False
    try:
        _, resp = _http("GET", P_BUDDY_INFO, account)
        if resp.get("code") == 0:
            return not (_data(resp) or {}).get("buddy")
    except Exception:
        pass
    return False


def travel(account):
    """按状态处理：arrived -> 领奖；idle 且未达上限 -> 派出；traveling -> 报告到达时间。"""
    status, resp = _http("GET", P_TRAVEL_STATUS, account)
    if resp.get("code") != 0:
        raise ClaimError("TRAVEL_STATUS_FAILED", "旅行状态查询失败", {"http": status, "body": resp})
    d = _data(resp)
    state = d.get("state", "unknown")
    base = {"state": state, "daily_limit_reached": bool(d.get("daily_limit_reached"))}

    if state == "arrived":
        c_status, c_resp = _http("POST", P_TRAVEL_CLAIM, account, {})
        if c_resp.get("code") not in (0, None):
            raise ClaimError("TRAVEL_CLAIM_FAILED", "领取旅行奖励失败",
                             {"http": c_status, "body": c_resp})
        base.update({
            "action": "claim",
            "reward_credit": d.get("reward_credit"),
            "location": (d.get("location") or {}).get("name"),
        })
        return base

    if state == "idle":
        if no_active_buddy(account, d):
            base.update({"action": "skipped", "reason": "no_active_buddy"})
            return base
        if base["daily_limit_reached"]:
            base.update({"action": "noop", "reason": "daily_limit_reached"})
            return base
        lid = random.randint(1, 4)
        dep_status, dep_resp = _http("POST", P_TRAVEL_DEPART, account, {"location_id": lid})
        if dep_resp.get("code") not in (0, None):
            raise ClaimError("TRAVEL_DEPART_FAILED", "派遣猫猫失败",
                             {"http": dep_status, "body": dep_resp})
        base.update({"action": "depart", "location": LOCATIONS[lid], "location_id": lid})
        return base

    if state == "traveling":
        loc = d.get("location") or {}
        base.update({
            "action": "noop",
            "reason": "traveling",
            "location": loc.get("name"),
            "arrive_at": _fmt_ts(d.get("arrive_at")) if d.get("arrive_at") else None,
            "reward_credit": d.get("reward_credit"),
        })
        return base

    base.update({"action": "unknown_state"})
    return base


def token_health(account):
    payload = _jwt_payload(account["token"])
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return {"valid": True, "reason": "no exp claim", "expires_at": None, "days_left": None}
    now = time.time()
    days_left = round((exp - now) / 86400.0, 1)
    return {
        "valid": exp > now,
        "expires_at": _fmt_ts(exp),
        "days_left": days_left,
        "typ": payload.get("typ"),
    }


def run_account(account):
    """单账号完整闭环。任何异常都收敛到该账号的 errors，不中断其它账号。"""
    sub = {
        "name": account["name"], "user_id": account.get("user_id"),
        "ok": True, "checkin": None, "travel": None, "travel_second": None,
        "token_health": None, "errors": [],
    }
    try:
        sub["token_health"] = token_health(account)
        if not sub["token_health"]["valid"]:
            raise ClaimError("TOKEN_EXPIRED",
                             "凭证已过期，请登录该账号运行 renew_github.py 重新导出")
    except ClaimError as e:
        sub["errors"].append(e.as_dict())

    if not sub["errors"]:
        try:
            sub["checkin"] = checkin(account)
        except ClaimError as e:
            sub["errors"].append(e.as_dict())
    if not sub["errors"]:
        try:
            sub["travel"] = travel(account)
        except ClaimError as e:
            sub["errors"].append(e.as_dict())
    # 领奖后猫猫会变 idle：若当日派遣次数未用尽，立刻再派一轮。
    if not sub["errors"] and (sub["travel"] or {}).get("action") == "claim":
        try:
            sub["travel_second"] = travel(account)
        except ClaimError as e:
            sub["errors"].append(e.as_dict())
    sub["ok"] = not sub["errors"]
    return sub


def notify(text):
    url = _env("WB_NOTIFY_URL")
    if not url:
        return {"sent": False, "reason": "WB_NOTIFY_URL 未配置"}
    if "qyapi.weixin.qq.com" in url:
        payload = {"msgtype": "text", "text": {"content": text}}
    else:
        payload = {"content": text, "source": "wb-github-checkin"}
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return {"sent": True, "status": r.status}
    except Exception as e:
        return {"sent": False, "reason": repr(e)}


def _brief(report):
    lines = ["WorkBuddy 云端领取简报 %s（共 %s 个账号）" % (
        dt.datetime.now(TZ8).strftime("%Y-%m-%d %H:%M:%S"), len(report.get("accounts") or []))]
    for sub in report.get("accounts") or []:
        lines.append("── %s ──" % sub.get("name"))
        if not sub.get("ok"):
            lines.append("失败：" + "; ".join("%s %s" % (e.get("code"), e.get("message"))
                                              for e in sub.get("errors") or []))
        ck = sub.get("checkin")
        lines.append("签到：%s" % (
            "未执行" if not ck else
            ("今日已签到（幂等），连续 %s 天" % ck.get("streak_days") if ck.get("already_checked_in")
             else "已领取，今日 +%s 积分，连续 %s 天" % (ck.get("today_credit"), ck.get("streak_days")))))
        for key, label in (("travel", "猫猫旅行"), ("travel_second", "领奖后二轮")):
            tr = sub.get(key)
            if not tr:
                continue
            action = tr.get("action")
            if action == "claim":
                lines.append("%s：已领取奖励 %s 积分（%s）" % (
                    label, tr.get("reward_credit"), tr.get("location")))
            elif action == "depart":
                lines.append("%s：已派往%s" % (label, tr.get("location")))
            elif tr.get("reason") == "traveling":
                lines.append("%s：在途 %s，预计 %s 到达，奖励 %s 积分" % (
                    label, tr.get("location"), tr.get("arrive_at"), tr.get("reward_credit")))
            elif tr.get("reason") == "no_active_buddy":
                lines.append("%s：跳过（该账号尚未领养猫猫）" % label)
            else:
                lines.append("%s：%s（%s）" % (label, action, tr.get("reason", "")))
        th = sub.get("token_health") or {}
        if th.get("days_left") is not None:
            lines.append("凭证有效期至 %s，剩余 %s 天" % (th.get("expires_at"), th.get("days_left")))
    if report.get("warnings"):
        for w in report["warnings"]:
            lines.append("提醒：" + w)
    return "\n".join(lines)


def main():
    report = {"ok": True, "time": dt.datetime.now(TZ8).isoformat(),
              "accounts": [], "errors": [], "warnings": [], "notify": None}
    LOG.info("wb-github-checkin start")

    try:
        accounts = load_accounts()
    except ClaimError as e:
        report["errors"].append(e.as_dict())
        report["ok"] = False
        accounts = []

    for account in accounts:
        try:
            sub = run_account(account)
        except Exception as e:  # 兜底：单账号未预期异常不影响其它账号
            sub = {"name": account.get("name"), "ok": False,
                   "errors": [{"code": "UNEXPECTED", "message": repr(e)}]}
        report["accounts"].append(sub)
        if not sub.get("ok"):
            report["ok"] = False
        for err in sub.get("errors") or []:
            report["errors"].append(dict(err, account=sub.get("name")))

    warn_days = int(_env("WB_WARN_DAYS", "7"))
    for sub in report["accounts"]:
        th = sub.get("token_health") or {}
        if th.get("days_left") is not None and th["days_left"] < warn_days:
            report["warnings"].append("%s 凭证剩余 %s 天（%s），需登录该账号重新导出" % (
                sub.get("name"), th.get("days_left"), th.get("expires_at")))
        for key in ("travel", "travel_second"):
            tr = sub.get(key) or {}
            if tr.get("action") == "skipped" and tr.get("reason") == "no_active_buddy":
                report["warnings"].append(
                    "%s 尚未领养猫猫：签到正常，派遣跳过；需在该账号客户端的成长空间里领养一次"
                    % sub.get("name"))
                break

    brief = _brief(report)
    print(brief)
    # 顶层错误（未绑定账号等，不挂在某个账号下）单独提示，便于在 Actions 日志排查
    for e in report.get("errors") or []:
        if "account" not in e:
            print("错误：%s %s" % (e.get("code"), e.get("message")))

    # 写入 GitHub Actions Step Summary（公开仓库日志里也不含 token）
    summary = _env("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("\n```\n" + brief + "\n```\n")
        except Exception:
            pass

    if not report["ok"] or report["warnings"] or _env("WB_NOTIFY_ON_SUCCESS") == "1":
        report["notify"] = notify(brief)

    fail = _env("WB_FAIL_ON_ERROR") == "1"
    return 1 if (fail and not report["ok"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())

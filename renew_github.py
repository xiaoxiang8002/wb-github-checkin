# -*- coding: utf-8 -*-
"""从本机 WorkBuddy 进程内存提取多账号 Bearer token，推送到 GitHub 仓库 Secret（WB_ACCOUNTS）。

为什么需要它：GitHub Actions 云上运行，读不到你本机 WorkBuddy 进程内存，因此「凭证续期」
必须在本地完成。本脚本负责把最新 token 推到仓库 Secret，Actions 下一次运行即生效。

前置：
  1. 已 `gh auth login`（一次，浏览器/设备码登录）
  2. 仓库已存在（由 setup.ps1 创建，或手动 `gh repo create`）

用法：
  python renew_github.py                                  提取并推送 WB_ACCOUNTS
  python renew_github.py --repo owner/wb-github-checkin   指定仓库（默认自动探测当前仓库）
  python renew_github.py --source D:/Work/wb-cloud-checkin/secrets.local.json
                                                         WorkBuddy 未运行时回退读取的凭证源
  python renew_github.py --dry-run                        只构建并校验，不推送、不提交
  python renew_github.py --install                        注册每周一次无窗口计划任务自动续期
  python renew_github.py --status                         查看计划任务状态
  python renew_github.py --uninstall                      删除计划任务
"""
import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HERE = Path(__file__).parent
TZ8 = dt.timezone(dt.timedelta(hours=8))
DEFAULT_ENDPOINT = "https://www.workbuddy.cn"
SOURCE_DEFAULT = Path(r"D:\Work\wb-cloud-checkin\secrets.local.json")
TASK_NAME = "WB-GitHub-Token-Renew"
JWT = re.compile(rb"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}")
READABLE = {0x02, 0x04, 0x20, 0x40}


def hidden_window():
    kw = {"creationflags": NO_WINDOW}
    if NO_WINDOW and hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    return kw


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD), ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD), ("Protect", wt.DWORD), ("Type", wt.DWORD),
    ]


def workbuddy_pids():
    cmd = ["powershell", "-NoProfile", "-Command",
           "Get-Process WorkBuddy -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=20, **hidden_window())
        return [int(x) for x in re.findall(r"\d+", out)]
    except Exception as e:
        print("获取 WorkBuddy 进程失败：%r" % e)
        return []


def scan_pid(pid):
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wt.HANDLE
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    k.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k.CloseHandle.argtypes = [wt.HANDLE]
    h = k.OpenProcess(0x0400 | 0x0010, False, pid)
    if not h:
        return set()
    found = set()
    addr, total = 0, 0
    try:
        while addr < 0x7FFFFFFF0000 and total < 800 * 1024 * 1024:
            m = MBI()
            if not k.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(m), ctypes.sizeof(m)):
                break
            size = m.RegionSize
            if size == 0:
                break
            if (m.State == 0x1000 and (m.Protect & 0xFF) in READABLE
                    and not (m.Protect & 0x100) and size < 64 * 1024 * 1024):
                buf = ctypes.create_string_buffer(size)
                got = ctypes.c_size_t(0)
                if k.ReadProcessMemory(h, ctypes.c_void_p(m.BaseAddress or addr), buf,
                                       size, ctypes.byref(got)) and got.value:
                    raw = buf.raw[:got.value]
                    total += got.value
                    for mm in JWT.finditer(raw):
                        found.add(mm.group(0).decode("ascii", "replace"))
            addr = (m.BaseAddress or addr) + size
    finally:
        k.CloseHandle(h)
    return found


def jwt_payload(token):
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    p = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(p.encode("ascii")))
    except Exception:
        return {}


def days_left(token):
    exp = jwt_payload(token).get("exp")
    return round((exp - time.time()) / 86400.0, 1) if exp else None


def harvest():
    """扫描 WorkBuddy 进程，返回 {user_id: (exp, token)}，只收 typ=Bearer 且未过期。"""
    pids = workbuddy_pids()
    if not pids:
        return {}, 0
    tokens = set()
    for pid in pids:
        tokens |= scan_pid(pid)
    best = {}
    for t in tokens:
        p = jwt_payload(t)
        if p.get("typ") != "Bearer":
            continue
        exp = p.get("exp") or 0
        now = time.time()
        if not (now < exp < now + 400 * 86400):
            continue
        uid = str(p.get("sub") or "")
        if not uid:
            continue
        if uid not in best or exp > best[uid][0]:
            best[uid] = (exp, t)
    return best, len(tokens)


def load_source(path):
    if not path or not Path(path).exists():
        return []
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        print("源文件解析失败：%r" % e)
        return []
    arr = cfg.get("accounts") or []
    if not arr and cfg.get("WB_ACCESS_TOKEN"):
        arr = [{"name": "默认账号", "token": cfg["WB_ACCESS_TOKEN"],
                "user_id": cfg.get("WB_USER_ID", ""), "endpoint": cfg.get("WB_ENDPOINT", DEFAULT_ENDPOINT)}]
    return [a for a in arr if a.get("token")]


def build_accounts(harvested, source_accounts):
    """优先用新鲜 token 覆盖已知 user_id；保留源里的 name/endpoint；新账号自动命名。"""
    by_uid = {}
    order = []
    for a in source_accounts:
        uid = str(a.get("user_id") or jwt_payload(str(a.get("token"))).get("sub") or "")
        if not uid:
            continue
        by_uid[uid] = {
            "name": a.get("name", "账号"),
            "user_id": uid,
            "token": str(a.get("token")),
            "endpoint": a.get("endpoint") or DEFAULT_ENDPOINT,
        }
        order.append(uid)

    used_fresh = set()
    for uid, (exp, tok) in harvested.items():
        used_fresh.add(uid)
        if uid in by_uid:
            by_uid[uid]["token"] = tok
        else:
            by_uid[uid] = {
                "name": "账号%d" % (len(by_uid) + 1),
                "user_id": uid,
                "token": tok,
                "endpoint": DEFAULT_ENDPOINT,
            }
            order.append(uid)

    accounts = []
    n = 0
    for uid in order:
        a = by_uid[uid]
        exp = jwt_payload(a["token"]).get("exp")
        if not exp or exp <= time.time():
            print("  跳过已过期账号 %s" % a.get("name"))
            continue
        a["expires_at"] = dt.datetime.fromtimestamp(exp, TZ8).strftime("%Y-%m-%d %H:%M:%S GMT+8")
        a["days_left"] = days_left(a["token"])
        accounts.append(a)
        n += 1
    return accounts, used_fresh


def resolve_repo(arg_repo):
    if arg_repo:
        return arg_repo
    try:
        r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner",
                            "-q", ".nameWithOwner"],
                           capture_output=True, text=True, timeout=30, encoding="utf-8",
                           env=net_env())
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def push_secret(accounts, repo):
    payload = json.dumps(accounts, ensure_ascii=False)
    cmd = ["gh", "secret", "set", "WB_ACCOUNTS"]
    if repo:
        cmd += ["--repo", repo]
    r = subprocess.run(cmd, input=payload,
                       capture_output=True, text=True, timeout=60, encoding="utf-8",
                       env=net_env())
    if r.returncode != 0:
        print("推送 Secret 失败：")
        print(r.stderr.strip() or r.stdout.strip())
        return False
    print("已推送 WB_ACCOUNTS（%s 个账号）到仓库 %s" % (len(accounts), repo or "当前仓库"))
    return True


def keepalive():
    """定时任务场景下给仓库留一次空提交，防止 GitHub 在 60 天无活动后自动停用 scheduled workflow。"""
    try:
        env = net_env()
        r = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=20, encoding="utf-8", env=env)
        if r.returncode != 0 or r.stdout.strip() != "true":
            return
        rr = subprocess.run(["git", "-C", str(HERE), "remote", "get-url", "origin"],
                            capture_output=True, text=True, timeout=20, encoding="utf-8", env=env)
        if rr.returncode != 0 or not rr.stdout.strip():
            return
        now = dt.datetime.now(TZ8).strftime("%Y-%m-%d %H:%M")
        c = subprocess.run(["git", "-C", str(HERE), "commit", "--allow-empty",
                            "-m", "token renew %s" % now],
                           capture_output=True, text=True, timeout=30, encoding="utf-8", env=env)
        if c.returncode == 0:
            subprocess.run(["git", "-C", str(HERE), "push"],
                           capture_output=True, text=True, timeout=60, encoding="utf-8", env=env)
            print("keepalive：已空提交并推送（保持仓库活跃）")
    except Exception as e:
        print("keepalive 跳过：%r" % e)


def require_gh_auth():
    r = subprocess.run(["gh", "auth", "status"],
                       capture_output=True, text=True, timeout=30, encoding="utf-8")
    if r.returncode != 0:
        print("未登录 GitHub CLI。请先在本机执行：gh auth login")
        print(r.stderr.strip() or r.stdout.strip())
        return False
    return True


def net_env(base=None):
    """返回带 CuteCloud 代理的网络环境副本（仅当 127.0.0.1:7890 在监听时添加）。

    本机直连 github.com 不通，必须走 CuteCloud 代理；gh/git 命令行不读 Windows
    系统代理，只认 HTTPS_PROXY/HTTP_PROXY 环境变量。续期任务运行时若 CuteCloud
    已连接，自动套用，否则原样放行（此时续期会失败但无副作用，token 仍有效数周）。
    """
    env = dict(base if base is not None else os.environ)
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.0)
        up = s.connect_ex(("127.0.0.1", 7890)) == 0
        s.close()
    except Exception:
        up = False
    if up:
        proxy = "http://127.0.0.1:7890"
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    return env


PS_INSTALL = r"""
$ErrorActionPreference = 'Stop'
$py = '{py}'
$script = '{script}'
$wd = '{wd}'
$a = New-ScheduledTaskAction -Execute $py -Argument ('-B "{script}"') -WorkingDirectory $wd
$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At (Get-Date).AddHours(1)
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
     -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName '{task}' -Action $a -Trigger $t -Settings $s `
     -Description 'WorkBuddy token auto-renew to GitHub secret' -Force | Out-Null
'INSTALLED'
"""

PS_STATUS = r"""
$t = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue
if (-not $t) {{ 'NOT_INSTALLED'; exit 0 }}
$i = Get-ScheduledTaskInfo -TaskName '{task}'
'TaskName  : ' + $t.TaskName
'State     : ' + $t.State
'LastRun   : ' + $i.LastRunTime
'LastResult: ' + $i.LastTaskResult
'NextRun   : ' + $i.NextRunTime
"""

PS_UNINSTALL = r"""
Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -ErrorAction SilentlyContinue
'UNINSTALLED'
"""


def powershell(script):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=120,
                       encoding="utf-8", errors="replace", **hidden_window())
    sys.stdout.write(r.stdout or "")
    if r.returncode != 0:
        sys.stderr.write(r.stderr or "")
    return r.returncode


def main():
    ap = argparse.ArgumentParser(description="本地提取 WorkBuddy 凭证并推送到 GitHub Secret")
    ap.add_argument("--repo", default="", help="目标仓库 owner/name（默认自动探测）")
    ap.add_argument("--source", default=str(SOURCE_DEFAULT),
                    help="WorkBuddy 未运行时的回退凭证源（默认腾讯云项目的 secrets.local.json）")
    ap.add_argument("--dry-run", action="store_true", help="只构建校验，不推送、不提交")
    ap.add_argument("--install", action="store_true", help="注册每周计划任务")
    ap.add_argument("--status", action="store_true", help="查看计划任务状态")
    ap.add_argument("--uninstall", action="store_true", help="删除计划任务")
    args = ap.parse_args()

    if args.install:
        exe = Path(sys.executable).with_name("pythonw.exe")
        if not exe.exists():
            exe = Path(sys.executable)
        return powershell(PS_INSTALL.format(py=str(exe), script=str(HERE / "renew_github.py"),
                                            wd=str(HERE), task=TASK_NAME))
    if args.uninstall:
        return powershell(PS_UNINSTALL.format(task=TASK_NAME))
    if args.status:
        rc = powershell(PS_STATUS.format(task=TASK_NAME))
        return rc

    if sys.platform != "win32":
        print("仅支持 Windows（凭证来自本机 WorkBuddy 进程内存）")
        return 1

    harvested, scanned = harvest()
    source_accounts = load_source(args.source)
    print("扫描 WorkBuddy 进程：候选 JWT %s 个，新鲜 Bearer %s 个；源文件账号 %s 个"
          % (scanned, len(harvested), len(source_accounts)))

    accounts, used_fresh = build_accounts(harvested, source_accounts)
    if not accounts:
        print("未构建出任何有效账号，跳过推送（避免清空线上 Secret）。")
        return 1
    for a in accounts:
        print("  %s | user_id=%s | 剩余 %s 天" % (a.get("name"), a.get("user_id"), a.get("days_left")))

    if args.dry_run:
        print("--dry-run：未推送。JSON 长度 %s 字符" % len(json.dumps(accounts, ensure_ascii=False)))
        return 0

    if not require_gh_auth():
        return 1
    repo = resolve_repo(args.repo)
    if not repo:
        print("无法确定目标仓库，请用 --repo owner/wb-github-checkin 指定。")
        return 1
    ok = push_secret(accounts, repo)
    if ok:
        keepalive()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

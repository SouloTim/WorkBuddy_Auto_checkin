#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token_sync.py — 把本机 WorkBuddy 的最新 accessToken 自动同步到 GitHub Secret。

设计前提（无法绕开）：
  accessToken 由客户端签发、以 Electron safeStorage 加密存在本机，云端 Actions
  runner 读不到。因此「续期」必须有一段代码在本机运行；本脚本即这一段。

工作方式：
  1. 复用 ~/workbuddy_auto_checkin/refresh_token.py 的解密链路读出当前 accessToken
     （不重复实现加解密，单一来源）
  2. 解析 JWT 的 exp，与本目录 state.json 里上次同步的到期时间比对
  3. 仅当令牌已更新（或距上次同步超过 FORCE_DAYS 天、或使用 --force）时，
     通过 gh secret set 从 stdin 覆盖 WB_ACCESS_TOKEN
  4. 全程明文 token / PAT 不落盘、不进 argv、不进日志；state.json 只存到期时间

用法：
    python3 token_sync.py             # 常规：有更新才写
    python3 token_sync.py --force     # 强制写一次（首次部署、手动验证）
    python3 token_sync.py --dry-run   # 只解密并报告，不联网、不写任何东西

退出码：0 成功（含「无需更新」）；1 失败。
"""

import base64
import datetime
import json
import os
import subprocess
import sys

# 凭据与状态固定落在本机用户目录，与脚本所在位置解耦 —— 即使脚本入库，
# PAT、state、日志也永远不会出现在仓库工作区里。
DATA_DIR = os.environ.get("WB_SYNC_DIR") or os.path.expanduser("~/.workbuddy/token_sync")
PAT_FILE = os.path.join(DATA_DIR, "gh_pat")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
LOG_FILE = os.path.join(DATA_DIR, "sync.log")

REPO = "SouloTim/WorkBuddy_Auto_checkin"
SECRET_NAME = "WB_ACCESS_TOKEN"
GH = "/opt/homebrew/bin/gh"
FORCE_DAYS = 20          # 超过这么多天没成功同步，即使 exp 没变也重写一次
REQUEST_TIMEOUT = 60


def resolve_repo_dir():
    """定位含 refresh_token.py 的仓库目录，供复用其解密链路。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("WB_REPO_DIR"),
        os.path.dirname(script_dir),                       # 脚本在 <repo>/tools/ 下
        script_dir,                                        # 脚本与 refresh_token.py 同级
        os.path.expanduser("~/workbuddy_auto_checkin"),
    ]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "refresh_token.py")):
            return c
    return os.path.expanduser("~/workbuddy_auto_checkin")


def log(msg, *secrets):
    """写日志并打印。任何敏感串在输出前被替换掉。"""
    for s in secrets:
        if s:
            msg = msg.replace(s, "<已隐藏>")
    line = "%s  %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def read_pat():
    if not os.path.isfile(PAT_FILE):
        raise RuntimeError("缺少 PAT 文件：%s" % PAT_FILE)
    mode = os.stat(PAT_FILE).st_mode & 0o777
    if mode & 0o077:
        raise RuntimeError("PAT 文件权限过宽（%o），请执行 chmod 600 %s" % (mode, PAT_FILE))
    with open(PAT_FILE, "r", encoding="utf-8") as f:
        pat = f.read().strip()
    if not pat.startswith("github_pat_"):
        raise RuntimeError("PAT 文件内容格式不符（应以 github_pat_ 开头）")
    return pat


def jwt_exp(token):
    """离线解析 JWT 的 exp 声明；非 JWT 或解析失败返回 None。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
        exp = claims.get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            return datetime.datetime.fromtimestamp(exp)
    except Exception:
        pass
    return None


def local_token():
    """返回 (token, exp)。解密链路完全复用仓库里的 refresh_token.py。"""
    repo_dir = resolve_repo_dir()
    if not os.path.isfile(os.path.join(repo_dir, "refresh_token.py")):
        raise RuntimeError("找不到仓库脚本 refresh_token.py（路径 %s），可用 WB_REPO_DIR 覆盖" % repo_dir)
    sys.path.insert(0, repo_dir)
    import refresh_token as rt

    auth_file = rt.find_auth_file()
    if not auth_file:
        raise RuntimeError("未找到 WorkBuddy 凭据文件，请确认客户端已安装并登录")
    session = rt.load_session(auth_file)
    token = session.get("auth", {}).get("accessToken")
    if not token:
        raise RuntimeError("凭据文件缺少 accessToken，请在 WorkBuddy 客户端重新登录")

    if not isinstance(token, str):
        exe = rt.find_runtime()
        token = rt.run_helper(exe, {"operation": "decrypt", "value": token})["accessToken"]
    if not (isinstance(token, str) and rt.TOKEN_RE.fullmatch(token)):
        raise RuntimeError("解密得到的 token 格式异常")

    exp = jwt_exp(token)
    if exp is None:
        # 兜底：凭据文件自带 expiresAt（毫秒）
        exp, _ = rt.session_expiry(session)
    if exp is None:
        raise RuntimeError("无法解析令牌到期时间")
    return token, exp


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def write_secret(token, pat):
    """token 只走 stdin，PAT 只走环境变量；两者都不出现在命令行参数里。"""
    env = dict(os.environ)
    env["GH_TOKEN"] = pat
    for key in ("GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        env.pop(key, None)
    env["GH_PROMPT_DISABLED"] = "1"
    env["NO_COLOR"] = "1"
    proc = subprocess.run(
        [GH, "secret", "set", SECRET_NAME, "--repo", REPO],
        input=token.encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, timeout=REQUEST_TIMEOUT,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError("写入 Secret 失败（exit %d）：%s" % (proc.returncode, err[:300]))
    return proc.stdout.decode("utf-8", "replace").strip()


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    force = "--force" in args

    token, exp = local_token()
    now = datetime.datetime.now()
    exp_text = exp.strftime("%Y-%m-%d %H:%M:%S")
    left = (exp - now).total_seconds() / 86400
    log("本机令牌到期 %s（剩余 %.1f 天）" % (exp_text, left), token)

    if left <= 0:
        log("[警告] 本机令牌已过期，仍尝试同步；请打开 WorkBuddy 客户端或重新登录", token)
    elif left <= 3:
        log("[警告] 本机令牌仅剩 %.1f 天，客户端可能未换发，请留意" % left, token)

    if dry_run:
        log("dry-run：不联网、不写 Secret", token)
        return 0

    pat = read_pat()
    state = load_state()
    prev_exp = state.get("exp")
    prev_sync = state.get("synced_at")
    stale = True
    if prev_sync:
        try:
            delta = (now - datetime.datetime.fromisoformat(prev_sync)).total_seconds() / 86400
            stale = delta >= FORCE_DAYS
        except ValueError:
            pass

    if not force and not stale and prev_exp == exp_text:
        log("令牌未变化，跳过写入（上次同步 %s）" % (prev_sync or "无记录"), token, pat)
        return 0

    reason = "强制同步" if force else ("距上次同步过久" if stale else "检测到客户端换发新令牌")
    log("准备写入 Secret：%s（%s）" % (reason, SECRET_NAME), token, pat)
    out = write_secret(token, pat)
    if out:
        log("gh 输出：%s" % out[:200], token, pat)

    save_state({
        "exp": exp_text,
        "synced_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "repo": REPO,
        "secret": SECRET_NAME,
    })
    log("同步完成：%s 已更新，到期 %s" % (SECRET_NAME, exp_text), token, pat)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        try:
            pat = read_pat()
        except Exception:
            pat = None
        log("[失败] %s: %s" % (type(e).__name__, e), pat)
        sys.exit(1)

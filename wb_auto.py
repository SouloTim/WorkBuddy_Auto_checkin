#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_auto.py —— 云端 runner 执行的每日自动化：签到 + 派猫猫旅行，结果推送到微信。

设计要点：
  1. 一次运行把签到与派猫都做掉；
  2. 先领已到家的旅行积分，再判断能否派新一趟；今日名额已用则不派；
  3. 签到与猫猫状态分开汇报；
  4. 猫猫失败不污染签到结论 —— 签到成功即整体成功；
  5. 顺带检查访问令牌剩余有效期，临近到期时在通知里提醒。

凭证来自 GitHub Secret（由本机 refresh_token.py 注入），只读环境变量，不落盘、不打日志。

推送通道（填哪条推哪条，可多条并存）：
  WECOM_WEBHOOK       企业微信群机器人
  SERVERCHAN_SENDKEY  Server酱（个人微信）
  PUSHPLUS_TOKEN      PushPlus（个人微信）
"""

import base64
import datetime
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
REQUEST_TIMEOUT = 20
MAX_RETRY = 3

CODE_NO_NETWORK = -1


def log(msg):
    print(msg, flush=True)


def masked(text):
    """把返回体里的 token 类字段脱敏后再落日志。"""
    if isinstance(text, dict):
        out = {}
        for k, v in text.items():
            if isinstance(v, (dict, list)):
                out[k] = masked(v)
            elif isinstance(v, str) and len(v) > 24:
                out[k] = v[:6] + "***" + v[-4:]
            else:
                out[k] = v
        return out
    if isinstance(text, list):
        return [masked(x) for x in text]
    if isinstance(text, str) and len(text) > 200:
        return text[:200] + "..."
    return text


def dig(obj, *keys):
    """逐层取值，任一层缺失返回 None。"""
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        else:
            return None
    return obj


def as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def request(url, headers, method="GET", payload=None, retry=True):
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, {"raw": raw[:300]}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = {"raw": raw[:300]}
            # 偶发 3xx（快速轮询时网关会短暂跳转）不是业务结论，退避后重试
            if 300 <= e.code < 400 and attempt < MAX_RETRY:
                log("  · 收到 %d 跳转，%.1fs 后重试（%d/%d）"
                    % (e.code, 2.0 * attempt, attempt, MAX_RETRY))
                import time
                time.sleep(2.0 * attempt)
                continue
            return e.code, body
        except Exception as e:
            # 只对网络抖动与 5xx 重试；写操作靠接口幂等，最多重试一次
            if not retry or attempt >= MAX_RETRY:
                return CODE_NO_NETWORK, {"error": "%s: %s" % (type(e).__name__, e)}
            log("  · 请求异常，%.1fs 后重试（%d/%d）" % (2.0, attempt, MAX_RETRY))
            import time
            time.sleep(2.0)


def get(url, headers):
    return request(url, headers, method="GET")


def post(url, headers, payload=None):
    return request(url, headers, method="POST", payload=payload)


def api_ok(code, body):
    return 200 <= code < 300 and (not isinstance(body, dict) or body.get("code", 0) == 0)


def unwrap(body):
    """真实返回统一是 {code,msg,data} 信封，业务字段都在 data 里。"""
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------- 签到

def run_checkin(headers, endpoint):
    """查状态 → 未签才领。返回 (ok, 汇报 dict)。"""
    scode, sbody = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
    log("  [签到-状态] HTTP %s %s" % (scode, json.dumps(masked(sbody), ensure_ascii=False)))

    if scode == CODE_NO_NETWORK:
        return False, {"result": "NETWORK", "report": "网络不可达，签到未完成"}
    if scode in (401, 403):
        return False, {"result": "AUTH", "report": "认证被拒（HTTP %s），请重新运行 refresh_token.py 注入新 token" % scode}
    if not (200 <= scode < 300):
        return False, {"result": "ERROR", "report": "状态接口异常（HTTP %s）" % scode}

    status = unwrap(sbody)
    if dig(status, "active") is False:
        name = dig(status, "activity_name")
        return True, {"result": "INACTIVE", "report": "签到活动未开启" + ("（%s）" % name if name else "")}

    if dig(status, "today_checked_in") in (True, 1):
        return True, {"result": "ALREADY", "report": _fmt_already(status)}

    ccode, cbody = post(endpoint + "/v2/billing/meter/daily-checkin", headers)
    log("  [签到-领取] HTTP %s %s" % (ccode, json.dumps(masked(cbody), ensure_ascii=False)))

    if ccode == CODE_NO_NETWORK:
        return False, {"result": "NETWORK", "report": "领取请求未送达，签到未完成"}
    if ccode in (401, 403):
        return False, {"result": "AUTH", "report": "认证被拒（HTTP %s）" % ccode}

    if _already_checked_in(ccode, cbody):
        _, fresh = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
        return True, {"result": "ALREADY",
                      "report": _fmt_already(unwrap(fresh), via="今日已签过（服务端判定已领取）")}

    credit = dig(unwrap(cbody), "credit")
    if credit is not None:
        _, fresh = post(endpoint + "/v2/billing/meter/checkin-activity-status", headers)
        s = unwrap(fresh)
        bonus = "，且为连签奖励日" if dig(s, "is_streak_day") else ""
        streak = dig(s, "streak_days")
        cum = dig(s, "total_credits")
        tail = ""
        if streak is not None:
            tail = "（连续 %s 天%s）" % (streak, "，累计 %s 积分" % cum if cum is not None else "")
        return True, {"result": "CLAIMED", "credit": credit,
                      "report": "成功领取 %s 积分%s%s" % (credit, bonus, tail)}

    if isinstance(cbody, dict) and ("code" in cbody or "msg" in cbody):
        return False, {"result": "ERROR",
                       "report": "领取失败：%s（HTTP %s）" % (cbody.get("msg") or cbody.get("code"), ccode)}
    return False, {"result": "UNKNOWN", "report": "未识别的领取返回"}


def _already_checked_in(code, body):
    if isinstance(body, dict):
        msg = body.get("msg") or ""
        if body.get("code") == 10001 or "已签" in msg:
            return True
    return False


def _fmt_eta(arrive_at, server_now):
    """把秒级时间戳差格式化成人类可读文案；数据缺失时不编造。"""
    if arrive_at is None or server_now is None:
        return ""
    try:
        left = int(arrive_at) - int(server_now)
    except (TypeError, ValueError):
        return ""
    if left <= 0:
        return "，已到家待领"
    if left < 3600:
        return "，约 %d 分钟后到家" % max(1, left // 60)
    return "，约 %.1f 小时后到家" % (left / 3600)


def _fmt_already(status, via="今日已签过"):
    parts = []
    if dig(status, "today_credit") is not None:
        parts.append("今日 +%s" % dig(status, "today_credit"))
    if dig(status, "streak_days") is not None:
        parts.append("连续 %s 天" % dig(status, "streak_days"))
    if dig(status, "total_credits") is not None:
        parts.append("累计 %s 积分" % dig(status, "total_credits"))
    return "%s（%s）" % (via, "，".join(parts)) if parts else via


# ---------------------------------------------------------------- 猫猫

def run_buddy(headers, endpoint):
    """先领已到家的旅行积分，再判断能否派新一趟。猫猫失败不影响签到结论。"""
    base = endpoint + "/v2/activity/growth"
    notes = []
    try:
        scode, sbody = get(base + "/buddy/travel/status", headers)
        log("  [猫猫-状态] HTTP %s %s" % (scode, json.dumps(masked(sbody), ensure_ascii=False)))

        if scode == CODE_NO_NETWORK:
            return {"result": "NETWORK", "report": "网络不可达，猫猫未处理", "notes": notes}
        if scode in (401, 403):
            return {"result": "AUTH", "report": "猫猫认证被拒（HTTP %s）" % scode, "notes": notes}
        if not (200 <= scode < 300):
            return {"result": "ERROR", "report": "旅行状态接口异常（HTTP %s）" % scode, "notes": notes}

        raw_state = dig(unwrap(sbody), "state")
        travel = raw_state
        daily_limit = bool(dig(unwrap(sbody), "daily_limit_reached"))
        travel_body = unwrap(sbody)

        # 1) 已到家 → 先领积分
        if travel == "arrived":
            ccode, cbody = post(base + "/buddy/travel/claim", headers,
                                {"record_id": dig(travel_body, "record_id")})
            log("  [猫猫-领礼物] HTTP %s %s" % (ccode, json.dumps(masked(cbody), ensure_ascii=False)))
            got = dig(unwrap(cbody), "reward_credit")
            if api_ok(ccode, cbody) and got is not None:
                notes.append("领旅行礼物 +%s 积分" % got)
                travel = "idle"
            else:
                return {"result": "ERROR",
                        "report": "领旅行礼物失败：%s（HTTP %s）" % (dig(cbody, "msg") or "未知", ccode),
                        "notes": notes}
        elif travel == "traveling":
            loc = dig(travel_body, "location", "name") or "?"
            notes.append("旅行中（%s%s）" % (loc, _fmt_eta(dig(travel_body, "arrive_at"),
                                                          dig(travel_body, "server_now"))))

        # 2) 判断能否派新一趟
        if travel == "traveling":
            return {"result": "IN_TRIP", "notes": notes,
                    "report": "；".join(notes) if notes else "猫猫在旅行中，无需派出"}

        if travel == "idle":
            if daily_limit:
                return {"result": "LIMIT_REACHED",
                        "report": "；".join(notes + ["今日旅行名额已用完，不再派出"])}
            kcode, kbody = get(base + "/buddy/travel/config", headers)
            log("  [猫猫-目的地] HTTP %s %s" % (kcode, json.dumps(masked(kbody), ensure_ascii=False)))
            locs = dig(unwrap(kbody), "locations") if 200 <= kcode < 300 else None
            if not locs or not isinstance(locs[0], dict):
                return {"result": "ERROR", "report": "未取到可派目的地", "notes": notes}
            loc = locs[0]
            dcode, dbody = post(base + "/buddy/travel/depart", headers,
                                {"location_id": loc.get("id")})
            log("  [猫猫-派出] HTTP %s %s" % (dcode, json.dumps(masked(dbody), ensure_ascii=False)))
            if 200 <= dcode < 300:
                depart = unwrap(dbody)
                name = dig(depart, "location", "name") or loc.get("name") or "?"
                dur = dig(depart, "duration_hours") or dig(depart, "location", "duration_hours")
                notes.append("已派猫猫去%s%s" % (name, "（约 %s 小时后回）" % dur if dur else ""))
                return {"result": "DEPARTED", "notes": notes, "report": "；".join(notes)}
            msg = dig(unwrap(dbody), "msg") or dig(dbody, "msg") or ""
            # 名额已用完属于正常幂等结果，不算失败
            if "limit" in str(msg).lower() or dcode == 429:
                notes.append("今日旅行名额已用完，不再派出")
                return {"result": "LIMIT_REACHED", "notes": notes, "report": "；".join(notes)}
            return {"result": "ERROR",
                    "report": "派出失败：%s（HTTP %s）" % (msg or "未知", dcode), "notes": notes}

        # 兜底：state 不是已知取值。原样汇报、不做写操作，避免误派或误领。
        return {"result": "UNKNOWN_STATE",
                "report": "；".join(notes) if notes else "未识别的旅行状态：%s" % raw_state,
                "notes": notes}
    except Exception as e:
        return {"result": "ERROR", "report": "猫猫模块异常（%s: %s）" % (type(e).__name__, e), "notes": notes}


# ---------------------------------------------------------------- 令牌到期提醒

EXPIRY_WARN_DAYS = 7      # 剩余天数 ≤ 此值 → 进入预警
EXPIRY_CRITICAL_DAYS = 3  # 剩余天数 ≤ 此值 → 紧急告警


def decode_jwt_claims(token):
    """本地解析 JWT 的载荷（不验签、不联网）。失败返回 {}。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def token_expiry(token):
    """返回 (到期 datetime, 剩余天数)；无法解析返回 (None, None)。"""
    exp = decode_jwt_claims(token).get("exp")
    if not isinstance(exp, (int, float)):
        return None, None
    expire = datetime.datetime.fromtimestamp(exp)
    left = (expire - datetime.datetime.now()).total_seconds() / 86400
    return expire, left


def expiry_line(token):
    """生成给人看的一行到期提醒文案；解析不出时返回 None（不臆造）。"""
    expire, left = token_expiry(token)
    if expire is None:
        return None
    date = expire.strftime("%Y-%m-%d")
    if left <= 0:
        return "🔴 访问令牌已过期（%s），签到会失败，请立即重跑 refresh_token.py 重新注入" % date
    if left <= EXPIRY_CRITICAL_DAYS:
        return "🔴 访问令牌 %s 到期，仅剩 %.1f 天，请尽快重跑 refresh_token.py 重新注入" % (date, left)
    if left <= EXPIRY_WARN_DAYS:
        return "🟡 访问令牌 %s 到期，剩 %.1f 天，建议近日重跑 refresh_token.py 重新注入" % (date, left)
    return "✅ 访问令牌有效期至 %s（剩 %.0f 天）" % (date, left)


# ---------------------------------------------------------------- 推送
#
# 三条通道，填了哪条就推哪条（可同时启用多条）：
#   WECOM_WEBHOOK     企业微信群机器人          官方 webhook，最稳
#   SERVERCHAN_SENDKEY Server酱（个人微信）      第三方中转，SCT 开头
#   PUSHPLUS_TOKEN    PushPlus（个人微信）       第三方中转
# 任一通道失败都只记录原因，不影响签到结论；当天多时段 cron 会自然重推。

def _http_post_json(url, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers=headers or {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as resp:
        return getattr(resp, "status", 200), json.loads(resp.read().decode("utf-8", "replace"))


def _http_post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as resp:
        return getattr(resp, "status", 200), json.loads(resp.read().decode("utf-8", "replace"))


def send_wecom(webhook, text):
    """企业微信群机器人。返回 (ok, reason)。"""
    try:
        _, body = _http_post_json(webhook, {"msgtype": "text", "text": {"content": text}})
    except urllib.error.HTTPError as e:
        return False, "企业微信 HTTP %s" % e.code
    except Exception as e:
        return False, "企业微信请求异常：%s: %s" % (type(e).__name__, e)
    if body.get("errcode") == 0:
        return True, ""
    return False, "企业微信返回 errcode=%s %s（92001/webhook 无效多为地址不完整）" % (
        body.get("errcode", "未知"), body.get("errmsg", ""))


def send_serverchan(sendkey, text):
    """Server酱（个人微信推送）。返回 (ok, reason)。"""
    is_sct = sendkey.startswith("sct")
    if is_sct:
        url = "https://sctapi.ftqq.com/%s.send" % sendkey
        try:
            _, body = _http_post_form(url, {"title": "WorkBuddy 每日自动化", "desp": text})
        except urllib.error.HTTPError as e:
            return False, "Server酱 HTTP %s" % e.code
        except Exception as e:
            return False, "Server酱请求异常：%s: %s" % (type(e).__name__, e)
        if body.get("code") == 0:
            return True, ""
        return False, "Server酱返回 code=%s %s" % (body.get("code", "未知"), body.get("message", ""))
    # 兼容旧版 SCKEY：GET 调用
    url = "https://sc.ftqq.com/%s.send?text=%s" % (sendkey, urllib.parse.quote("WorkBuddy 每日自动化"))
    try:
        with urllib.request.urlopen(url, timeout=15, context=ssl.create_default_context()) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return False, "Server酱(旧版) HTTP %s" % e.code
    except Exception as e:
        return False, "Server酱(旧版)请求异常：%s: %s" % (type(e).__name__, e)
    if body.get("errno") == 0:
        return True, ""
    return False, "Server酱(旧版)返回 errno=%s %s" % (body.get("errno", "未知"), body.get("errmsg", ""))


def send_pushplus(token, text):
    """PushPlus（个人微信推送）。返回 (ok, reason)。"""
    try:
        _, body = _http_post_json("https://www.pushplus.plus/send",
                                  {"token": token, "title": "WorkBuddy 每日自动化",
                                   "content": text, "template": "txt"})
    except urllib.error.HTTPError as e:
        return False, "PushPlus HTTP %s" % e.code
    except Exception as e:
        return False, "PushPlus请求异常：%s: %s" % (type(e).__name__, e)
    if body.get("code") == 200:
        return True, ""
    return False, "PushPlus返回 code=%s %s" % (body.get("code", "未知"), body.get("msg", ""))


def build_message(title, checkin_line, buddy_line, expiry, overall_ok):
    lines = [
        "【%s】%s" % (title, "签到成功" if overall_ok else "签到未达成"),
        "",
        "签到：%s" % checkin_line,
        "猫猫：%s" % buddy_line,
    ]
    if expiry:
        lines += ["", "令牌：%s" % expiry]
    return "\n".join(lines)


def push_all(channels, text):
    """按配置推送所有通道，返回 [{name, ok, reason}]。"""
    results = []
    for name, send in channels:
        try:
            ok, reason = send(text)
        except Exception as e:
            ok, reason = False, "%s: %s" % (type(e).__name__, e)
        log("  [%s] %s%s" % (name, "成功" if ok else "失败", "" if ok else "：" + str(reason)))
        results.append({"name": name, "ok": ok, "reason": reason})
    return results


# ---------------------------------------------------------------- 主流程

def main():
    token = os.environ.get("WB_ACCESS_TOKEN")
    endpoint = os.environ.get("WB_ENDPOINT") or DEFAULT_ENDPOINT

    # 推送通道：填了哪条就推哪条，可同时启用多条
    channels = []
    if os.environ.get("WECOM_WEBHOOK"):
        channels.append(("企业微信", lambda t: send_wecom(os.environ["WECOM_WEBHOOK"], t)))
    if os.environ.get("SERVERCHAN_SENDKEY"):
        channels.append(("Server酱", lambda t: send_serverchan(os.environ["SERVERCHAN_SENDKEY"], t)))
    if os.environ.get("PUSHPLUS_TOKEN"):
        channels.append(("PushPlus", lambda t: send_pushplus(os.environ["PUSHPLUS_TOKEN"], t)))

    if not token:
        log("缺少环境变量 WB_ACCESS_TOKEN（请在本机运行 refresh_token.py 注入 GitHub Secret）")
        return 2
    if not channels:
        log("未配置任何推送通道（WECOM_WEBHOOK / SERVERCHAN_SENDKEY / PUSHPLUS_TOKEN），本次仅打印日志")

    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer %s" % token,
        "Content-Type": "application/json",
        "User-Agent": "WorkBuddy",
    }

    log("=== 1/2 签到 ===")
    checkin_ok, checkin = run_checkin(headers, endpoint)
    log("  结论：%s" % checkin["report"])

    # 猫猫独立 try：任何异常都不影响签到结论
    log("=== 2/2 猫猫旅行 ===")
    try:
        buddy = run_buddy(headers, endpoint)
    except Exception as e:
        buddy = {"result": "ERROR", "report": "猫猫模块异常（%s: %s）" % (type(e).__name__, e)}
    log("  结论：%s" % buddy.get("report", ""))

    # 整体成败只看签到：签到成功即成功
    overall_ok = checkin_ok
    checkin_line = ("✅ " if checkin_ok else "❌ ") + checkin["report"]
    buddy_line = ("🐱 " + buddy.get("report", "")) if buddy.get("report") else "🐱 无结论"

    # 令牌到期提醒：本地解析 JWT 的 exp，不需要额外 Secret
    expiry = expiry_line(token)
    if expiry:
        log("  [令牌] %s" % expiry)

    # 推送
    message = build_message("WorkBuddy 每日自动化", checkin_line, buddy_line, expiry, overall_ok)
    log("=== 推送 ===")
    if channels:
        pushes = push_all(channels, message)
    else:
        pushes = []
        log("  · 无推送通道，跳过")

    result = {
        "checkin": {"ok": checkin_ok, **checkin},
        "buddy": {k: v for k, v in buddy.items() if k != "notes"},
        "expiry": expiry,
        "push": pushes,
        "overall_ok": overall_ok,
    }
    print("RESULT_JSON " + json.dumps(result, ensure_ascii=False))

    # 结果文件只含结论文案，不含 token，供 Actions 写入 step summary
    result_file = os.environ.get("WB_RESULT_FILE")
    if result_file:
        try:
            if not channels:
                push_line = "未配置推送通道"
            else:
                push_line = "；".join(
                    "%s%s" % (p["name"], "✅" if p["ok"] else "❌ " + str(p["reason"]))
                    for p in pushes)
            with open(result_file, "w", encoding="utf-8") as f:
                f.write("| 项目 | 结论 |\n| --- | --- |\n")
                f.write("| 签到 | %s |\n" % checkin_line)
                f.write("| 猫猫旅行 | %s |\n" % buddy_line)
                f.write("| 令牌有效期 | %s |\n" % (expiry or "无法解析"))
                f.write("| 推送 | %s |\n" % push_line)
                f.write("| 整体 | %s |\n" % ("成功（签到已达成）" if overall_ok else "失败（签到未达成）"))
        except OSError as e:
            log("结果文件写入失败：%s" % e)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
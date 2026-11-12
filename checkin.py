#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
2026 GLaDOS 自动签到 GitHub版
定时：北京时间17:30触发，脚本内随机0~1800秒延时，17:30~18:00签到
推送：Server酱旧排版，无自动兑换，适配新版challenge校验
新增：账号之间随机休眠60~180秒（最后一个账号不休眠）
修改点：无论成功/失败，全部推送；推送排版对齐你最早那版样式（Markdown标题+分行硬换行）
新增：diagnose_only诊断模式跳过前置随机延时
【本次改动】
- 记录延时变量，推送增加延时+实际签到时间
- ✅ 计数逻辑改为老版本：只要签到接口正常返回(is_success)就算成功，重复签到计入成功数量
"""
import html
import hashlib
import json
import math
import os
import re
import sys
import time
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests

# Fix Windows Unicode Output
if sys.platform.startswith('win'):
    sys.stdout.reconfigure(encoding='utf-8')

# ================= 配置 =================
DOMAINS = [
    "https://glados.cloud",
    "https://glados.rocks",
    "https://glados.network",
]

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)

HEADERS = {
    'Content-Type': 'application/json;charset=UTF-8',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
}

NORMAL_CHECKIN_MESSAGES = (
    "checkin! got",
    "checkin repeats! please try tomorrow",
    "today's observation logged",
    "done for today"
)

CHECKIN_CHALLENGE_DELAY = 2.7

CURRENT_SESSION_COOKIES = ('gld:sess', 'gld:sess.sig')
LEGACY_SESSION_COOKIES = ('koa:sess', 'koa:sess.sig')

# ================= 工具函数 =================
def beijing_time():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    print(f"[{beijing_time()}] {msg}", flush=True)

def extract_cookie(raw: str):
    if not raw:
        return None
    raw = raw.strip()
    raw = re.sub(r'^cookie\s*:\s*', '', raw, flags=re.IGNORECASE)
    if any(f'{name}=' in raw for name in CURRENT_SESSION_COOKIES + LEGACY_SESSION_COOKIES):
        return raw
    if raw.startswith(('{', '[')):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                pairs = []
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    name = item.get('name')
                    value = item.get('value')
                    if name and value is not None:
                        pairs.append(f'{name}={value}')
                cookie = '; '.join(pairs)
                return cookie if cookie and get_session_cookie_kind(cookie) else None
            token = parsed.get('token') if isinstance(parsed, dict) else None
            return f'koa:sess={token}' if token else None
        except (json.JSONDecodeError, AttributeError, TypeError):
            return None
    if raw.count('.') == 2 and '=' not in raw and len(raw) > 50:
        return 'koa:sess=' + raw
    return raw

def get_cookie_names(cookie_header):
    names = set()
    for item in cookie_header.split(';'):
        name, separator, _ = item.strip().partition('=')
        if separator and name:
            names.add(name)
    return names

def get_session_cookie_kind(cookie_header):
    names = get_cookie_names(cookie_header)
    if set(CURRENT_SESSION_COOKIES).issubset(names):
        return 'gld'
    if set(LEGACY_SESSION_COOKIES).issubset(names):
        return 'koa'
    return None

def get_cookies():
    raw = os.environ.get("GLADOS_COOKIE", "")
    if not raw:
        log("❌ 未配置 GLADOS_COOKIE")
        return []
    if raw.lstrip().startswith(('{', '[')):
        cookie = extract_cookie(raw)
        return [cookie] if cookie else []
    sep = '\n' if '\n' in raw else '&'
    return [cookie for item in raw.split(sep) if (cookie := extract_cookie(item))]

class UserAgentConfigurationError(ValueError):
    pass

def normalize_user_agent(raw):
    if any((ord(char) < 32 and char not in '\t\r\n') or ord(char) == 127 for char in raw):
        raise UserAgentConfigurationError('含有无效控制字符，请重新复制浏览器的 User-Agent')
    value = raw.strip()
    if not value:
        return '', False
    if '\r' in value or '\n' in value:
        raise UserAgentConfigurationError('含有换行，请粘贴完整单行 User-Agent')
    try:
        value.encode('latin-1')
    except UnicodeEncodeError:
        raise UserAgentConfigurationError(
            '含有不支持的字符，请重新复制浏览器的 User-Agent，不要加入说明文字或中文引号'
        ) from None
    original = value
    prefix_removed = False
    if value.lower().startswith('user-agent:'):
        value = value[len('user-agent:'):].strip()
        prefix_removed = True
    if len(value) >= 2 and value[0] == value[-1]:
        inner = value[1:-1]
        single_quote_wrapper = value[0] == "'" and (
            not inner or any(char in ' \t' for char in inner)
            or inner.lower().startswith('user-agent:')
        )
        if value[0] == '"' or single_quote_wrapper:
            value = inner.strip()
    if not prefix_removed and value.lower().startswith('user-agent:'):
        value = value[len('user-agent:'):].strip()
    if not value:
        raise UserAgentConfigurationError('只有请求头名称或引号，没有 User-Agent 值')
    if value[0] == '"' or value[-1] == '"':
        raise UserAgentConfigurationError('外层双引号不成对或重复，请只保留 User-Agent 值')
    if len(value) >= 2 and value[0] == value[-1] == "'" and any(char in ' \t' for char in value):
        raise UserAgentConfigurationError('包裹 User-Agent 的单引号重复，请只保留 User-Agent 值')
    if value.lower().startswith('user-agent:'):
        raise UserAgentConfigurationError('请求头名称重复，请只保留 User-Agent 值')
    return value, value != original

def get_browser_headers():
    user_agent, _ = normalize_user_agent(os.environ.get("GLADOS_USER_AGENT", ""))
    user_agent = user_agent or DEFAULT_USER_AGENT
    headers = {'User-Agent': user_agent}
    chrome = re.search(r'(?:Chrome|Chromium)/(\d+)', user_agent)
    if chrome:
        major = chrome.group(1)
        if 'Macintosh' in user_agent:
            platform = 'macOS'
        elif 'Windows' in user_agent:
            platform = 'Windows'
        elif 'Android' in user_agent:
            platform = 'Android'
        elif 'Linux' in user_agent:
            platform = 'Linux'
        else:
            platform = 'Unknown'
        headers.update({
            'Sec-CH-UA': (
                f'"Chromium";v="{major}", '
                f'"Google Chrome";v="{major}", '
                '"Not_A Brand";v="99"'
            ),
            'Sec-CH-UA-Mobile': '?1' if 'Mobile' in user_agent else '?0',
            'Sec-CH-UA-Platform': f'"{platform}"',
        })
    return headers

def is_non_retryable_checkin_result(result):
    if not isinstance(result, dict):
        return False
    code = result.get('code')
    reason = str(result.get('reason', '')).strip().lower()
    message = str(result.get('message', '')).strip().lower()
    return (
        code == -2
        or result.get('http_status') in (401, 403)
        or reason == 'device-mismatch'
        or '没有权限' in message
        or 'permission' in message
        or 'unauthorized' in message
        or 'automated check-in detected' in message
    )

def is_missing_api_response(result):
    return not isinstance(result, dict) or result.get('request_failed') is True

def summarize_checkin_failure(result):
    if not isinstance(result, dict):
        return '未收到有效 API 响应；本次请求失败，今日签到状态未知'
    reason = str(result.get('reason', '')).strip().lower()
    message = str(result.get('message', '')).strip().lower()
    if reason == 'checkin-challenge-unavailable':
        label = '未获取有效签到验证，本次未提交签到，请稍后重试'
    elif reason == 'checkin-challenge-expired':
        label = '签到验证已过期，本次未提交签到，请稍后重试'
    elif result.get('request_failed') is True:
        if result.get('invalid_headers') is True:
            label = '请求头格式无效，请检查 Cookie 和 User-Agent 格式'
        elif result.get('invalid_response') is True:
            label = '服务响应格式异常，未收到有效 API 响应'
        else:
            label = '网络或服务暂时异常，未收到有效 API 响应'
        label += '；本次请求失败，今日签到状态未知'
    elif reason == 'device-mismatch':
        label = '服务端拒绝设备校验；请核对会话和 User-Agent 配置'
    elif 'automated check-in detected' in message:
        label = '服务端拒绝自动签到；请核对会话配置，仍失败时联系服务方'
    elif is_non_retryable_checkin_result(result):
        label = '服务端拒绝会话认证或访问'
    else:
        label = '签到失败'
    details = []
    code = result.get('code')
    if type(code) is int and -999 <= code <= 999:
        details.append(f'code={code}')
    status = result.get('http_status')
    if type(status) is int and 100 <= status <= 599:
        details.append(f'http_status={status}')
    if result.get('request_failed') is True and result.get('timed_out') is True:
        details.append('timeout=true')
    if reason in ('device-mismatch', 'http-auth-rejected'):
        details.append(f'reason={reason}')
    return label + (f" [{', '.join(details)}]" if details else '')

def log_user_agent_configuration():
    try:
        user_agent, normalized = normalize_user_agent(os.environ.get("GLADOS_USER_AGENT", ""))
    except UserAgentConfigurationError as error:
        log(f'❌ User-Agent 配置错误: {error}')
        return False
    status = '已配置' if user_agent else '未配置，使用默认值'
    if normalized:
        status += '（已自动去除外层引号或请求头名称）'
    log('浏览器 User-Agent: ' + status)
    return True

def is_normal_checkin_result(result):
    if is_missing_api_response(result):
        return False
    if is_non_retryable_checkin_result(result):
        return False
    message = str(result.get('message', '')).strip().lower()
    if any(marker in message for marker in NORMAL_CHECKIN_MESSAGES):
        return True
    return result.get('code') == 0

def checkin_with_retry(client, attempts=3, delay_seconds=60):
    attempts = max(1, attempts)
    last_result = None
    for attempt in range(1, attempts + 1):
        last_result = client.checkin()
        if is_normal_checkin_result(last_result):
            return last_result, True
        if is_non_retryable_checkin_result(last_result):
            log(f"❌ 签到认证失败，不再重试: {summarize_checkin_failure(last_result)}")
            return last_result, False
        if attempt < attempts:
            log(f"⚠️ 签到第 {attempt}/{attempts} 次失败，{delay_seconds} 秒后重试")
            time.sleep(max(0, delay_seconds))
    return last_result, False

# ================= 核心逻辑 =================
class GLaDOS:
    def __init__(self, cookie):
        self.cookie = cookie
        self.domain = DOMAINS[0]
        self.email = "?"
        self.left_days = "?"
        self.points = "?"
        self.points_change = "?"
        self.exchange_info = ""
        self.session_cookie_kind = get_session_cookie_kind(cookie)
        if self.session_cookie_kind != 'gld':
            log(
                "⚠️ Cookie 未包含完整的 gld:sess 与 gld:sess.sig；"
                "2026-09 新版接口可能返回“没有权限”"
            )

    def req(self, method, path, data=None, form=False, domains=None):
        browser_headers = get_browser_headers()
        failure = {'request_failed': True, 'reason': 'request-failed'}
        for d in DOMAINS if domains is None else domains:
            try:
                url = f"{d}{path}"
                h = HEADERS.copy()
                h.update(browser_headers)
                h['Cookie'] = self.cookie
                h['Origin'] = d
                h['Referer'] = f"{d}/console/checkin"
                if form:
                    h.pop('Content-Type', None)
                    resp = requests.post(url, headers=h, data=data, timeout=10)
                elif method == 'GET':
                    resp = requests.get(url, headers=h, timeout=10)
                else:
                    resp = requests.post(url, headers=h, json=data, timeout=10)
                if resp.status_code == 200:
                    payload = resp.json()
                    if isinstance(payload, dict):
                        self.domain = d
                        return payload
                    failure['invalid_response'] = True
                    log(f"⚠️ {d} 响应格式异常")
                    continue
                if resp.status_code in (401, 403):
                    return {
                        'http_status': resp.status_code,
                        'reason': 'http-auth-rejected',
                    }
                failure['http_status'] = resp.status_code
                log(f"⚠️ {d} 返回 HTTP {resp.status_code}")
            except (requests.RequestException, ValueError) as e:
                if isinstance(e, requests.Timeout):
                    failure['timed_out'] = True
                elif isinstance(e, requests.exceptions.InvalidHeader):
                    failure['invalid_headers'] = True
                elif isinstance(e, ValueError):
                    failure['invalid_response'] = True
                log(f"⚠️ {d} 请求失败: {type(e).__name__}")
                continue
        return failure

    def get_status(self):
        res = self.req('GET', '/api/user/status')
        if res and 'data' in res:
            d = res['data']
            self.email = d.get('email', 'Unknown')
            self.left_days = str(d.get('leftDays', '?')).split('.')[0]
            return True
        return False

    def get_points(self):
        res = self.req('GET', '/api/user/points')
        if res and 'points' in res:
            self.points = str(res.get('points', '0')).split('.')[0]
            history = res.get('history', [])
            if history:
                last = history[0]
                change = str(last.get('change', '0')).split('.')[0]
                if not change.startswith('-'):
                    change = '+' + change
                self.points_change = change
            # 兑换计划
            plans = res.get('plans', {})
            pts = int(self.points)
            exchange_lines = []
            for plan_id, plan_data in plans.items():
                need = plan_data['points']
                days = plan_data['days']
                if pts >= need:
                    exchange_lines.append(f"✅ {need}分→{days}天 (可兑换)  ")
                else:
                    exchange_lines.append(f"❌ {need}分→{days}天 (差{need-pts}分)  ")
            self.exchange_info = "\n".join(exchange_lines)
            return True
        return False

    def checkin(self):
        challenge = self.req('GET', '/api/user/checkin/challenge')
        if is_non_retryable_checkin_result(challenge) or is_missing_api_response(challenge):
            return challenge
        nonce = challenge.get('nonce')
        ttl = challenge.get('ttl', 300000)
        if (
            challenge.get('code') != 0
            or not isinstance(nonce, str) or not 0 < len(nonce) <= 4096
            or type(ttl) not in (int, float)
            or (type(ttl) is float and not math.isfinite(ttl))
            or ttl <= CHECKIN_CHALLENGE_DELAY * 1000
        ):
            return {'request_failed': True, 'reason': 'checkin-challenge-unavailable'}
        domain = self.domain
        received_at = time.monotonic()
        proof = hashlib.sha256((nonce + '.glados-observe-v1').encode('utf-8')).hexdigest()
        time.sleep(CHECKIN_CHALLENGE_DELAY)
        if (time.monotonic() - received_at) * 1000 >= ttl:
            return {'request_failed': True, 'reason': 'checkin-challenge-expired'}
        return self.req('POST', '/api/user/checkin', {
            'token': urlsplit(domain).hostname,
            'nonce': nonce,
            'proof': proof,
        }, domains=(domain,))

# ================= Server酱推送（旧排版，和你最早版本一致） =================
def serverchan_send(sendkey, title, desp):
    sendkey = (sendkey or "").strip()
    if not sendkey:
        return False
    if sendkey.startswith("sctp"):
        match = re.match(r"sctp([0-9]+)t", sendkey)
        if not match:
            log("❌ Server酱 推送失败: Server酱³ SendKey 格式无效")
            return False
        url = f"https://{match.group(1)}.push.ft07.com/send/{sendkey}.send"
    else:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
    try:
        response = requests.post(url, json={'title': title, 'desp': desp}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('code') != 0:
            log(f"❌ Server酱 推送失败: code={payload.get('code')}")
            return False
        log("✅ Server酱 推送成功")
        return True
    except (requests.RequestException, ValueError) as e:
        log(f"❌ Server酱 推送失败: {type(e).__name__}")
        return False

# ================= 主程序 =================
def main():
    log("🚀 2026 GLaDOS Checkin Starting...")

    # 读取诊断标记
    diagnose_only = os.environ.get("DIAGNOSE_ONLY", "").lower() in ["true", "1", "yes"]
    delay = 0
    real_checkin_time = ""

    # 【修改处】诊断模式跳过前置随机延时，正常签到保留延时
    if not diagnose_only:
        delay = random.randint(0, 1800)
        log(f"⏳ 等待 {delay} 秒后开始签到")
        remain = delay
        while remain > 0:
            step = min(60, remain)
            time.sleep(step)
            remain -= step
            log(f"⏳ 等待中，还剩 {remain} 秒开始签到")
        real_checkin_time = beijing_time()
    else:
        log("🔍 诊断模式，跳过签到延时，直接校验账号")
        real_checkin_time = beijing_time()

    cookies = get_cookies()
    if not cookies:
        return 1
    if not log_user_agent_configuration():
        return 1

    success_cnt = 0
    results = []
    for i, cookie in enumerate(cookies, 1):
        g = GLaDOS(cookie)
        res, is_success = checkin_with_retry(g, attempts=3, delay_seconds=60)
        msg = res.get('message', 'Success') if is_success else summarize_checkin_failure(res)
        if not is_non_retryable_checkin_result(res) and not is_missing_api_response(res):
            g.get_status()
            g.get_points()
        status_icon = "✅" if is_success else "❌"
        log(f"{status_icon} 账号 {i} | 积分: {g.points} | 天数: {g.left_days} | 结果: {msg}")

        # ========= 计数逻辑：只有积分增量为+，才算签到成功，重复签到不计入 =========
        if g.points_change.startswith("+"):
            success_cnt += 1

        # 【和你最早版本一模一样的Markdown排版，末尾两个空格硬换行】
        block = f"""# 🧾 账号{i}
📧 账号邮箱：{g.email}  
💰 当前积分：{g.points} {g.points_change}  
⏱️ 剩余天数：{g.left_days} 天  
✅ 签到结果：{msg}  
💱 兑换信息：  
{g.exchange_info}

"""
        results.append(block)

        # 账号之间随机休眠60~180秒，最后一个账号不休眠
        if i < len(cookies):
            sleep_t = random.randint(60, 180)
            log(f"⏳ 账号间间隔休眠 {sleep_t} 秒")
            time.sleep(sleep_t)

    # ====== 无论成功失败，一定会推送 ======
    serverchan_sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if serverchan_sendkey:
        title = f"GLaDOS签到: 成功{success_cnt}/{len(cookies)}"
        content = ""
        for res_text in results:
            content += res_text
        # 追加延时信息
        if diagnose_only:
            content += f"\n🕒 执行时间：{real_checkin_time}\n⏱ 本次延时：跳过（诊断模式）\n🕒 实际签到时刻：{real_checkin_time}"
        else:
            content += f"\n🕒 执行时间：{real_checkin_time}\n⏱ 本次延时：{delay} 秒\n🕒 实际签到时刻：{real_checkin_time}"
        serverchan_send(serverchan_sendkey, title, content)

    return 0 if success_cnt == len(cookies) else 1

if __name__ == '__main__':
    sys.exit(main())

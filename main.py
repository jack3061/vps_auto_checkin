import os
import json
import time
import html
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional

import requests


# =========================
# Env (保持不改变量名)
# =========================
URL = (os.environ.get("URL") or "").strip().rstrip("/")
CONFIG = os.environ.get("CONFIG") or ""

TG_BOT_TOKEN = (os.environ.get("TG_BOT_TOKEN") or "").strip()
TG_CHAT_ID = (os.environ.get("TG_CHAT_ID") or "").strip()

NOTIFY_ON_SUCCESS = (os.environ.get("NOTIFY_ON_SUCCESS") or "false").strip().lower() in (
    "1", "true", "yes", "y", "on"
)
NOTIFY_TITLE = (os.environ.get("NOTIFY_TITLE") or "Ikuuu机场签到").strip()

DEBUG = (os.environ.get("DEBUG") or "false").strip().lower() in ("1", "true", "yes", "y", "on")
try:
    TIMEOUT = int((os.environ.get("TIMEOUT") or "20").strip())
except Exception:
    TIMEOUT = 20


# =========================
# Helpers
# =========================
def now_cn_str() -> str:
    dt = datetime.now(timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %H:%M:%S") + " (UTC+8)"


def html_escape(s: str) -> str:
    return html.escape(s or "", quote=False)


def mask_email(s: str) -> str:
    s = (s or "").strip()
    if "@" in s:
        name, dom = s.split("@", 1)
        if len(name) <= 2:
            name_mask = name[0] + "*"
        else:
            name_mask = name[0] + "*" * (len(name) - 2) + name[-1]
        return f"{name_mask}@{dom}"
    if len(s) <= 2:
        return s[:1] + "*"
    return s[:1] + "*" * (len(s) - 2) + s[-1:]


def parse_json_maybe(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def is_already_checked_in(msg: str) -> bool:
    """
    “已签到过”也算成功（只备注）
    """
    m = (msg or "").strip()
    if not m:
        return False
    keywords = ["已经", "已", "签到过", "今日", "今天", "似乎已经", "重复", "领取过"]
    return ("签到" in m or "check" in m.lower()) and any(k in m for k in keywords)


def parse_accounts(config_text: str) -> List[Tuple[str, str]]:
    """
    兼容两种格式（但最终只取第一个账号）：
    1) 推荐：每行一个账号：email,password
    2) 兼容：两行一组：email 换行 password
    支持空行 & # 注释
    """
    raw_lines = config_text.splitlines()
    lines: List[str] = []
    for ln in raw_lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        lines.append(ln)

    if not lines:
        raise ValueError("CONFIG 为空：请填写账号密码配置")

    # 逗号格式：每行 email,password
    if any("," in ln for ln in lines):
        accounts: List[Tuple[str, str]] = []
        for ln in lines:
            if "," not in ln:
                raise ValueError("CONFIG 使用逗号格式但存在不含逗号的行，请统一为：email,password")
            email, pwd = ln.split(",", 1)
            email = email.strip()
            pwd = pwd.strip()
            if not email or not pwd:
                raise ValueError(f"CONFIG 行格式错误（email/password 为空）：{ln}")
            accounts.append((email, pwd))
        return accounts

    # 两行一组
    if len(lines) % 2 != 0:
        raise ValueError("CONFIG 两行一组格式错误：行数必须为偶数（邮箱/密码交替）")

    accounts = []
    for i in range(0, len(lines), 2):
        email = lines[i].strip()
        pwd = lines[i + 1].strip()
        if not email or not pwd:
            raise ValueError("CONFIG 两行一组格式错误：存在空邮箱或空密码")
        accounts.append((email, pwd))
    return accounts


def pick_first_account(config_text: str) -> Tuple[str, str]:
    accounts = parse_accounts(config_text)
    return accounts[0]  # ✅ 单账号：只取第一个


def tg_send_html(text_html: str) -> None:
    """
    Telegram HTML 发送（重试；未配置 token/chat_id 则静默跳过）
    """
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return

    api = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    last_err: Optional[str] = None
    for _ in range(3):
        try:
            r = requests.post(api, data=payload, timeout=TIMEOUT)
            if r.status_code == 200:
                return
            last_err = f"{r.status_code} {r.text[:200]}"
        except Exception as e:
            last_err = repr(e)
        time.sleep(1)

    if DEBUG and last_err:
        print(f"[TG] send failed: {last_err}")


# =========================
# Core
# =========================
@dataclass
class CheckinResult:
    email_masked: str
    login_ok: bool
    checkin_ok: bool
    checkin_executed: bool
    already_checked: bool
    reason: str


def sign_one(email: str, password: str) -> CheckinResult:
    masked = mask_email(email)

    if not URL:
        return CheckinResult(masked, False, False, False, False, "URL 未配置（Secrets 里设置 URL）")

    login_url = f"{URL}/auth/login"
    checkin_url = f"{URL}/user/checkin"

    s = requests.Session()
    headers = {
        "origin": URL,
        "referer": f"{URL}/auth/login",
        "user-agent": "Mozilla/5.0",
    }

    try:
        # ---- login
        res = s.post(login_url, headers=headers, data={"email": email, "passwd": password}, timeout=TIMEOUT)
        j = parse_json_maybe(res.text.strip())

        if not isinstance(j, dict) or j.get("ret") not in (1, "1", True):
            msg = (j.get("msg") if isinstance(j, dict) else None) or res.text.strip()[:300]
            return CheckinResult(masked, False, False, False, False, msg)

        # ---- checkin
        res2 = s.post(checkin_url, headers=headers, timeout=TIMEOUT)
        j2 = parse_json_maybe(res2.text.strip())

        if not isinstance(j2, dict):
            return CheckinResult(masked, True, False, True, False, res2.text.strip()[:300])

        if j2.get("ret") in (1, "1", True):
            msg_ok = (j2.get("msg") or "").strip()
            return CheckinResult(masked, True, True, True, False, msg_ok)

        # ret!=1：失败；但“已签到过”=> 算成功（备注原因）
        msg2 = (j2.get("msg") or "").strip()
        already = is_already_checked_in(msg2)
        if already:
            return CheckinResult(masked, True, True, True, True, msg2)

        return CheckinResult(masked, True, False, True, False, msg2)

    except Exception as ex:
        return CheckinResult(masked, False, False, False, False, repr(ex))


def format_notify_html(r: CheckinResult) -> str:
    # 标题样式：📊 + 横线（参考你给的截图）
    title = f"📊 <b>{html_escape(NOTIFY_TITLE)}</b>"
    line = "────────────────────"

    lines = [
        title,
        line,
        f"👤 <b>账号</b>：{html_escape(r.email_masked)}",
    ]

    # 登录
    lines.append(f"🔐 <b>登录</b>：{'✅ 成功' if r.login_ok else '❌ 失败'}")

    # 登录失败：签到未执行
    if not r.login_ok:
        lines.append("📝 <b>签到</b>：⏸ 未执行")
        if r.reason:
            lines.append(f"📌 <b>原因</b>：{html_escape(r.reason)}")
        lines.append(f"🕒 <b>签到时间</b>：{html_escape(now_cn_str())}")
        return "\n".join(lines)

    # 登录成功：签到
    if r.checkin_ok:
        lines.append("📝 <b>签到</b>：✅ 成功")
        if r.already_checked and r.reason:
            lines.append(f"🗒️ <b>备注</b>：{html_escape(r.reason)}")
    else:
        lines.append("📝 <b>签到</b>：❌ 失败")
        if r.reason:
            lines.append(f"📌 <b>原因</b>：{html_escape(r.reason)}")

    lines.append(f"🕒 <b>签到时间</b>：{html_escape(now_cn_str())}")
    return "\n".join(lines)


def main():
    email, pwd = pick_first_account(CONFIG)
    result = sign_one(email, pwd)

    # Actions 日志里打印一行（便于看）
    print(
        f"[{result.email_masked}] "
        f"login={'OK' if result.login_ok else 'FAIL'} "
        f"checkin={'OK' if result.checkin_ok else 'FAIL'} "
        f"{'(already)' if result.already_checked else ''} "
        f"reason={result.reason[:120] if result.reason else ''}"
    )

    text_html = format_notify_html(result)

    # 失败：一定通知 + exit 1
    # 成功：默认静默；开关打开才通知
    hard_fail = (not result.login_ok) or (result.login_ok and result.checkin_executed and not result.checkin_ok)
    if hard_fail:
        tg_send_html(text_html)
        raise SystemExit(1)

    if NOTIFY_ON_SUCCESS:
        tg_send_html(text_html)


if __name__ == "__main__":
    main()

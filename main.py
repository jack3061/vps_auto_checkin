import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional


# ====== Env ======
URL = (os.environ.get("URL") or "").strip().rstrip("/")
CONFIG = os.environ.get("CONFIG") or ""

TG_BOT_TOKEN = (os.environ.get("TG_BOT_TOKEN") or "").strip()
TG_CHAT_ID = (os.environ.get("TG_CHAT_ID") or "").strip()

NOTIFY_ON_SUCCESS = (os.environ.get("NOTIFY_ON_SUCCESS") or "false").strip().lower() in ("1", "true", "yes", "y", "on")
ALREADY_CHECKEDIN_IS_SUCCESS = (os.environ.get("ALREADY_CHECKEDIN_IS_SUCCESS") or "true").strip().lower() in ("1", "true", "yes", "y", "on")
NOTIFY_TITLE = (os.environ.get("NOTIFY_TITLE") or "Ikuuu机场签到").strip()

DEBUG = (os.environ.get("DEBUG") or "false").strip().lower() in ("1", "true", "yes", "y", "on")
try:
    TIMEOUT = int((os.environ.get("TIMEOUT") or "20").strip())
except Exception:
    TIMEOUT = 20


def now_cn_str() -> str:
    # UTC+8 时间
    dt = datetime.now(timezone(timedelta(hours=8)))
    return dt.strftime("%Y-%m-%d %H:%M:%S") + " (UTC+8)"


def _mask_email(s: str) -> str:
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


def parse_accounts(config_text: str) -> List[Tuple[str, str]]:
    """
    支持两种格式：
    1) 推荐：每行一个账号：email,password
    2) 兼容：两行一组：email 换行 password
    支持：空行、#注释行
    """
    raw_lines = config_text.splitlines()
    lines = []
    for ln in raw_lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        lines.append(ln)

    if not lines:
        raise ValueError("CONFIG 为空：请填写账号密码配置")

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


def tg_send(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return

    api = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }

    last_err: Optional[Exception] = None
    for _ in range(3):
        try:
            r = requests.post(api, data=payload, timeout=TIMEOUT)
            if r.status_code == 200:
                return
            last_err = RuntimeError(f"TG send failed: {r.status_code} {r.text[:200]}")
        except Exception as e:
            last_err = e
        time.sleep(1)

    if DEBUG and last_err:
        print(f"[TG] send failed: {last_err}")


def parse_json_maybe(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def is_already_checked_in(msg: str) -> bool:
    m = (msg or "")
    # 常见提示：已经签到 / 似乎已经签到过了 / 今日已签到 等
    keywords = ["已经", "已", "签到过", "今日", "今天"]
    return ("签到" in m) and any(k in m for k in keywords)


def format_notify(masked_email: str, login_ok: bool, checkin_ok: bool, reason: str, already: bool) -> str:
    t = now_cn_str()
    lines = [
        f"",
        f"账号：{masked_email}",
        f"登录：{'成功' if login_ok else '失败'}",
    ]

    if checkin_ok:
        if already:
            lines.append("签到：成功（已签到过）")
            if reason:
                lines.append(f"原因：{reason}")
        else:
            lines.append("签到：成功")
    else:
        lines.append("签到：失败")
        if reason:
            lines.append(f"原因：{reason}")

    lines.append(f"签到时间：{t}")
    return "\n".join(lines)


def sign_one(email: str, password: str) -> Tuple[bool, bool, bool, str]:
    """
    返回：(login_ok, checkin_ok, already_checked_in, reason_msg)
    """
    if not URL:
        return False, False, False, "URL 未配置（Secrets 里设置 URL）"

    login_url = f"{URL}/auth/login"
    checkin_url = f"{URL}/user/checkin"

    s = requests.Session()
    headers = {
        "origin": URL,
        "referer": f"{URL}/auth/login",
        "user-agent": "Mozilla/5.0",
    }

    try:
        # login
        res = s.post(login_url, headers=headers, data={"email": email, "passwd": password}, timeout=TIMEOUT)
        j = parse_json_maybe(res.text.strip())

        if not isinstance(j, dict) or j.get("ret") not in (1, "1", True):
            msg = (j.get("msg") if isinstance(j, dict) else None) or res.text.strip()[:200]
            return False, False, False, msg

        # checkin
        res2 = s.post(checkin_url, headers=headers, timeout=TIMEOUT)
        j2 = parse_json_maybe(res2.text.strip())

        if not isinstance(j2, dict):
            return True, False, False, res2.text.strip()[:200]

        if j2.get("ret") in (1, "1", True):
            return True, True, False, (j2.get("msg") or "").strip()

        msg2 = (j2.get("msg") or "").strip()
        already = is_already_checked_in(msg2)

        if already and ALREADY_CHECKEDIN_IS_SUCCESS:
            # 当作成功，但保留原因用于展示
            return True, True, True, msg2

        return True, False, already, msg2

    except Exception as ex:
        return False, False, False, repr(ex)


def main():
    accounts = parse_accounts(CONFIG)

    any_fail = False
    fail_texts: List[str] = []
    ok_texts: List[str] = []

    for email, pwd in accounts:
        masked = _mask_email(email)
        login_ok, checkin_ok, already, reason = sign_one(email, pwd)

        text = format_notify(masked, login_ok, checkin_ok, reason, already)

        # 失败一定收集；成功按开关决定是否通知
        if not login_ok or not checkin_ok:
            any_fail = True
            fail_texts.append(text)
        else:
            ok_texts.append(text)

        # 日志打印（Actions 里看）
        print(text)
        print("-" * 40)

    # Telegram：默认仅失败通知
    if any_fail:
        tg_send("\n\n".join(fail_texts))
        raise SystemExit(1)

    if NOTIFY_ON_SUCCESS and ok_texts:
        tg_send("\n\n".join(ok_texts))


if __name__ == "__main__":
    main()

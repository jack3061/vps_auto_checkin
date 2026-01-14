import os
import json
import time
import requests
from typing import List, Tuple, Optional

# ====== Env ======
URL = (os.environ.get("URL") or "").strip().rstrip("/")
CONFIG = os.environ.get("CONFIG") or ""

TG_BOT_TOKEN = (os.environ.get("TG_BOT_TOKEN") or "").strip()
TG_CHAT_ID = (os.environ.get("TG_CHAT_ID") or "").strip()

NOTIFY_ON_SUCCESS = (os.environ.get("NOTIFY_ON_SUCCESS") or "false").strip().lower() in ("1", "true", "yes", "y", "on")
DEBUG = (os.environ.get("DEBUG") or "false").strip().lower() in ("1", "true", "yes", "y", "on")

try:
    TIMEOUT = int((os.environ.get("TIMEOUT") or "20").strip())
except Exception:
    TIMEOUT = 20


def _mask_email(s: str) -> str:
    s = (s or "").strip()
    if "@" in s:
        name, dom = s.split("@", 1)
        if len(name) <= 2:
            name_mask = name[0] + "*"
        else:
            name_mask = name[0] + "*" * (len(name) - 2) + name[-1]
        return f"{name_mask}@{dom}"
    # 非邮箱也简单打码
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
        if not ln:
            continue
        if ln.startswith("#"):
            continue
        lines.append(ln)

    if not lines:
        raise ValueError("CONFIG 为空：请填写账号密码配置")

    # 如果任意行包含逗号，按“每行一个账号”解析
    if any("," in ln for ln in lines):
        accounts: List[Tuple[str, str]] = []
        for ln in lines:
            if "," not in ln:
                raise ValueError("CONFIG 使用了逗号格式，但存在不含逗号的行，请统一为：email,password")
            email, pwd = ln.split(",", 1)
            email = email.strip()
            pwd = pwd.strip()
            if not email or not pwd:
                raise ValueError(f"CONFIG 行格式错误（email/password 为空）：{ln}")
            accounts.append((email, pwd))
        return accounts

    # 否则按“两行一组”
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
    """发送 Telegram 通知；未配置 token/chat_id 则静默跳过"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return

    api = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }

    # 简单重试
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


def sign_one(index: int, email: str, password: str) -> Tuple[bool, str]:
    """
    返回 (success, message)
    success=True：签到成功（不一定通知，取决于 NOTIFY_ON_SUCCESS）
    success=False：失败（一定通知）
    """
    if not URL:
        return False, "URL 未配置（Secrets 里设置 URL）"

    login_url = f"{URL}/auth/login"
    checkin_url = f"{URL}/user/checkin"

    session = requests.Session()
    headers = {
        "origin": URL,
        "referer": f"{URL}/auth/login",
        "user-agent": "Mozilla/5.0",
    }

    try:
        if DEBUG:
            print(f"=== [{index}] login: {email} ===")

        # 登录
        data = {"email": email, "passwd": password}
        res = session.post(login_url, headers=headers, data=data, timeout=TIMEOUT)
        txt = res.text.strip()

        # 有些站返回 JSON
        try:
            j = json.loads(txt)
        except Exception:
            j = None

        if not j or (isinstance(j, dict) and j.get("ret") not in (1, "1", True)):
            msg = ""
            if isinstance(j, dict):
                msg = j.get("msg") or str(j)
            else:
                msg = txt[:200]
            return False, f"[{_mask_email(email)}] 登录失败：{msg}"

        login_msg = j.get("msg") or "登录成功"

        # 签到
        res2 = session.post(checkin_url, headers=headers, timeout=TIMEOUT)
        txt2 = res2.text.strip()
        try:
            j2 = json.loads(txt2)
        except Exception:
            j2 = None

        if not j2 or (isinstance(j2, dict) and j2.get("ret") not in (1, "1", True)):
            msg2 = ""
            if isinstance(j2, dict):
                msg2 = j2.get("msg") or str(j2)
            else:
                msg2 = txt2[:200]
            return False, f"[{_mask_email(email)}] 签到失败：{msg2}"

        checkin_msg = j2.get("msg") or "签到成功"

        # 成功信息（默认不通知）
        ok_text = f"[{_mask_email(email)}] {login_msg}；{checkin_msg}"
        return True, ok_text

    except Exception as ex:
        return False, f"[{_mask_email(email)}] 异常：{repr(ex)}"


def main():
    accounts = parse_accounts(CONFIG)

    any_fail = False
    success_msgs = []
    fail_msgs = []

    for idx, (email, pwd) in enumerate(accounts, start=1):
        ok, msg = sign_one(idx, email, pwd)
        if ok:
            success_msgs.append(f"{idx}. {msg}")
        else:
            any_fail = True
            fail_msgs.append(f"{idx}. {msg}")

    # 仅失败通知（默认）
    if any_fail:
        text = "❌ 机场签到失败\n\n" + "\n".join(fail_msgs)
        # 如果有成功的也顺带带上，方便排查（可删）
        if success_msgs and DEBUG:
            text += "\n\n✅ 本次成功（DEBUG）\n" + "\n".join(success_msgs)
        tg_send(text)
        print(text)
        # 让 Actions 标红更直观
        raise SystemExit(1)

    # 全部成功：默认静默；如开启 NOTIFY_ON_SUCCESS 才通知
    text_ok = "✅ 机场签到成功\n\n" + "\n".join(success_msgs)
    print(text_ok)
    if NOTIFY_ON_SUCCESS:
        tg_send(text_ok)


if __name__ == "__main__":
    main()

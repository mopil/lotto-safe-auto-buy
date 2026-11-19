"""알림 — 텔레그램 하나. 설정에서 알림을 끄면 Actions 실행 요약(로그)에만 남는다.

send(text) 는 '보냄'으로 쳐도 되면 True: 텔레그램으로 실제 나갔거나, 사용자가 알림을 꺼 둬서 로그에만 남기면 되는 경우.
dry-run 이거나 텔레그램을 켰는데 못 보냈으면 False — 부르는 쪽은 True 일 때만 '알렸다'로 적는다 (다음 실행이 다시 보낸다).
"""
from __future__ import annotations

import json
import os
import urllib.request

import settings

DRY_RUN = False
TIMEOUT = 20
API = "https://api.telegram.org/bot{token}/{method}"


def _post(url: str, body: dict | None = None) -> dict:
    """요청 한 번 (재시도 없음). 오류는 그대로 예외."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json", "User-Agent": "lotto-safe-auto-buy"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:   # noqa: S310  (https 고정 URL)
        return json.loads(resp.read().decode("utf-8") or "{}")


def _summary(text: str) -> None:
    """GitHub Actions 실행 요약 페이지에도 남긴다 (알림을 꺼 둔 사람도 여기서 본다)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n```\n" + text + "\n```\n")
    except OSError:
        pass


def _target() -> tuple[str | None, str | None, bool]:
    """(토큰, 채팅 ID, 알림 켬?)."""
    try:
        cfg = settings.load_config()
    except settings.SettingsError:
        cfg = settings.DEFAULT_CONFIG
    chat = cfg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
    return settings.secret("TELEGRAM_TOKEN"), chat, bool(cfg.get("telegram"))


def send(text: str, silent: bool | None = None) -> bool:
    if DRY_RUN:
        print("[DRY]\n" + text)
        return False
    print(text)
    _summary(text)
    token, chat, on = _target()
    if not on:
        return True                      # 알림 끔 — 로그·요약에 남긴 걸로 끝
    if not token or not chat:
        print("[lotto] 텔레그램 토큰이나 채팅 ID 가 없어서 못 보냄 — '① 설정' 을 다시 실행해 주세요")
        return False
    body = {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}
    if silent is not None:
        body["disable_notification"] = bool(silent)
    try:
        return bool(_post(API.format(token=token, method="sendMessage"), body).get("ok"))
    except Exception as e:  # noqa: BLE001  (알림 실패는 구매를 막지 않는다 — '보냄' 기록만 안 남는다)
        print(f"[lotto] 텔레그램 발송 실패: {type(e).__name__}")
        return False


def notify(source: str, items: list[tuple[str, str, str, str]]) -> bool:
    """🚨 경보 한 통 (알림음 켬). items = [(분류, 이름, 사유, 상세)]."""
    if not items:
        return False
    lines = [f"🚨 {reason}\n{detail}" for _sec, _name, reason, detail in items]
    return send("\n\n".join(lines), silent=False)


def write_brief(source: str, items) -> None:
    """(원본 시스템의 하루 요약 자리 — 이 레포에선 하지 않는다)"""
    return None


def discover_chat(token: str, get=_post) -> str | None:
    """봇에게 가장 최근에 말을 건 대화의 채팅 ID. 아무도 말을 안 걸었으면 None."""
    data = get(API.format(token=token, method="getUpdates"))
    best = None
    for upd in data.get("result") or []:
        msg = upd.get("message") or upd.get("edited_message") or upd.get("channel_post") or {}
        chat = (msg.get("chat") or {}).get("id")
        if chat is not None:
            best = (upd.get("update_id", 0), str(chat)) if best is None or upd.get("update_id", 0) >= best[0] else best
    return best[1] if best else None


def send_test(token: str, chat: str, post=_post) -> bool:
    body = {"chat_id": chat, "text": "✅ lotto-safe-auto-buy 연결됐어요. 구매·결과 알림이 여기로 옵니다."}
    return bool(post(API.format(token=token, method="sendMessage"), body).get("ok"))

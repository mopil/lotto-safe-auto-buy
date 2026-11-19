"""'① 설정' · '② 멈춤' 워크플로가 부르는 스크립트. 구매는 절대 안 한다.

    python lotto_safe/configure.py setup   (환경변수 FORM_BUY · FORM_LOTTO_GAMES · FORM_PENSION_MODE · FORM_TELEGRAM)
    python lotto_safe/configure.py stop

setup:
  1. 비밀값(LOTTO_SECRETS) 모양 확인 — 아이디·비밀번호가 있나
  2. 로그인 확인만 (구매 X) — 예치금 · 이번 회차 이미 산 장 수. 로그인 멈춤 기록(login_blocked)은 여기서 푼다 (사람이 다시 설정한 것)
  3. 텔레그램(받음일 때만): 봇에게 가장 최근에 말을 건 대화를 찾아 테스트 메시지
  4. 다 통과하면 config.json 저장 + 켜기 → state 브랜치에 기록
  하나라도 실패하면 켜지 않는다. 결과는 실행 요약 페이지에 ✅·❌ 표로.
"""
from __future__ import annotations

import json
import os
import sys

import channel
import lotto
import settings

BUY_LABELS = {"둘 다": "both", "로또만": "lotto", "연금만": "pension", "both": "both", "lotto": "lotto", "pension": "pension"}
PENSION_LABELS = {"매주 직접 구매": "weekly", "5회차 예약 (실험적)": "reserve", "weekly": "weekly", "reserve": "reserve"}
TELEGRAM_LABELS = {"받음": True, "안 받음": False, "true": True, "false": False}


def summary(rows: list[tuple[str, bool | None, str]], title: str) -> None:
    lines = [f"## {title}", "", "| 단계 | 결과 | 내용 |", "|---|---|---|"]
    for step, ok, note in rows:
        mark = "✅" if ok else ("➖" if ok is None else "❌")
        lines.append(f"| {step} | {mark} | {note} |")
    text = "\n".join(lines) + "\n"
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)


def form_config() -> dict:
    buy = BUY_LABELS.get((os.environ.get("FORM_BUY") or "").strip())
    mode = PENSION_LABELS.get((os.environ.get("FORM_PENSION_MODE") or "").strip())
    tg = TELEGRAM_LABELS.get((os.environ.get("FORM_TELEGRAM") or "").strip())
    if buy is None or mode is None or tg is None:
        raise settings.SettingsError("설정 폼 값을 못 읽음 — 워크플로 폼에서 다시 골라 주세요")
    return settings.config_from_form(buy, int(os.environ.get("FORM_LOTTO_GAMES") or "1"), mode, tg, None)


def check_login(cfg: dict) -> tuple[bool, str]:
    """로그인 + 예치금 + 최근 구매내역만 본다. 돈 나가는 호출은 없다."""
    state = lotto.load_state()
    if state.pop("login_blocked", None) is not None or state.get("login_fail_streak"):
        state["login_fail_streak"] = 0
        lotto.save_state(state)                         # 사람이 다시 설정했다 → 로그인 멈춤을 푼다
    session = lotto.login()
    if session is None:
        return False, "로그인 실패 — 아이디·비밀번호(LOTTO_SECRETS)를 확인해 주세요 (자세한 사유는 위 로그)"
    client, capture = session
    balance = lotto.read_balance(client, capture, buying=False)
    rounds, _closed = lotto.open_rounds(lotto.now_kst())
    try:
        orders = lotto.fetch_orders(client, capture, lotto.BUY_LOOKBACK_DAYS)
        have = " · ".join(f"{lotto.GAME_LABEL[g]} {r}회 {lotto.tickets_in(orders, g, r)}{lotto.GAME_UNIT[g]}" for g, r in rounds.items())
    except Exception as e:  # noqa: BLE001
        have = f"구매내역 못 읽음 ({type(e).__name__})"
    money = lotto.won(balance) if balance is not None else "못 읽음"
    return True, f"예치금 {money}" + (f" · 이번 회차 {have}" if have else "")


def check_telegram(cfg: dict) -> tuple[bool | None, str, str | None]:
    if not cfg["telegram"]:
        return None, "알림 안 받음 — 결과는 Actions 실행 요약에만 남아요", None
    token = settings.secret("TELEGRAM_TOKEN")
    if not token:
        return False, "LOTTO_SECRETS 에 TELEGRAM_TOKEN 줄이 없어요", None
    try:
        chat = channel.discover_chat(token)
    except Exception as e:  # noqa: BLE001
        return False, f"봇 토큰으로 텔레그램을 못 읽음 ({type(e).__name__}) — 토큰을 확인해 주세요", None
    if not chat:
        return False, "봇에게 온 메시지가 없어요 — 텔레그램에서 내 봇에게 아무 말이나 보낸 뒤 다시 실행해 주세요", None
    try:
        ok = channel.send_test(token, chat)
    except Exception as e:  # noqa: BLE001
        return False, f"테스트 메시지 실패 ({type(e).__name__})", None
    return (True, "연결됨 — 텔레그램에 테스트 메시지를 보냈어요", chat) if ok else (False, "테스트 메시지 실패", None)


def setup() -> int:
    rows: list[tuple[str, bool | None, str]] = []
    try:
        settings.mask_secrets()
        cfg = form_config()
    except settings.SettingsError as e:
        summary([("설정 값", False, str(e))], "① 설정 결과")
        return 1
    games = " + ".join(lotto.GAME_LABEL[g] for g in cfg["games"])
    rows.append(("고른 것", True, f"{games} · 로또 회차당 {cfg['lotto_games']}게임 · 연금 "
                 f"{'5회차 예약' if cfg['pension_mode'] == 'reserve' else '매주 1세트(5장)'}"))
    lotto.apply_config(cfg)
    lotto.db.init()
    try:
        ok, note = check_login(cfg)
    except Exception as e:  # noqa: BLE001
        ok, note = False, f"{type(e).__name__}: {str(e)[:150]}"
    rows.append(("로그인 확인 (구매 안 함)", ok, note))
    tg_ok, tg_note, chat = check_telegram(cfg)
    rows.append(("텔레그램", tg_ok, tg_note))
    if ok and tg_ok is not False:
        cfg["chat_id"] = chat
        settings.save_config(cfg)
        try:
            backend_state = lotto.backend().state
            if isinstance(backend_state, lotto.GitBranchStore):
                lotto.db.close()
                backend_state.persist(f"setup {lotto.stamp()}")
            rows.append(("자동 구매 켜기", True, "켜짐 — 매주 월요일 오전(+수요일 보충)에 사요. 한도를 넘는 구매는 하지 않아요"))
        except lotto.StateError as e:
            rows.append(("자동 구매 켜기", False, f"설정을 state 브랜치에 못 남김: {e}"))
            ok = False
    else:
        rows.append(("자동 구매 켜기", False, "위에서 ❌ 가 있어서 켜지 않았어요. 고친 뒤 '① 설정' 을 다시 실행해 주세요"))
    summary(rows, "① 설정 결과")
    return 0 if ok and tg_ok is not False else 1


def stop() -> int:
    cfg = settings.load_config()
    cfg["enabled"] = False
    settings.save_config(cfg)
    try:
        backend_state = lotto.backend().state
        if isinstance(backend_state, lotto.GitBranchStore):
            backend_state.persist(f"stop {lotto.stamp()}")
    except lotto.StateError as e:
        summary([("멈춤", False, f"state 브랜치에 못 남김: {e}")], "② 멈춤 결과")
        return 1
    summary([("멈춤", True, "자동 구매를 껐어요. 다시 켜려면 '① 설정' 을 실행해 주세요")], "② 멈춤 결과")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "setup":
        return setup()
    if command == "stop":
        return stop()
    print(json.dumps({"ok": False, "error": "setup | stop"}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    sys.exit(main())

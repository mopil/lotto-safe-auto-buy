"""로또 6/45 + 연금복권720+ 주간 자동 구매·결과 확인 — 안전장치가 먼저인 구현.

    python lotto_safe/lotto.py buy   [--dry-run] [--no-jitter]   이번 회차 로또(남은 게임) + 연금(비어 있을 때만 · 또는 5회차 예약)
    python lotto_safe/lotto.py check [--dry-run] [--no-jitter]   추첨 끝난 회차 결과를 쌓고 결과 알림 1통

한도: config.json 의 회차당 상한(로또 1-5게임 · 연금 세트 1개=5장)을 넘지 않는다. 결제 요청 직전마다 '이 요청 뒤 회차별 장 수'를
사이트 구매·예약내역 · 로컬 DB · 시도 기록(셋 중 큰 값)으로 다시 센다 (assert_round_caps) — 하나라도 못 세면 안 산다.
중복 방지: ① 한 번에 하나만 실행 (로컬 = 파일 잠금 · Actions = 워크플로 concurrency) ② 사이트 구매·예약내역
③ 시도 기록 (결제 요청 직전에 쓰고, 이미 적힌 회차면 예외 → 결제 안 함). 기록이 깨졌거나 못 쓰면 사지 않는다. 결제 요청은 어떤 것도 재시도하지 않는다.
연금 예약은 실행의 맨 마지막에, 앞 단계가 깨끗하고 예치금이 계산과 같을 때만 넣고, 요청 뒤 예약내역(회차당 딱 5장)·예치금(딱 25,000원)으로 대조한다.
로그인 최소화 (약관상 반복·규칙적 호출 금지): 로그인 한 번에 다 하고, DB·기록으로 할 일이 없으면 로그인 없이 끝내고, 예약 실행(LOTTO_JITTER=1)에선
로그인 전에 0-30분 랜덤 대기. 비번 오류 1번·그 밖의 로그인 실패 연속 2번이면 기록해 두고 사람이 풀 때까지 로그인 안 함.
로그인한 김에 이상징후(내가 안 산 주문·다른 복권·설명 안 되는 예치금 감소·되던 로그인이 비번 오류로 바뀜)를 보고, 있으면 🚨 한 통 + 그 실행은 구매 중단.
연금 결과는 공개 당첨번호로 직접 판정 — 로그인 없이. 회차는 날짜 공식으로 계산하고 사기 직전 사이트 회차와 대조한다.
번호는 전부 자동. AI 는 이 경로에 없다 — 구매는 늘 같은 결과를 내는 이 스크립트가 한다.
dhapi 4.2.4 로 버전을 고정해서 그 안의 비공개 메서드(_get_lotto645_ticket_detail·_parse_buy_list_json·_session)를 쓴다 —
버전을 올리면 이 파일부터 다시 맞춘다. 연금 구매·예약 흐름은 pension720.py.
저장소: LOTTO_BACKEND=local(기본 — 상태 폴더 ~/.lotto-safe · 파일 잠금) | actions(상태 폴더 = 같은 레포 state 브랜치, 쓸 때마다 커밋·푸시,
못 쓰면 안 삼 — store.py). 계정은 LOTTO_SECRETS(DH_ID·DH_PW) 또는 같은 이름 환경변수. 알림은 텔레그램(channel.py).
마지막 줄 JSON: {"ok": true} · {"skipped": 이유} · {"ok": false, "error": ...}.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time as time_mod
from collections import namedtuple
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from dhapi.domain.lotto645_ticket import Lotto645Ticket
from dhapi.domain.user import User
from dhapi.port.lottery_client import LotteryClient

import channel
import db
import pension720
import settings
import store

HERE = Path(__file__).resolve().parent

KST = timezone(timedelta(hours=9))
LOTTO, PENSION = "lotto645", "pension720"          # lotto_order.game 값
GAME_LABEL = {LOTTO: "로또", PENSION: "연금720"}
GAME_UNIT = {LOTTO: "게임", PENSION: "장"}

# ---- 회차당 상한 — config.json 에서 정한다 (apply_config). 당첨이 나와도 자동으로 안 올린다 ----
GAME_PRICE = 1000                 # 원 — 로또 1게임 = 연금 1장
MAX_GAMES_PER_ROUND = 1           # 로또 한 회차 게임 수 (config lotto_games, 1-5 — 5게임 = 온라인 1회 한도 5,000원)
PENSION_SETS_PER_ROUND = 1        # 연금 한 회차에 모든 조 세트 1개
PENSION_TICKETS_PER_SET = 5       # 세트 = 같은 번호로 1-5조 5장
PENSION_MAX_TICKETS = PENSION_SETS_PER_ROUND * PENSION_TICKETS_PER_SET   # 연금 한 회차 5장
PENSION_RESERVE_ROUNDS = 5        # 예약 한 번 = 다음 회차부터 연속 5회차 (사이트 최대) → 한 요청 25,000원
WEEKLY_CAP_WON = MAX_GAMES_PER_ROUND * GAME_PRICE + PENSION_MAX_TICKETS * GAME_PRICE   # 주 환산 상한 = 고른 게임의 회차 상한 합
ROUND_CAP = {LOTTO: MAX_GAMES_PER_ROUND, PENSION: PENSION_MAX_TICKETS}   # 회차당 상한 (게임·장)
REQUEST_MAX_ROUNDS = {LOTTO: 1, PENSION: PENSION_RESERVE_ROUNDS}         # 결제 요청 하나가 건드릴 수 있는 회차 수
RESERVE = "reserve"               # 할 일 이름 — 연금 예약 (게임 이름 아님)
PENSION_RESERVE_ENABLED = False   # config pension_mode = reserve 면 True (사용자가 '① 설정' 에서 고른 것)
ENABLED_GAMES = {LOTTO, PENSION}  # config games — 고르지 않은 게임은 아예 안 산다
LOTTO_4TH_PRIZE = 50_000          # 로또 4등 고정 금액
MAN, EOK = 10_000, 100_000_000
MINUS = "−"                       # 알림 글의 마이너스 (U+2212)


def won(x: int, signed: bool = False) -> str:
    """한국식 금액. 5,000원 · 5만원 · 1만 2,000원 · 5,400만원 · 20억원 · 19억 5,000만원 · −1만 2,000원 · 0원.
    signed=True 면 양수에 + (결과·손익용). 모든 알림·경보 금액은 이 함수 하나로."""
    x = int(x)
    if x == 0:
        return "0원"
    sign = MINUS if x < 0 else ("+" if signed else "")
    n = abs(x)
    eok, rest = divmod(n, EOK)
    man, one = divmod(rest, MAN)
    parts = []
    if eok:
        parts.append(f"{eok:,}억")
    if man:
        parts.append(f"{man:,}만")
    if one:
        parts.append(f"{one:,}")
    return sign + " ".join(parts) + "원"


SET_COST = PENSION_TICKETS_PER_SET * GAME_PRICE                 # 연금 세트 5,000원
RESERVE_COST = PENSION_RESERVE_ROUNDS * PENSION_MAX_TICKETS * GAME_PRICE   # 예약 한 번 25,000원


class CapError(RuntimeError):
    """회차 상한을 넘거나 그 회차에 이미 있는 양을 셀 수 없음 — 사지 않는다."""


class StateError(RuntimeError):
    """상태 기록을 못 읽거나 이미 적힌 시도를 또 적으려 함 — 사지 않는다."""


def validate_caps() -> None:
    """상수끼리 어긋나면 시작부터 멈춘다 (import 때 + 설정 적용 때 + buy 시작 때). assert 가 아니라 raise — python -O 에도 안 꺼진다."""
    # 1 <= … <= 5 : 온라인 1회 한도 5게임
    if not 1 <= MAX_GAMES_PER_ROUND <= settings.LOTTO_GAMES_MAX:
        raise CapError(f"로또 회차 상한 {MAX_GAMES_PER_ROUND}게임이 1-{settings.LOTTO_GAMES_MAX} 밖")
    per_round_total = sum(ROUND_CAP[g] for g in ENABLED_GAMES) * GAME_PRICE
    # > : 회차당 상한 합이 주 상한과 딱 같은 건 허용
    if per_round_total > WEEKLY_CAP_WON:
        raise CapError(f"회차당 상한 합 {won(per_round_total)} > 주 상한 {won(WEEKLY_CAP_WON)}")
    if ROUND_CAP != {LOTTO: MAX_GAMES_PER_ROUND, PENSION: PENSION_MAX_TICKETS}:
        raise CapError("회차 상한 표(ROUND_CAP)가 상수와 다름")
    if PENSION_MAX_TICKETS != PENSION_TICKETS_PER_SET:
        raise CapError(f"연금 회차 상한 {PENSION_MAX_TICKETS}장이 세트 1개({PENSION_TICKETS_PER_SET}장)가 아님")
    if PENSION_TICKETS_PER_SET * GAME_PRICE != pension720.SET_PRICE:
        raise CapError("연금 세트 가격 상수 불일치")
    # 1 <= … <= 5 : 사이트 예약 최대(연속 5회차) 안에서만
    if not 1 <= PENSION_RESERVE_ROUNDS <= pension720.RESERVE_MAX_ROUNDS or REQUEST_MAX_ROUNDS != {LOTTO: 1, PENSION: PENSION_RESERVE_ROUNDS}:
        raise CapError(f"예약 회차 수 {PENSION_RESERVE_ROUNDS} 가 사이트 최대 {pension720.RESERVE_MAX_ROUNDS} 를 넘거나 요청 한도 표와 다름")
    if not ENABLED_GAMES or not ENABLED_GAMES <= {LOTTO, PENSION}:
        raise CapError(f"살 게임이 이상함: {sorted(ENABLED_GAMES)}")


def apply_config(cfg: dict) -> None:
    """config.json(검증된 dict) → 회차 상한·살 게임·연금 방식. 바꾼 뒤 validate_caps."""
    global MAX_GAMES_PER_ROUND, ROUND_CAP, WEEKLY_CAP_WON, PENSION_RESERVE_ENABLED, ENABLED_GAMES
    cfg = settings.validate_config(cfg)
    ENABLED_GAMES = set(cfg["games"])
    MAX_GAMES_PER_ROUND = cfg["lotto_games"]
    ROUND_CAP = {LOTTO: MAX_GAMES_PER_ROUND, PENSION: PENSION_MAX_TICKETS}
    WEEKLY_CAP_WON = sum(ROUND_CAP[g] for g in ENABLED_GAMES) * GAME_PRICE
    PENSION_RESERVE_ENABLED = PENSION in ENABLED_GAMES and cfg["pension_mode"] == "reserve"
    validate_caps()

validate_caps()

SALES_START = time(6, 0)          # 매일 00:00-06:00 판매 정지 (2026-11-16 00:22 로또 '판매시간이 아닙니다' 실측) — 두 게임 다 06:00 부터
SAT_BUY_CUTOFF = time(19, 30)     # 로또 판매 마감 토 20:00 보다 30분 앞 — 네트워크 지연 여유
PENSION_LAST_BUY_WEEKDAY = 3      # 연금은 월(0)-목(3)에만 산다 — 그 주 목요일 추첨분. 금-토엔 로또만 (로그인 한 번)
PENSION_THU_CUTOFF = time(16, 30) # 연금 판매 마감 목 17:00 보다 30분 앞
BUY_LOOKBACK_DAYS = 7             # 이번 회차 구매는 최근 7일 안에만 있다 (로또 월 06:00 · 연금 지난 목 22:00 판매 시작)
RESERVE_LOOKBACK_DAYS = 60        # 예약 내역은 예약일로 찾는다 — 5주 앞까지 예약하니 60일이면 살아 있는 예약이 다 들어온다 (사이트 최대 3개월)
CHECK_LOOKBACK_DAYS = 21          # check 가 몇 주 못 돌아도 다음 실행이 줍는다
RESULT_LATE_DAYS = 2              # 토 추첨 → 월요일(2일 뒤)까지 로또 결과가 없으면 경보
PENSION_RESULT_LATE_DAYS = 1      # 목 추첨 → 금요일까지 연금 당첨번호가 없으면 경보
RESULT_READY = time(21, 0)        # 토 20:35 추첨 → 21:00 부터 그 회차 결과를 보러 간다
PENSION_RESULT_READY = time(20, 0)  # 목 19:05 추첨 → 20:00 부터
MAX_JITTER_MINUTES = 30           # 예약 실행 때 로그인 전 랜덤 대기 상한 (규칙적 호출 티 줄이기)
FIRST_ROUND_DRAW = date(2002, 12, 7)
LOTTO_NAME = "로또6/45"
LOTTO_CODE = "LO40"               # 구매내역 ltGdsCd (사이트 mylotteryledger 화면 코드)
# 로또 결과 문구 (사이트 화면 코드 기준: 당첨·낙첨·미확인·미추첨). 아래 두 묶음에 없는 문구는 '낯선 문구' 경보 — 추측해서 결과를 내지 않는다
PENDING_RESULTS = {"", "미추첨", "추첨전", "추첨대기"}
DRAWN_RESULTS = {"낙첨", "당첨"}
WRONG_PASSWORD_MARK = "아이디 또는 비밀번호"   # dhapi 4.2.4 로그인 실패 문구 — 비번 오류 (5번 틀리면 계정 잠김)
UNKNOWN_ERROR_MARK = "알 수 없는 오류"         # dhapi 4.2.4 로또 결제 — 응답을 못 읽었을 때 문구 (결제가 됐는지 모른다)
LOGIN_FAIL_LIMIT = 2              # 비번 오류가 아닌 로그인 실패도 연속 2번이면 로그인을 멈춘다 (반복 호출 금지 — 약관 제12조)
LEDGER_PAGE_SIZE = 100            # dhapi 4.2.4 show_buy_list 는 구매내역 첫 쪽(100줄)만 읽는다 — 넘으면 다 못 읽은 것
# 상태 폴더: 시도 기록(state.json) · 설정(config.json) · 구매 DB(lotto.sqlite). Actions 에선 state 브랜치를 여기 꺼내 둔다
STATE_DIR = settings.state_dir()
STATE = STATE_DIR / "state.json"
LOCK = STATE_DIR / "lotto.lock"   # buy·check 공용 프로세스 잠금 (로컬만 — 내용 없음, 잡고 있는지만 본다)
SECTOR = "복권"
DRY = False
NO_JITTER = False


# ---------- 공통 ----------
def now_kst() -> datetime:
    return datetime.now(KST)


def emit(**kw) -> None:
    """마지막 줄 판정 JSON. 값이 None 인 칸은 뺀다."""
    print(json.dumps({k: v for k, v in kw.items() if v is not None}, ensure_ascii=False))


def draw_date_of(round_no: int) -> date:
    return FIRST_ROUND_DRAW + timedelta(days=7 * (round_no - 1))


def sales_round(today: date) -> int:
    """로또 — 지금 판매 중인 회차. dhapi 4.2.4 _get_round 와 같은 공식 (로그인 없이).
    이번 주 토요일(오늘이 토면 오늘, 일요일이면 다음 토)까지 1회(2002-12-07)부터 몇 주인지 + 1."""
    this_saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    return 1 + (this_saturday - FIRST_ROUND_DRAW).days // 7


def pension_week_round(today: date) -> int:
    """연금 — 이번 주(월요일 시작) 목요일에 추첨하는 회차. 월-목에만 쓴다."""
    thursday = today + timedelta(days=PENSION_LAST_BUY_WEEKDAY - today.weekday())
    return pension720.round_of_draw(thursday)


def latest_drawn_round(now: datetime) -> int:
    """로또 — 결과를 볼 수 있는 가장 최근 회차. 토 21:00 전이면 지난주 회차."""
    today = now.date()
    last_saturday = today - timedelta(days=(today.weekday() - 5) % 7)
    # < : 21:00 정각부터 이번 토 회차를 본다 (20:35 추첨 + 결과 반영 여유)
    if last_saturday == today and now.time() < RESULT_READY:
        last_saturday -= timedelta(days=7)
    return 1 + (last_saturday - FIRST_ROUND_DRAW).days // 7


def latest_drawn_pension(now: datetime) -> int:
    """연금 — 결과를 볼 수 있는 가장 최근 회차. 목 20:00 전이면 지난주 회차."""
    today = now.date()
    last_thursday = today - timedelta(days=(today.weekday() - PENSION_LAST_BUY_WEEKDAY) % 7)
    # < : 20:00 정각부터 이번 목 회차를 본다 (19:05 추첨 + 반영 여유)
    if last_thursday == today and now.time() < PENSION_RESULT_READY:
        last_thursday -= timedelta(days=7)
    return pension720.round_of_draw(last_thursday)


LATEST_DRAWN = {LOTTO: latest_drawn_round, PENSION: latest_drawn_pension}


def jitter() -> float:
    """예약 실행(LOTTO_JITTER=1 — 워크플로 schedule)이면 로그인 전 0-30분 랜덤 대기. 손 실행·dry-run·--no-jitter 는 바로. 잔 초를 돌려준다."""
    if DRY or NO_JITTER or os.environ.get("LOTTO_JITTER") != "1":
        return 0.0
    seconds = random.uniform(0, MAX_JITTER_MINUTES * 60)
    print(f"[lotto] 로그인 전 {seconds / 60:.1f}분 대기")
    time_mod.sleep(seconds)
    return seconds


def normalize_date(text) -> str | None:
    """'2026-11-21' · '20261121' · '2026.11.21' → '2026-11-21'. 못 읽으면 None."""
    digits = re.sub(r"\D", "", str(text or ""))
    if len(digits) < 8:
        return None
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def result_state(result) -> str:
    """로또 결과 문구 → 'pending'(추첨 전) · 'drawn'(낙첨·당첨) · 'unknown'(낯선 문구 — 결과를 추측하지 않는다)."""
    text = "".join(str(result or "").split())
    if text in PENDING_RESULTS:
        return "pending"
    return "drawn" if text in DRAWN_RESULTS else "unknown"


# ---------- 어댑터 (상태 저장소 · 계정 · 알림) — LOTTO_BACKEND 하나로 고른다 ----------
# local(기본) = 내 PC 에서 직접 · actions = GitHub Actions (state 브랜치). 결제·상한·시도 기록 순서는 두 모드가 같은 코드를 탄다.
BACKEND_ENV = "LOTTO_BACKEND"
Backend = namedtuple("Backend", "name state creds notifier")
_lock_handle = None
_BACKEND: Backend | None = None


class LocalFileStore:
    """로컬 실행: 상태 폴더의 state.json + 운영체제 파일 잠금. 경로는 모듈 전역 STATE·LOCK 을 부를 때마다 읽는다 (테스트가 바꾼다)."""
    name = "local"

    def acquire_lock(self) -> bool:
        """lotto.py 는 한 번에 하나만 돈다. 이미 다른 실행이 잡고 있으면 False (기다리지 않는다 — 부르는 쪽이 조용히 끝낸다).
        운영체제 파일 잠금이라 프로세스가 죽으면 저절로 풀린다 (잠금 파일이 남아 있어도 됨)."""
        global _lock_handle
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        handle = open(LOCK, "a+b")
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # noqa: PLC0415
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        _lock_handle = handle   # 프로세스가 끝날 때까지 쥐고 있는다
        return True

    def load(self) -> dict:
        """state.json. 파일이 없으면 빈 상태(첫 실행). 있는데 못 읽거나 모양이 다르면 StateError — 시도 기록을 모른 채 사지 않는다."""
        if not STATE.exists():
            return {}
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise StateError(f"state.json 을 못 읽음 ({type(e).__name__}) — 고칠 때까지 사지 않아요") from e
        if not isinstance(state, dict):
            raise StateError("state.json 모양이 다름 — 고칠 때까지 사지 않아요")
        return state

    def save(self, state: dict) -> None:
        """임시 파일에 다 쓴 뒤 바꿔치기 — 쓰다 죽어도 state.json 이 반쪽으로 남지 않는다."""
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_name(STATE.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, STATE)


class GitBranchStore(LocalFileStore):
    """GitHub Actions: 상태 폴더 = 같은 레포 `state` 브랜치를 꺼내 둔 곳. 읽기 전에 그 브랜치인지 확인하고,
    쓸 때마다 커밋·푸시한다 (store.publish). 푸시까지 성공해야 기록된 것 — 실패하면 StateError → 결제 직전 기록이면 결제까지 못 간다.
    잠금은 없다 — 워크플로 `concurrency:` 그룹(cancel-in-progress: false)이 한 번에 하나만 돌게 한다."""
    name = "git-branch"

    def acquire_lock(self) -> bool:
        return True

    def load(self) -> dict:
        store.verify_branch(STATE.parent, StateError)
        return super().load()

    def save(self, state: dict) -> None:
        super().save(state)
        store.publish(STATE.parent, f"state {stamp()}", StateError)

    def persist(self, message: str) -> None:
        """실행 끝에 DB·설정까지 같이 기록 (바뀐 게 없으면 아무것도 안 함)."""
        store.publish(STATE.parent, message, StateError)


class EnvProvider:
    """계정 = LOTTO_SECRETS 의 DH_ID·DH_PW (또는 같은 이름 환경변수). 둘 중 하나라도 비면 None (로그인 안 함 → 경보)."""
    name = "env"

    def read(self) -> User | None:
        try:
            user_id = (settings.secret("DH_ID") or "").strip()
            password = settings.secret("DH_PW") or ""          # 비번은 공백도 그대로 (자르지 않는다)
        except settings.SettingsError as e:
            print(f"[lotto] {e}")
            return None
        if not user_id or not password:
            return None
        return User(user_id, password)

    def missing_hint(self) -> str:
        return ("동행복권 아이디·비밀번호가 없어요 (돈 안 나감). 레포 Settings → Secrets and variables → Actions 의 "
                "LOTTO_SECRETS 에 DH_ID=… · DH_PW=… 두 줄이 있는지 확인해 주세요.")


class ChannelNotifier:
    """알림 = channel.py (텔레그램). channel 함수는 부를 때마다 모듈에서 찾는다 (테스트가 바꿔 끼운다). dry 는 channel.DRY_RUN 이 맡는다."""
    name = "telegram"
    dry = False

    def send(self, *args, **kwargs) -> bool:
        return channel.send(*args, **kwargs)

    def notify(self, *args, **kwargs) -> bool:
        return channel.notify(*args, **kwargs)

    def write_brief(self, *args, **kwargs) -> None:
        return channel.write_brief(*args, **kwargs)


def make_backend(name: str | None) -> Backend:
    """local(기본·빈 값) | actions. 모르는 값이면 StateError — 기록을 못 남기는 채로 사지 않는다."""
    name = (name or "local").strip().lower()
    if name == "local":
        return Backend("local", LocalFileStore(), EnvProvider(), ChannelNotifier())
    if name == "actions":
        return Backend("actions", GitBranchStore(), EnvProvider(), ChannelNotifier())
    raise StateError(f"{BACKEND_ENV}={name!r} 모름 (local | actions) — 사지 않아요")


def backend() -> Backend:
    """이번 실행의 어댑터 묶음 (처음 부를 때 env 로 한 번 고른다)."""
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = make_backend(os.environ.get(BACKEND_ENV))
    return _BACKEND


def notifier():
    return backend().notifier


# ---------- 프로세스 잠금 (buy·check 공용) ----------
def acquire_lock() -> bool:
    """한 번에 하나만 돈다 — local 은 운영체제 파일 잠금, actions 는 잠금 없음(워크플로 concurrency 그룹이 대신한다)."""
    return backend().state.acquire_lock()


# ---------- 상태 (결제·예약 시도 · 멈춘 회차 · 로그인 잠금 · 경보 하루 1번 · 기록 없는 회차 확인) ----------
def load_state() -> dict:
    """상태 전체. 없으면 빈 상태(첫 실행). 못 읽거나 모양이 다르면 StateError — 시도 기록을 모른 채 사지 않는다."""
    return backend().state.load()


def save_state(state: dict) -> None:
    """상태 전체를 쓴다. 실패하면 예외 — 결제 직전 기록(mark_*_attempt)이면 결제까지 못 간다."""
    backend().state.save(state)


def week_key(day: date) -> str:
    """그 날이 든 주의 월요일 날짜 — '주 1번' 기록용."""
    return (day - timedelta(days=day.weekday())).isoformat()


def alert(key: str, reason: str, detail: str) -> None:
    """실패·예치금 부족 경보. 같은 key 는 KST 하루 1번만 (dry-run 은 출력만, 기록 안 함).
    '보냈다' 기록은 실제로 나갔을 때만 — 발송이 실패하면 다음 실행이 다시 보낸다. state 를 못 읽으면 중복 방지 없이 보낸다."""
    today = now_kst().date().isoformat()
    try:
        state = load_state()
    except StateError:
        state = None
    sent = state.setdefault("alerts", {}) if state is not None else {}
    if sent.get(key) == today and not DRY:
        print(f"[lotto] 오늘 이미 보낸 경보: {key}")
        return
    delivered = notifier().notify("lotto", [(SECTOR, "복권 task", reason, detail)])
    if DRY or state is None or not delivered:
        return
    sent[key] = today
    try:
        save_state(state)
    except StateError as e:   # '보냄' 기록만 못 남김 — 다음 실행이 같은 경보를 한 번 더 보낼 뿐, 구매 판단엔 영향 없음
        print(f"[lotto] 경보 기록 실패 (경보는 나감): {e}")


def stamp() -> str:
    return now_kst().isoformat(timespec="seconds")


def log_spend(state: dict, amount: int, what: str) -> None:
    """결제 요청을 보낼 돈을 적는다 (시도 기록과 같은 저장에 실린다). 예치금이 줄어든 걸 '내가 쓴 돈'으로 설명하는 근거.
    실패한 결제도 남는다 — 설명되는 돈이 실제보다 많아질 뿐이라 거짓 경보는 안 난다."""
    state.setdefault("spend_log", []).append({"ts": stamp(), "won": int(amount), "what": what})


def pension_attempted(round_no: int) -> bool:
    return str(round_no) in load_state().get("pension_attempts", {})


def mark_pension_attempt(round_no: int) -> None:
    """연금 결제 직전에 부른다. 이미 적힌 회차거나 저장이 실패하면 예외 → 결제 안 함 (같은 회차를 또 결제하는 길을 막는다)."""
    state = load_state()
    attempts = state.setdefault("pension_attempts", {})
    if str(round_no) in attempts:
        raise StateError(f"연금 {round_no}회 결제 시도가 이미 적혀 있음 — 다시 결제 안 함")
    attempts[str(round_no)] = stamp()
    log_spend(state, SET_COST, f"연금 {round_no}회")
    save_state(state)
    if not pension_attempted(round_no):
        raise StateError("연금 결제 시도 기록이 안 써짐")


def lotto_attempted_games(round_no: int) -> int:
    """이 회차에 결제 요청을 보낸 로또 게임 수 합 (실패·결과 불명 포함)."""
    return to_int((load_state().get("lotto_attempts", {}).get(str(round_no)) or {}).get("games"))


def mark_lotto_attempt(round_no: int, games: int) -> None:
    """로또 결제 직전에 부른다. 이 회차 시도 합이 회차 상한을 넘거나 저장이 실패하면 예외 → 결제 안 함."""
    state = load_state()
    attempts = state.setdefault("lotto_attempts", {})
    total = to_int((attempts.get(str(round_no)) or {}).get("games")) + games
    # > : 시도 합이 상한(3게임)과 딱 같은 건 허용
    if games <= 0 or total > MAX_GAMES_PER_ROUND:
        raise StateError(f"로또 {round_no}회 결제 시도 합 {total}게임 — 회차 상한 {MAX_GAMES_PER_ROUND}게임 초과")
    attempts[str(round_no)] = {"games": total, "ts": stamp()}
    log_spend(state, games * GAME_PRICE, f"로또 {round_no}회")
    save_state(state)
    if lotto_attempted_games(round_no) != total:
        raise StateError("로또 결제 시도 기록이 안 써짐")


# 예약 시도 결과: pending(요청 직전) · ok(예약내역에서 확인) · unknown(결과 불명 — 있는 걸로 침) · failed(서버가 거절했고 예약내역에도 없음)
RESERVE_COUNTS = {"pending", "ok", "unknown"}   # 이 결과의 회차는 '예약 있음'으로 센다


def reserve_attempts() -> list[dict]:
    return list(load_state().get("pension_reserve_attempts", []))


def reserve_attempt_rounds(only_counted: bool) -> set[int]:
    """예약 요청을 보낸 회차들. only_counted=True 면 '예약 있음'으로 세는 것만 (failed 제외)."""
    return {int(r) for a in reserve_attempts() if not only_counted or a.get("result") in RESERVE_COUNTS for r in a.get("rounds", [])}


def mark_reserve_attempt(rounds: list[int], deposit_before: int | None = None) -> None:
    """예약 결제 직전에 부른다. 한 회차라도 이미 시도(예약·직접 구매)가 적혀 있거나 저장이 실패하면 예외 → 결제 안 함.
    deposit_before = 요청 직전에 읽은 구매가능 예치금 — 나중에 '돈이 나갔나'를 가리는 기준."""
    state = load_state()
    taken = {int(r) for a in state.get("pension_reserve_attempts", []) for r in a.get("rounds", [])}
    taken |= {int(r) for r in state.get("pension_attempts", {})}
    overlap = sorted(taken & set(rounds))
    if not rounds or overlap:
        raise StateError(f"연금 예약 시도가 이미 적혀 있는 회차 {overlap} — 다시 예약 안 함")
    state.setdefault("pension_reserve_attempts", []).append(
        {"rounds": list(rounds), "ts": stamp(), "result": "pending", "deposit_before": deposit_before})
    log_spend(state, len(rounds) * SET_COST, f"연금 예약 {rounds[0]}-{rounds[-1]}회")
    save_state(state)
    if not set(rounds) <= reserve_attempt_rounds(only_counted=True):
        raise StateError("연금 예약 시도 기록이 안 써짐")


def set_reserve_result(rounds: list[int], result: str, **extra) -> None:
    """예약 시도 줄에 판정을 적는다. extra = 같이 남길 관측값 (deposit_after 등)."""
    state = load_state()
    for a in state.get("pension_reserve_attempts", []):
        if a.get("rounds") == list(rounds):
            a["result"] = result
            a.update(extra)
    save_state(state)


def unresolved_reserve_attempts() -> list[dict]:
    """판정이 안 끝난 예약 시도 — pending(요청 중 프로세스가 끊김) · unknown(결과 불명). 다음 로그인 때 예약내역으로 가린다."""
    return [a for a in reserve_attempts() if a.get("result") in ("pending", "unknown")]


def reserve_enabled() -> bool:
    """예약을 넣어도 되나 — 사용자가 '① 설정' 에서 연금을 '5회차 예약' 으로 고른 경우만 (config pension_mode = reserve)."""
    return PENSION_RESERVE_ENABLED


def reserved_seen() -> set[int]:
    """마지막으로 사이트 예약내역을 읽었을 때 살아 있던(취소 아님) 예약 회차 — 로그인 없이 '예약 있음'을 아는 용도."""
    return {int(r) for r in load_state().get("pension_reserved_seen", [])}


def remember_reserved_seen(rounds: set[int]) -> None:
    state = load_state()
    state["pension_reserved_seen"] = sorted(rounds)
    save_state(state)


def reserved_known() -> set[int]:
    """state 기준 '예약 있음' 회차 = 예약 시도(failed 제외) + 사이트에서 본 예약."""
    return reserve_attempt_rounds(only_counted=True) | reserved_seen()


def halted(game: str, round_no: int) -> bool:
    return f"{game}:{round_no}" in load_state().get("halted", {})


def halt(game: str, round_no: int, why: str) -> None:
    """이 게임·회차는 손으로 풀 때까지 더 안 산다 (산 게 구매내역에 안 보일 때 등)."""
    state = load_state()
    state.setdefault("halted", {})[f"{game}:{round_no}"] = f"{stamp()} {why}"
    save_state(state)


def login_stop_reason(now: datetime, buying: bool = True) -> str | None:
    """로그인하면 안 되는 이유 (없으면 None). ① login_blocked 가 적혀 있음 (비번 오류 1번 · 그 밖의 로그인 실패 연속 2번) — 손으로 지울 때까지
    ② (buy 만) 오늘 예치금 부족 경보를 이미 보냄 — 같은 날 또 로그인해 봐야 못 산다. check 는 결과를 봐야 해서 ② 를 안 따진다."""
    state = load_state()
    blocked = state.get("login_blocked")
    if blocked:
        alert("login-blocked", "로그인 멈춤 — 직접 확인 필요",
              f"동행복권 로그인을 멈춘 상태예요 ({blocked}). 비번 오류는 5번 틀리면 계정이 잠기고, 연속 실패는 같은 호출을 되풀이하게 돼서 멈췄어요. "
              "사이트에 직접 로그인해 보고, 비번이 바뀌었으면 LOTTO_SECRETS 의 DH_PW 를 고친 뒤 Actions 의 '① 설정' 을 다시 실행하면 로그인 멈춤이 풀려요 (로컬은 state.json 의 login_blocked 줄을 지우기).")
        return "로그인 멈춤 기록(login_blocked) — 로그인 안 함"
    if buying and state.get("deposit_short_day") == now.date().isoformat():
        return "오늘 예치금 부족 경보를 이미 보냄 — 내일 다시 (충전했으면 state.json 의 deposit_short_day 를 지우면 바로)"
    return None


def mark_deposit_short(now: datetime) -> None:
    state = load_state()
    state["deposit_short_day"] = now.date().isoformat()
    save_state(state)


# ---------- dhapi 연결 ----------
class Capture:
    """dhapi 가 화면에 찍던 결과를 받아 두는 그릇 (endpoint 자리에 넣는다)."""

    def __init__(self):
        self.balance: dict = {}
        self.orders: list[dict] = []
        self.bought_slots: list = []

    def print_result_of_buy_lotto645(self, slots):
        self.bought_slots = slots

    def print_result_of_show_balance(self, **kw):
        self.balance = kw

    def print_result_of_show_buy_list(self, found_data, output_format, start, end):
        self.orders = found_data

    def print_result_of_assign_virtual_account(self, *a, **kw):
        pass


def count_games(numbers: str | None) -> int:
    """번호 줄 '[A] 자동: 1 2 3 4 5 6' 개수 = 게임 수. 번호를 못 읽었으면 0 → 부르는 쪽이 '못 센 줄'로 본다
    (구입매수 prchsQty 는 게임 수인지 장 수인지 확인된 적이 없어서 세는 데 안 쓴다)."""
    return len([l for l in (numbers or "").splitlines() if l.startswith("[")])


def is_lotto_item(item: dict) -> bool:
    """로또 6/45 줄인가 — 상품 코드 또는 이름(띄어쓰기가 바뀌어도). 이름이 조금 바뀌었다고 줄을 버리면 산 걸 못 세고 또 산다."""
    return item.get("ltGdsCd") == LOTTO_CODE or LOTTO_NAME in "".join(str(item.get("ltGdsNm") or "").split())


def to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_order(item: dict, numbers: str | None) -> dict | None:
    """로또 구매내역 한 줄 → 주문 dict. 로또6/45 가 아니거나 회차·주문번호가 없으면 None.
    번호 조회가 실패한 줄은 games 0 으로 돌려준다 (부르는 쪽이 '못 센 줄'로 바꾼다)."""
    if not is_lotto_item(item) or not item.get("ntslOrdrNo"):
        return None
    round_digits = re.sub(r"\D", "", str(item.get("ltEpsdView") or ""))
    if not round_digits:
        return None
    round_no = int(round_digits)
    good_numbers = numbers if numbers and numbers.startswith("[") else None
    return {
        "order_no": str(item["ntslOrdrNo"]),
        "game": LOTTO,
        "round": round_no,
        "bought_on": normalize_date(item.get("eltOrdrDt")),
        "games": count_games(good_numbers),
        "numbers": good_numbers,
        "result": (item.get("ltWnResult") or "").strip(),
        "prize": int(item.get("ltWnAmt") or 0),
        "draw_date": normalize_date(item.get("epsdRflDt")) or draw_date_of(round_no).isoformat(),
    }


def parse_pension_orders(items: list[dict]) -> list[dict]:
    """연금 구매내역 줄들 → 주문 dict 목록. 결과·당첨금은 비워 둔다 — check 가 공개 당첨번호로 직접 판정한다.
    사이트는 세트 하나를 '한 장 = 한 줄'로 준다 (2026-11-16 실측: 같은 ntslOrdrNo 로 gmInfo '1:845545'…'5:845545' 5줄,
    줄마다 prchsQty 1). 그래서 주문번호로 묶고, 같은 (주문번호, gmInfo) 줄이 두 번 와도 한 번만 센다.
    회차나 번호(gmInfo)를 못 읽은 줄은 추측해서 세지 않고 '못 센 줄'(unknown_order)로 돌려준다 → buy 는 안 산다."""
    by_key: dict[str, dict] = {}
    unknown: list[dict] = []
    for item in items:
        if not pension720.is_pension_item(item):
            continue
        round_digits = re.sub(r"\D", "", str(item.get("ltEpsd") or item.get("ltEpsdView") or ""))
        tickets = pension720.tickets_of(str(item.get("gmInfo") or ""))
        # 회차 없음 · 번호 0장 = 못 읽은 줄 (다른 회차 줄이 멀쩡해도 이 줄이 이번 회차일 수 있다)
        if not round_digits or int(round_digits) <= 0 or not tickets:
            unknown.append(unknown_order(item))
            continue
        round_no = int(round_digits)
        # 주문번호가 없으면 줄마다 따로 (회차·날짜·번호로 대신)
        key = str(item.get("ntslOrdrNo") or f"{round_no}-{item.get('eltOrdrDt')}-{item.get('gmInfo')}")
        order = by_key.setdefault(key, {"round": round_no, "item": item, "tickets": set()})
        if order["round"] != round_no:
            unknown.append(unknown_order(item))   # 같은 주문번호인데 회차가 다름 — 못 믿는다
            continue
        order["tickets"].update(tickets)
    orders = []
    for key, o in by_key.items():
        item, round_no, tickets = o["item"], o["round"], sorted(o["tickets"])
        orders.append({
            "order_no": f"{pension720.PENSION_CODE}-{key}",
            "game": PENSION,
            "round": round_no,
            "bought_on": normalize_date(item.get("eltOrdrDt")),
            "games": len(tickets),
            "numbers": pension720.format_tickets(tickets) or None,
            "result": "",
            "prize": 0,
            "draw_date": normalize_date(item.get("epsdRflDt")) or pension720.draw_date_of(round_no).isoformat(),
        })
    return orders + unknown


def unknown_order(item: dict) -> dict:
    """로또·연금 줄인데 회차·주문번호·장 수를 못 읽음. DB 엔 안 넣고, buy 는 이게 있으면 안 산다."""
    return {"unknown": True, "game": "unknown", "round": 0, "games": 0, "order_no": str(item.get("ntslOrdrNo") or "?"),
            "name": str(item.get("ltGdsNm") or "")}


def row_label(order: dict) -> str:
    """경보·출력에 넣을 줄 이름 — 상품 이름, 없으면 주문번호 끝 4자리만 (주문번호 전체는 알림·로그에 안 싣는다)."""
    return order.get("name") or f"주문번호 끝 {str(order.get('order_no') or '?')[-4:]}"


def other_order(item: dict) -> dict:
    """로또·연금이 아닌 복권 줄 (스피또·전자복권 등). 이 task 는 안 사는 상품 — DB 엔 안 넣고 이상징후 감지만 본다."""
    name = pension720.short_text(item.get("ltGdsNm") or item.get("ltGdsCd") or "이름 없음", 30)
    bought_on = normalize_date(item.get("eltOrdrDt"))
    # '이미 알린 줄' 열쇠 — 주문번호가 없는 줄끼리 한 열쇠('other:?')로 뭉치지 않게 날짜·상품·번호까지 넣는다
    key = ":".join(["other", str(item.get("ntslOrdrNo") or ""), bought_on or "", name, pension720.short_text(item.get("gmInfo"), 80)])
    return {"other": True, "game": "other", "round": 0, "games": 0, "order_no": str(item.get("ntslOrdrNo") or "?"),
            "name": name, "bought_on": bought_on, "key": key}


class RawLotteryClient(LotteryClient):
    """구매내역을 표 대신 주문 dict 목록으로 돌려받는다 (dhapi 4.2.4 의 _parse_buy_list_json 덮어쓰기). 로또 + 연금."""

    def _parse_buy_list_json(self, response_data):
        data = (response_data or {}).get("data") or {}
        items = data.get("list") or []
        total = to_int(data.get("total"))
        # dhapi 는 첫 쪽만 읽는다. > : 전체 줄 수가 한 쪽(100줄)을 넘으면 뒤쪽을 못 본 것.
        # 전체 줄 수를 안 주는데 한 쪽이 꽉 찼으면(>= 100) 더 있는지 알 수 없다 → 그것도 못 읽은 걸로
        truncated = total > LEDGER_PAGE_SIZE or (total <= 0 and len(items) >= LEDGER_PAGE_SIZE)
        orders = self._orders_of(items)
        if truncated:
            orders.append(unknown_order({"ltGdsNm": f"구매내역이 한 쪽({LEDGER_PAGE_SIZE}줄)을 넘어 다 못 읽음"}))
        return orders

    def _orders_of(self, items: list[dict]) -> list[dict]:
        # 연금 줄인데 회차·번호를 못 읽은 줄은 '못 센 줄'로 섞여 나온다 → buy 는 안 산다 (fail-closed)
        orders = parse_pension_orders([i for i in items if pension720.is_pension_item(i)])
        for item in items:
            if pension720.is_pension_item(item):
                continue
            if not is_lotto_item(item):
                orders.append(other_order(item))   # 이 task 가 안 사는 복권 — 세지는 않고 이상징후로만 본다
                continue
            order = self._lotto_order(item)
            # > 0 : 번호 조회 실패(게임 수 0)도 못 센 줄
            orders.append(order if order and order["games"] > 0 else unknown_order(item))
        return orders

    def _lotto_order(self, item: dict) -> dict | None:
        if not is_lotto_item(item):
            return None
        numbers = None
        if item.get("gmInfo") and item.get("ntslOrdrNo"):
            numbers = self._get_lotto645_ticket_detail(item["ntslOrdrNo"], item["gmInfo"], item.get("eltOrdrDt", ""))
            time_mod.sleep(self._DETAIL_REQUEST_DELAY)
        return parse_order(item, numbers)


def read_user() -> User | None:
    """계정을 읽는다 (LOTTO_SECRETS 의 DH_ID·DH_PW). dhapi CredentialsProvider 는 없으면 input() 으로 묻기 때문에 안 쓴다."""
    return backend().creds.read()


def login() -> tuple[RawLotteryClient, Capture] | None:
    """로그인. 실패하면 경보 보내고 None."""
    user = read_user()
    if user is None:
        alert("credentials", "계정 파일 없음·못 읽음", backend().creds.missing_hint())
        return None
    capture = Capture()
    try:
        client = RawLotteryClient(user, capture)
    except Exception as e:  # noqa: BLE001  (점검중·비번 오류·네트워크)
        if WRONG_PASSWORD_MARK in str(e):
            # 비번 오류는 다시 시도하면 계정이 잠긴다 → 적어 두고 손으로 지울 때까지 로그인 안 함 (dry-run 이어도 적는다 — 틀린 횟수는 똑같이 쌓인다)
            state = load_state()
            state["login_blocked"] = f"{stamp()} 비번 오류"
            save_state(state)
            if state.get("last_balance"):
                # 전엔 이 계정 파일로 로그인이 됐다 → 누가 비번을 바꿨을 수 있다
                anomaly_alert("password", "되던 로그인이 비번 오류로 바뀜",
                              [f"마지막으로 로그인된 때 {state['last_balance'].get('ts')} · 지금 {stamp()} 아이디·비번 오류",
                               "직접 비번을 바꾼 거면 LOTTO_SECRETS 의 DH_PW 를 고치고 Actions 의 '① 설정' 을 다시 실행하면 로그인 멈춤이 풀려요 (로컬은 state.json 의 login_blocked 줄을 지우기)."])
            alert("login-blocked", "로그인 실패 — 비번 확인 필요",
                  "동행복권이 아이디·비번이 틀렸대요. 계정 잠김을 피하려고 로그인을 멈췄어요. LOTTO_SECRETS 의 DH_ID·DH_PW 를 확인한 뒤 "
                  "Actions 의 '① 설정' 을 다시 실행하면 로그인 멈춤이 풀려요 (로컬은 state.json 의 login_blocked 줄을 지우기).")
            return None
        # 비번 오류가 아닌 실패(점검·네트워크·사이트 개편)도 연속으로 나면 멈춘다 — 안 되는 로그인을 날마다 되풀이하지 않는다 (dry-run 도 센다)
        state = load_state()
        streak = to_int(state.get("login_fail_streak")) + 1
        state["login_fail_streak"] = streak
        # >= : 연속 2번째 실패부터 멈춘다 (1번은 점검·순간 장애일 수 있어 다음 실행이 한 번 더 본다)
        blocked = streak >= LOGIN_FAIL_LIMIT
        if blocked:
            state["login_blocked"] = f"{stamp()} 로그인 연속 {streak}번 실패"
        save_state(state)
        # 멈출 때는 열쇠를 달리한다 — 같은 날 첫 실패 경보가 이미 나갔어도 '멈췄다'는 알림은 간다
        alert("login-streak" if blocked else "login", "로그인 실패" + (" — 연속 실패라 로그인 멈춤" if blocked else ""),
              f"동행복권 로그인 실패: {pension720.short_text(str(e), 120)}"
              + (f" · 연속 {streak}번째라 로그인을 멈췄어요. 사이트가 되는지 직접 본 뒤 Actions 의 '① 설정' 을 다시 실행하면 로그인 멈춤이 풀려요 (로컬은 state.json 의 login_blocked 줄을 지우기)." if blocked else ""))
        return None
    state = load_state()
    if state.get("login_fail_streak"):   # 로그인됐다 → 연속 실패 횟수를 지운다 (적힌 게 있을 때만 쓴다)
        state["login_fail_streak"] = 0
        save_state(state)
    return client, capture


# ---------- 이상징후 감지 (계정이 털렸나 — 이미 한 로그인 안에서만 본다, 로그인을 늘리지 않는다) ----------
ANOMALY_HEAD = "🚨 동행복권 이상 감지 — {what} · 비밀번호를 바꾸고 예치금 충전 계좌 연결을 확인하세요"
# 예치금이 줄었는데 그 사이 내 예약 회차의 판매가 열렸다 — 예약이 실행되며 빠진 돈일 수 있다 (털림 의심과 말을 나눈다)
ANOMALY_SOFT_HEAD = "🚨 동행복권 예치금 확인 — {what} · 내 구매일 수도 있어요 (예약 실행 등) — 구매내역에 없으면 비밀번호를 바꾸세요"


def anomaly_alert(kind: str, what: str, details: list[str], head: str = ANOMALY_HEAD) -> bool:
    """이상징후 한 통 (알림음 켬). 같은 종류는 KST 하루 1번. 이번에 실제로 나갔으면 True
    (dry-run·오늘 이미 보냄·발송 실패는 False — 부른 쪽은 True 일 때만 '알렸다'로 적는다)."""
    text = "\n".join([head.format(what=what), *details, "직접 한 거면 무시해도 돼요."])
    today = now_kst().date().isoformat()
    state = load_state()
    sent = state.setdefault("alerts", {})
    if sent.get(f"anomaly-{kind}") == today and not DRY:
        print(f"[lotto] 오늘 이미 보낸 이상징후 알림: {kind}")
        return False
    delivered = notifier().send(text, silent=False)
    if DRY or not delivered:
        return False
    notifier().write_brief("lotto", [(SECTOR, "이상징후", what, text)])
    state = load_state()
    state.setdefault("alerts", {})[f"anomaly-{kind}"] = today
    save_state(state)
    return True


def explained_count(game: str, round_no: int) -> int:
    """이 회차에 '내가 결제 요청을 보낸' 양 (state 시도 기록). 사이트 구매내역이 이보다 많으면 내가 안 산 것.
    예약은 예약한 때 내 것으로 센다 — 그 회차 판매가 열려 구매내역에 5장이 떠도 이상이 아니다."""
    if game == LOTTO:
        return lotto_attempted_games(round_no)
    mine = pension_attempted(round_no) or round_no in reserve_attempt_rounds(only_counted=True)
    return PENSION_MAX_TICKETS if mine else 0


def acked_orders(seen: dict, game: str, round_no: int) -> tuple[set[str], int]:
    """이 회차에서 이미 알린 것 → (알린 주문번호들, 그때 '내 기록에 없던 양' 중 가장 큰 값).
    열쇠 = '게임:회차:주문번호+주문번호…' (수량이 아니라 주문번호 묶음). 값은 {'ts', 'extra'} — 옛 값(글)이면 extra 0."""
    prefix = f"{game}:{round_no}:"
    orders: set[str] = set()
    extra = 0
    for key, value in seen.items():
        if not key.startswith(prefix):
            continue
        orders |= set(key[len(prefix):].split("+"))
        extra = max(extra, to_int(value.get("extra")) if isinstance(value, dict) else 0)
    return orders, extra


def order_anomalies(orders: list[dict]) -> list[dict]:
    """구매내역에서 내가 안 만든 것 — [{'key', 'kind': 'order'|'other', 'what', 'detail', 'extra'}]. 이미 알린 것(anomaly_seen)은 뺀다.
    order: 로또·연금 회차의 사이트 수량 > 내 결제 시도 (손으로 산 것도 여기 걸린다 — 한 번 알리고 끝)
    other: 로또·연금이 아닌 복권."""
    seen = load_state().get("anomaly_seen", {})
    found = []
    for o in orders:
        if o.get("other") and o["key"] not in seen and o["key"] not in {f["key"] for f in found}:
            found.append({"key": o["key"], "kind": "other", "what": f"다른 복권 구매 ({o['name']})", "extra": 0,
                          "detail": f"{o['name']} · 주문번호 끝 {o['order_no'][-4:]} · {o.get('bought_on') or '날짜 모름'}"})
    by_round: dict[tuple[str, int], list[dict]] = {}
    for o in orders:
        if not o.get("other") and not o.get("unknown"):
            by_round.setdefault((o["game"], o["round"]), []).append(o)
    for (game, round_no), rows in sorted(by_round.items()):
        count = sum(o["games"] for o in rows)
        extra = count - explained_count(game, round_no)
        # <= 0 : 사이트 수량이 내 시도 이하면 정상 (실패한 시도가 있으면 더 적을 수 있다)
        if extra <= 0:
            continue
        order_nos = sorted({str(o["order_no"]) for o in rows})
        told_orders, told_extra = acked_orders(seen, game, round_no)
        # 새 주문이 없으면(전부 이미 알린 주문) 정상. 새 주문이 있어도 '내 기록에 없는 양'이 알린 것보다 안 늘었으면 정상 —
        # 알린 뒤에 내가 산 것이다 (<= : 알린 양과 딱 같으면 새로 늘어난 게 없다)
        if set(order_nos) <= told_orders or extra <= told_extra:
            continue
        tails = ", ".join(sorted({str(o["order_no"])[-4:] for o in rows}))
        days = ", ".join(sorted({o.get("bought_on") or "날짜 모름" for o in rows}))
        found.append({"key": f"{game}:{round_no}:{'+'.join(order_nos)}", "kind": "order", "extra": extra,
                      "what": f"내가 안 산 {GAME_LABEL[game]} 주문",
                      "detail": f"{GAME_LABEL[game]} {round_no}회 {count}{GAME_UNIT[game]} 중 {extra}{GAME_UNIT[game]}({won(extra * GAME_PRICE)})은 "
                                f"내 결제 기록에 없어요 · 주문번호 끝 {tails} · 구입일 {days}"})
    return found


def expected_balance(state: dict) -> int | None:
    """지난 로그인 때 예치금 − 그 뒤 내가 결제 요청한 돈 = 지금 있어야 할 예치금. 기준값이 없으면(첫 실행) None."""
    last = state.get("last_balance")
    if not last:
        return None
    # >= : 기준 잔액을 읽은 바로 그 초에 적힌 결제도 그 뒤에 나간 돈이다 (기준 갱신 때 그보다 앞선 것만 지운다)
    spent = sum(e["won"] for e in state.get("spend_log", []) if e["ts"] >= last["ts"])
    return last["amount"] - spent


def reserve_opened_since(last_ts: str, today: date) -> int:
    """지난 기준 뒤로 판매가 열린 내 예약 회차 수 (판매 개시 = 앞 회차 추첨일). 그만큼은 예약이 실행되며 빠졌을 수 있는 돈이다."""
    since = date.fromisoformat(last_ts[:10])
    # <= 양쪽 : 기준을 읽은 날·오늘 열린 회차도 넣는다 (넓게 잡는 쪽 — 말만 부드러워질 뿐 알림은 그대로 간다)
    return len([r for r in reserved_known() if since <= pension720.draw_date_of(r) - timedelta(days=7) <= today])


def balance_anomaly(balance: int, state: dict) -> dict | None:
    """지난 로그인 때 예치금 → 지금 예치금이 '내가 쓴 돈'으로 설명되나. 설명 안 되게 줄었으면 {'what', 'detail', 'soft'}, 아니면 None.
    기준값이 없으면(첫 실행) None. 늘어난 건(충전·당첨금 입금·예약 취소 환불) 이상이 아니다 — detect_anomalies 가 적어 두고 다음 구매 알림에 한 줄.
    예약은 예약한 때 쓴 돈으로 적혀 있어서(spend_log) 예약 회차가 열려도 예치금이 그대로면 아무 일도 없다.
    soft: 줄어든 돈이 그 사이 열린 내 예약 회차 값 이하 — 예약이 실행되며 한 번 더 빠진 걸 수 있어 '내 구매일 수도'로 알린다. 그 밖은 '털림 의심'."""
    expected = expected_balance(state)
    # >= : 예상과 딱 같거나 많으면 줄어든 게 없다
    if expected is None or balance >= expected:
        return None
    last = state["last_balance"]
    gap = expected - balance
    opened = reserve_opened_since(last["ts"], now_kst().date())
    # <= : 줄어든 돈이 열린 예약 회차 값과 딱 같아도 예약 실행으로 본다
    soft = opened > 0 and gap <= opened * SET_COST
    what = f"예치금 {won(gap)} 줄었어" if soft else f"예치금 {won(gap)} 이 설명 안 됨 — 털림 의심"
    detail = (f"{last['ts']} 구매가능 {won(last['amount'])} → 지금 {won(balance)} · 그 사이 내가 결제 요청한 돈 {won(last['amount'] - expected)} · "
              f"설명 안 되는 감소 {won(gap)}")
    if soft:
        detail += f" · 그 사이 판매가 열린 내 예약 {opened}회차 (회차당 {won(SET_COST)})"
    return {"what": what, "detail": detail, "soft": soft}


def detect_anomalies(orders: list[dict], balance: int | None, now: datetime) -> bool:
    """로그인한 김에 이상징후를 본다. 하나라도 있으면 True (buy 는 그 실행에서 아무것도 안 산다).
    알림이 실제로 나간 것만 '알렸다'로 적는다 — 주문은 anomaly_seen, 잔액은 기준값 갱신. 그래서 못 알린 이상은 다음 실행이 다시 잡는다.
    한 번 알린 주문·잔액 변화는 다음 실행부터 정상으로 본다 — '알렸다' = 알림 발송이 성공했다는 것만 본다 (사람이 읽었는지는 안 본다)."""
    state = load_state()
    found = order_anomalies(orders)
    told: list[dict] = []
    for kind in ("order", "other"):
        items = [f for f in found if f["kind"] == kind]
        if items and anomaly_alert(kind, items[0]["what"] + (f" 외 {len(items) - 1}건" if len(items) > 1 else ""), [f["detail"] for f in items]):
            told += items
    odd_balance = balance_anomaly(balance, state) if balance is not None else None
    balance_told = bool(odd_balance) and anomaly_alert("balance", odd_balance["what"], [odd_balance["detail"]],
                                                       ANOMALY_SOFT_HEAD if odd_balance["soft"] else ANOMALY_HEAD)
    expected = expected_balance(state)
    # > : 예상보다 많을 때만 (충전·당첨금 입금·환불)
    grew = balance - expected if balance is not None and expected is not None and balance > expected else 0
    if grew:
        print(f"[lotto] 충전·입금 감지: 예치금이 예상보다 {won(grew)} 많음 (충전·당첨금 입금 — 다음 구매 알림에 한 줄)")
    if DRY:
        return bool(found or odd_balance)
    state = load_state()
    for f in told:
        state.setdefault("anomaly_seen", {})[f["key"]] = {"ts": stamp(), "extra": f["extra"]}
    if grew:
        state["deposit_up"] = to_int(state.get("deposit_up")) + grew
    # 잔액 기준값: 이상이 없거나, 이상을 실제로 알렸을 때만 새 값으로 (못 알렸으면 옛 기준을 두고 다음 실행이 다시 잡는다)
    if balance is not None and (not odd_balance or balance_told):
        ts = now.isoformat(timespec="seconds")
        state["last_balance"] = {"ts": ts, "amount": int(balance)}
        # < : 새 기준보다 앞선 지출 기록은 이미 이 잔액에 반영됐다
        state["spend_log"] = [e for e in state.get("spend_log", []) if e["ts"] >= ts]
    save_state(state)
    return bool(found or odd_balance)


def fetch_orders(client: RawLotteryClient, capture: Capture, days: int) -> list[dict]:
    """최근 days 일 주문 (로또 + 연금). 실패하면 예외를 그대로 올린다 (부르는 쪽이 판단)."""
    today = now_kst().date()
    start = (today - timedelta(days=days)).strftime("%Y%m%d")
    capture.orders = []
    client.show_buy_list("json", start, today.strftime("%Y%m%d"))
    return list(capture.orders)


def fetch_reservations(client: RawLotteryClient) -> list[dict]:
    """연금 예약구매 내역 (GET — 읽기). 실패하면 예외 (부르는 쪽은 연금을 사지도 예약하지도 않는다)."""
    today = now_kst().date()
    return pension720.fetch_reservations(client._session, today - timedelta(days=RESERVE_LOOKBACK_DAYS), today)


def tickets_in(orders: list[dict], game: str, round_no: int) -> int:
    return sum(o["games"] for o in orders if o["game"] == game and o["round"] == round_no)


def reserved_in(reservations: list[dict], round_no: int) -> int:
    """사이트 예약내역 기준 이 회차에 살아 있는(취소 아님) 예약 장 수."""
    return sum(r["qty"] for r in reservations if r["round"] == round_no and not r["cancelled"])


# ---------- DB ----------
# 결과 핑을 보낸 줄(notified_at 있음)은 결과·당첨금을 다시 안 덮는다 — 보낸 숫자와 장부 숫자가 같게
UPSERT_SQL = """
insert into lotto_order (order_no, game, round, bought_on, games, numbers, result, prize, draw_date, checked_at)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (order_no) do update set
    game = excluded.game, round = excluded.round, bought_on = excluded.bought_on, games = excluded.games,
    numbers = coalesce(excluded.numbers, lotto_order.numbers),
    result = case when lotto_order.notified_at is null then excluded.result else lotto_order.result end,
    -- prize_net(세후)은 check 가 판정할 때만 쓴다 — upsert 는 안 건드림
    prize = case when lotto_order.notified_at is null then excluded.prize else lotto_order.prize end,
    draw_date = excluded.draw_date, checked_at = excluded.checked_at
"""


def upsert_orders(orders: list[dict]) -> None:
    orders = [o for o in orders if not o.get("unknown") and not o.get("other")]
    if not orders:
        return
    stamp = now_kst().isoformat(timespec="seconds")
    with db.conn() as c:
        for o in orders:
            c.execute(UPSERT_SQL, (o["order_no"], o["game"], o["round"], o["bought_on"], o["games"], o["numbers"],
                                   o["result"], o["prize"], o["draw_date"], stamp))


def pnl(rows: list[dict]) -> int:
    """손익(세후) = 세후 당첨금 합 - 장(게임) 수 × 1,000원. prize_net 이 없는 옛 줄은 세전을 한 장으로 보고 세금을 뺀다."""
    net = sum(r["prize_net"] if r.get("prize_net") is not None else after_tax(r["prize"] or 0) for r in rows)
    return net - sum(r["games"] or 0 for r in rows) * GAME_PRICE


def db_tickets(game: str, round_no: int) -> int:
    with db.conn() as c:
        row = c.execute("select coalesce(sum(games), 0) as n from lotto_order where game = %s and round = %s",
                        (game, round_no)).fetchone()
    return int(row["n"] or 0)


def drawn_rows() -> list[dict]:
    """결과가 확정돼 핑까지 나간 줄 (손익 계산용). 이번에 판정한 줄은 pnl_lines 가 따로 얹는다."""
    with db.conn() as c:
        return c.execute("select * from lotto_order where notified_at is not null").fetchall()


def save_results(rows: list[dict]) -> None:
    with db.conn() as c:
        for r in rows:
            c.execute("update lotto_order set result = %s, prize = %s, prize_net = %s where order_no = %s",
                      (r["result"], r["prize"], r.get("prize_net"), r["order_no"]))


# ---------- buy: 판매 시간 ----------
def before_sales_start(now: datetime) -> str | None:
    """매일 00:00-06:00 판매 정지 — 두 게임 공통."""
    # < : 06:00 정각부터 판다 (05:59 는 막음)
    if now.time() < SALES_START:
        return "06:00 전 (매일 00:00-06:00 판매 정지)"
    return None


def buy_window_closed(now: datetime) -> str | None:
    """로또 — 판매 시간이 아니면 이유, 사도 되면 None."""
    if now.weekday() == 6:
        return "일요일 (06:00 전 판매 정지 · 새 회차는 월요일에)"
    # >= : 19:30 정각부터 막는다 (마감 20:00 보다 30분 여유)
    if now.weekday() == 5 and now.time() >= SAT_BUY_CUTOFF:
        return "토요일 19:30 이후 (판매 마감 20:00)"
    return before_sales_start(now)


def pension_window_closed(now: datetime) -> str | None:
    """연금 — 살 때가 아니면 이유, 사도 되면 None."""
    # > : 목(3)까지는 산다. 금-일은 다음 주 회차라 월요일 로또 로그인 때 같이 산다
    if now.weekday() > PENSION_LAST_BUY_WEEKDAY:
        return "금-일 (연금은 월-목에 로또와 같이)"
    # >= : 16:30 정각부터 막는다 (목 17:00 마감보다 30분 여유)
    if now.weekday() == PENSION_LAST_BUY_WEEKDAY and now.time() >= PENSION_THU_CUTOFF:
        return "목 16:30 이후 (판매 마감 17:00)"
    # 연금은 사이트 판매 여부(roundRemainTime IS_SALE_FLAG)도 사기 직전에 한 번 더 본다 (buy_pension)
    return before_sales_start(now)


def open_rounds(now: datetime) -> tuple[dict[str, int], list[str]]:
    """({게임: 지금 살 회차}, [닫힌 이유]) — 판매 시간인 게임만."""
    rounds, closed = {}, []
    if LOTTO in ENABLED_GAMES:            # 설정에서 고르지 않은 게임은 아예 안 본다 (닫힌 이유에도 안 적는다)
        lotto_closed = buy_window_closed(now)
        if lotto_closed:
            closed.append(f"로또 {lotto_closed}")
        else:
            rounds[LOTTO] = sales_round(now.date())
    if PENSION in ENABLED_GAMES:
        pension_closed = pension_window_closed(now)
        if pension_closed:
            closed.append(f"연금 {pension_closed}")
        else:
            rounds[PENSION] = pension_week_round(now.date())
    if not closed and not rounds:
        closed.append("설정에서 고른 게임 없음")
    return rounds, closed


def needs_login(game: str, round_no: int) -> bool:
    """DB·state 만 보고 이 게임의 이번 회차를 아직 살 게 남았나 (로그인할 이유가 되나)."""
    if halted(game, round_no):
        return False
    if game == LOTTO:
        # < : DB(사이트에서 채운 것)나 결제 시도 기록이 이번 회차 3게임이면 로그인 안 함
        return max(db_tickets(LOTTO, round_no), lotto_attempted_games(round_no)) < MAX_GAMES_PER_ROUND
    # 연금 이번 회차: 이미 결제를 시도했거나(결과 불명이어도) 예약이 걸려 있으면 다시 안 산다 — 기록은 check 가 줍는다
    return (db_tickets(PENSION, round_no) < PENSION_MAX_TICKETS and not pension_attempted(round_no)
            and round_no not in reserved_known())


def reserve_rounds_after(cur_round: int) -> list[int]:
    """예약 한 번이 덮는 회차 = 지금 판매 중인 회차의 다음 회차부터 연속 PENSION_RESERVE_ROUNDS 개."""
    return list(range(cur_round + 1, cur_round + 1 + PENSION_RESERVE_ROUNDS))


def reserve_due(cur_round: int) -> bool:
    """state 만 보고 예약을 넣을 때인가 — 다음 5회차 중 예약(시도·사이트에서 본 것)이 걸린 회차가 하나도 없을 때만.
    규칙: 5회차가 전부 비었을 때만 5주치를 한 번에 예약한다 (사이트 예약은 '다음 회차부터 연속'만 돼서 빈 뒤쪽만 따로 못 넣는다).
    일부만 걸려 있으면 예약을 안 넣고, 빈 회차는 그 주가 되면 '이번 회차 직접 구매'가 메운다.
    실패한 예약 시도(failed)도 여기선 막는다 — 같은 회차들에 예약을 또 시도하지 않는다 (그 5주는 주간 직접 구매로)."""
    targets = set(reserve_rounds_after(cur_round))
    return not (targets & (reserve_attempt_rounds(only_counted=False) | reserved_seen()))


def wanted(rounds: dict[str, int]) -> set[str]:
    """DB·state 기준 이번 실행에 할 일 — LOTTO · PENSION(이번 회차 직접 구매) · RESERVE(다음 5회차 예약). 비면 로그인 안 함."""
    todo = {g for g, r in rounds.items() if needs_login(g, r)}
    # 예약은 ① 스위치가 켜져 있고 ② 이번 주에 '예약 값이 모자람'을 이미 알린 적이 없을 때만 할 일이다
    # (예약만 모자란 상태로 날마다 로그인·경보하지 않는다 — 주 1번. 충전했으면 다음 주 월요일이 알아서, 바로 하려면 state 의 reserve_short_week 를 지운다)
    short_this_week = load_state().get("reserve_short_week") == week_key(now_kst().date())
    if PENSION in rounds and reserve_enabled() and not short_this_week and reserve_due(rounds[PENSION]):
        todo.add(RESERVE)
    return todo


# ---------- buy: 얼마나 살지 ----------
def split_budget(buyable: int, lotto_games: int, pension_sets: int) -> tuple[int, int, int]:
    """예치금이 모자랄 때 나누는 규칙 → (살 연금 세트, 살 로또 게임, 모자란 원).
    연금 세트(쪼갤 수 없음)를 먼저 — 마감이 목요일로 더 빠르다. 남는 돈으로 로또를 한 게임 단위로.
    예약(5주치)은 이 둘을 사고 남은 돈이 예약 값 이상일 때만 (cmd_buy) — 마감이 없어서 맨 뒤."""
    buyable = max(buyable, 0)   # 음수(수수료·출금 신청 등)로 와도 '0원'으로
    # >= : 딱 맞게 있으면 산다
    sets = pension_sets if buyable >= pension_sets * SET_COST else 0
    left = buyable - sets * SET_COST
    games = min(lotto_games, left // GAME_PRICE)
    short = (pension_sets - sets) * SET_COST + (lotto_games - games) * GAME_PRICE
    return sets, games, short


def unknown_rows(orders: list[dict]) -> list[dict]:
    return [o for o in orders if o.get("unknown")]


def round_have(orders: list[dict], reservations: list[dict], lotto_round: int | None, pension_rounds: list[int]) -> dict[tuple[str, int], int]:
    """회차별 '이미 있는 양' — 사이트 구매내역 · 사이트 예약내역 · state 시도 기록 중 **큰 값**.
    (예약이 실행되면 구매내역에도 같은 장이 뜨니 더하지 않는다. 연금은 0 이 아니면 세트를 더 못 사니 어느 쪽이든 결론은 같다.)
    못 읽은 줄이 하나라도 있으면 CapError — 추측해서 세지 않는다."""
    unknown = unknown_rows(orders)
    if unknown:
        raise CapError(f"구매내역에 못 읽은 줄 {len(unknown)}개 ({', '.join(row_label(o) for o in unknown)})")
    have: dict[tuple[str, int], int] = {}
    if lotto_round is not None:
        have[(LOTTO, lotto_round)] = max(tickets_in(orders, LOTTO, lotto_round), lotto_attempted_games(lotto_round))
    counted = reserve_attempt_rounds(only_counted=True)
    for r in pension_rounds:
        tried = PENSION_MAX_TICKETS if pension_attempted(r) or r in counted else 0
        have[(PENSION, r)] = max(tickets_in(orders, PENSION, r), reserved_in(reservations, r), tried)
    return have


def assert_round_caps(have: dict[tuple[str, int], int], add: dict[tuple[str, int], int]) -> None:
    """결제 요청 직전 안전핀 — 이 요청 하나가 끝난 뒤 건드린 회차마다 (이미 있는 양 + 이번에 살 양) ≤ 회차 상한.
    add = {(게임, 회차): 살 양}. 한 요청은 한 게임만, 회차 수는 로또 1 · 연금 5(예약) 까지. 어기면 CapError."""
    games = {g for g, _ in add}
    if not add or len(games) != 1:
        raise CapError(f"상한 확인 불가 — 결제 요청 하나에 게임이 {len(games)}개")
    game = next(iter(games))
    # > : 회차 수가 한도와 딱 같은 건 허용 (예약 5회차)
    if game not in ROUND_CAP or len(add) > REQUEST_MAX_ROUNDS[game]:
        raise CapError(f"상한 초과 시도 — {game} 한 요청에 {len(add)}개 회차")
    for (_, round_no), count in add.items():
        if (game, round_no) not in have:
            raise CapError(f"상한 확인 불가 — {GAME_LABEL[game]} {round_no}회에 이미 있는 양을 안 셈")
        after = have[(game, round_no)] + count
        # > : 회차 상한과 딱 같은 건 허용 (로또 3게임 · 연금 5장). <= 0 : 0·음수 구매는 잘못된 요청
        if count <= 0 or after > ROUND_CAP[game]:
            raise CapError(f"상한 초과 시도 — {GAME_LABEL[game]} {round_no}회 {have[(game, round_no)]}+{count}{GAME_UNIT[game]} "
                           f"(회차 상한 {ROUND_CAP[game]}{GAME_UNIT[game]})")
    # 금액으로 한 번 더: 한 요청 값이 (회차 수 × 회차 상한) 을 못 넘는다 — 예약 25,000원이 최대
    if sum(add.values()) * GAME_PRICE > REQUEST_MAX_ROUNDS[game] * ROUND_CAP[game] * GAME_PRICE:
        raise CapError("상한 초과 시도 — 한 요청 금액이 한도를 넘음")


# ---------- buy: 실제 구매 ----------
def buy_pension(client: RawLotteryClient, round_no: int) -> dict:
    """연금 모든 조 세트 1개 (이번 회차 직접 구매). 결제(connPro)는 회차당 1번 — 결제 직전 state 에 기록한다."""
    brief = pension720.short_text
    game = pension720.PensionGame(client._session, DRY)
    try:
        game.open()
        info = game.round_info()
    except Exception as e:  # noqa: BLE001
        alert("pension-open", "연금 구매 준비 실패", f"{round_no}회 연금 게임 페이지를 못 열어서 안 샀어요 (돈 안 나감): {brief(str(e), 120)}")
        return {"error": "준비 실패"}
    # 사이트 회차·판매 여부가 날짜 계산과 다르면 안 산다 (설·추석 휴무로 회차가 밀리는 경우 등)
    if str(info.get("ROUND")) != str(round_no) or info.get("IS_SALE_FLAG") != "true":
        alert("pension-round", "연금 회차 불일치",
              f"계산 {round_no}회 · 사이트 {brief(info.get('ROUND'))}회 판매중={brief(info.get('IS_SALE_FLAG'))} "
              f"(마감 {brief(info.get('CLOSE_DATE_TIME'))}). 안 샀어요 — 회차 공식 확인 필요.")
        return {"error": "회차 불일치"}
    try:
        number = game.assign_set_number(round_no)
    except Exception as e:  # noqa: BLE001
        alert("pension-number", "연금 번호 배정 실패", f"{round_no}회 번호를 못 받아서 안 샀어요 (돈 안 나감): {brief(str(e), 120)}")
        return {"error": "번호 배정 실패"}
    if DRY:
        print(f"[DRY] 연금 {round_no}회 (마감 {brief(info.get('CLOSE_DATE_TIME'))}) · 배정 번호 {number} → "
              f"1-{PENSION_TICKETS_PER_SET}조 {PENSION_TICKETS_PER_SET}장 {won(SET_COST)} 살 차례 (주문·결제 안 함)")
        return {"dry": True, "number": number}
    marked = []   # 이번 실행이 '결제 시도'를 적었나 — 적은 뒤의 실패만 '결과 불명'

    def before_payment() -> None:
        mark_pension_attempt(round_no)
        marked.append(round_no)

    try:
        paid = game.buy_set(round_no, number, before_payment=before_payment)
    except Exception as e:  # noqa: BLE001
        if marked:
            alert("pension-unknown", "연금 결제 결과 불명 — 구매내역 확인",
                  f"{round_no}회 {number} 세트 결제 요청 뒤 응답을 못 받았어요: {brief(str(e), 120)}. 이번 회차는 다시 사지 않아요. 동행복권 구매내역 확인해 주세요.")
            return {"error": "결과 불명", "attempted": True}
        alert("pension-order", "연금 주문 실패", f"{round_no}회 결제 요청 전에 멈춤 (돈 안 나감): {brief(str(e), 120)}")
        return {"error": "주문 실패"}
    if paid["code"] != pension720.SUCCESS:
        alert("pension-unknown", "연금 결제 결과 불명 — 구매내역 확인",
              f"{round_no}회 {number} 세트 결제 응답 코드 {brief(paid['code'])} ({paid.get('msg') or '설명 없음'}). 이번 회차는 다시 사지 않아요. 구매내역 확인해 주세요.")
        return {"error": f"결제 코드 {brief(paid['code'])}", "attempted": True}
    return {"number": number, "tickets": len(paid["tickets"]), "attempted": True}


def reserve_pension(client: RawLotteryClient, cur_round: int, rounds: list[int], deposit: int) -> dict:
    """연금 다음 회차부터 연속 예약 (모든 조 세트 1개씩). 결제(addMyReserve)는 한 번 — 결제 직전 state 에 회차들을 기록한다.
    여기선 경보를 '결제 요청 전에 멈춘 것'만 보낸다. 요청을 보낸 뒤의 성공·실패·불명 판정은 부른 쪽이 예약내역을 다시 읽고 한다."""
    brief = pension720.short_text
    game = pension720.PensionGame(client._session, DRY)
    try:
        game.open()
        game.open_reserve()
        info = game.round_info()
        if str(info.get("ROUND")) != str(cur_round) or info.get("IS_SALE_FLAG") != "true":
            raise pension720.PensionError(f"회차 불일치 — 계산 {cur_round}회 · 사이트 {brief(info.get('ROUND'))}회 판매중={brief(info.get('IS_SALE_FLAG'))}")
        win_dates = pension720.reserve_draw_dates(info, cur_round, rounds)
    except Exception as e:  # noqa: BLE001
        alert("reserve-open", "연금 예약 준비 실패", f"{rounds[0]}-{rounds[-1]}회 예약을 준비하다 멈췄어요 (돈 안 나감): {brief(str(e), 120)}")
        return {"error": "준비 실패"}
    if DRY:
        print(f"[DRY] 연금 예약 계획: {rounds[0]}-{rounds[-1]}회 ({len(rounds)}회차 · 추첨 {win_dates[0]} - {win_dates[-1]}) 모든 조 세트 1개씩 "
              f"{won(len(rounds) * SET_COST)} — 예치금에서 바로 차감 (겹침 확인·예약 요청 안 보냄)")
        return {"dry": True, "rounds": rounds}
    marked = []

    def before_payment() -> None:
        mark_reserve_attempt(rounds, deposit)
        marked.append(True)

    try:
        paid = game.reserve(cur_round, rounds, win_dates, deposit, before_payment=before_payment)
    except Exception as e:  # noqa: BLE001
        if marked:
            return {"error": "결과 불명", "attempted": True, "detail": f"응답을 못 받음 ({brief(str(e), 120)})"}
        alert("reserve-order", "연금 예약 실패", f"{rounds[0]}-{rounds[-1]}회 예약이 결제 요청 전에 멈춤 (돈 안 나감): {brief(str(e), 120)}")
        return {"error": "예약 실패"}
    if paid["code"] != pension720.SUCCESS:
        return {"error": f"예약 코드 {brief(paid['code'])}", "attempted": True, "rejected": True,
                "detail": f"응답 코드 {brief(paid['code'])} ({paid.get('msg') or '설명 없음'})"}
    return {"rounds": rounds, "attempted": True}


def site_lotto_round() -> int:
    """로또 — 사이트 기준 지금 판매 중인 회차 = 마지막으로 추첨된 회차 + 1 (공개 당첨번호 API, 로그인 없음 — 회차를 비워 부르면 최근 회차를 준다,
    2026-11-16 실측: 1250회·20261114). 못 읽으면 예외 — 부르는 쪽은 사지 않는다. 토 19:30 이후·일요일엔 안 사니 '추첨 직후' 어긋남은 없다."""
    import requests  # noqa: PLC0415  (dhapi 의존성 — 늘 있음)
    resp = requests.get(LOTTO_DRAW_URL.format(""), timeout=pension720.TIMEOUT,
                        headers={"User-Agent": pension720.DESKTOP_UA, "Accept": "application/json"})
    drawn = [int(x["ltEpsd"]) for x in ((resp.json() or {}).get("data") or {}).get("list") or []]
    if not drawn:
        raise RuntimeError("최근 추첨 회차가 응답에 없음")
    return max(drawn) + 1


def lotto_round_stop(round_no: int) -> str | None:
    """계산한 로또 회차를 사이트 회차와 대조 (공개 API — 로그인 없음). 같으면 None, 못 읽거나 다르면 경보를 보내고 안 사는 이유를 돌려준다."""
    try:
        site_round = site_lotto_round()
    except Exception as e:  # noqa: BLE001
        alert("lotto-round", "로또 회차 확인 실패 — 안 삼", f"사이트에서 지금 회차를 못 읽어서 {round_no}회를 안 샀어요 (돈 안 나감): {pension720.short_text(str(e), 120)}")
        return "회차 확인 실패"
    if site_round != round_no:
        alert("lotto-round", "로또 회차 불일치 — 안 삼", f"계산 {round_no}회 · 사이트 {site_round}회 (마지막 추첨 회차 + 1). 안 샀어요 — 회차 공식 확인 필요.")
        return "회차 불일치"
    return None


def buy_lotto(client: RawLotteryClient, capture: Capture, round_no: int, games: int) -> dict:
    """로또 자동 games 게임. 결제 요청 직전 state 에 (회차, 게임 수)를 기록한다 — 기록이 안 되면 안 산다.
    실패 경보는 여기서 안 보낸다: 부른 쪽이 구매내역을 다시 읽어 '실패(돈 안 나감)'인지 '결과 불명'인지 가려서 보낸다."""
    # 사기 직전 사이트 회차와 대조 (연금과 같은 원칙 — 설·추석 휴무로 회차가 밀리면 날짜 공식이 어긋난다). 못 읽거나 다르면 안 산다
    round_stop = lotto_round_stop(round_no)
    if round_stop:
        return {"error": round_stop}
    if DRY:
        print(f"[DRY] 로또 {round_no}회 (사이트 회차와 같음) → 자동 {games}게임 {won(games * GAME_PRICE)} 살 차례 (안 삼)")
        return {"dry": True, "games": games}
    capture.bought_slots = []
    try:
        mark_lotto_attempt(round_no, games)
    except Exception as e:  # noqa: BLE001
        alert("buy", "로또 구매 안 함", f"{round_no}회 결제 시도 기록을 못 써서 안 샀어요 (돈 안 나감): {pension720.short_text(str(e), 120)}")
        return {"error": "시도 기록 실패"}
    try:
        client.buy_lotto645(Lotto645Ticket.create_auto_tickets(games))
    except Exception as e:  # noqa: BLE001
        return {"error": "구매 실패", "attempted": True, "games": games, "detail": pension720.short_text(str(e), 120)}
    # dhapi slots [{'slot': 'A', 'mode': '자동', 'numbers': ['01', '02', …]}] — 못 읽으면 빈 목록 (알림엔 '번호는 구매내역에서')
    numbers = []
    for slot in capture.bought_slots or []:
        try:
            numbers.append(sorted(int(n) for n in slot["numbers"]))
        except (KeyError, TypeError, ValueError):
            continue
    return {"games": games, "numbers": numbers, "attempted": True}


def read_balance(client: RawLotteryClient, capture: Capture, buying: bool = True) -> int | None:
    """구매가능 예치금. 못 읽으면 None — buy 면 경보, check 면(이상징후 감지용이라) 한 줄 찍기만."""
    capture.balance = {}
    try:
        client.show_balance()
    except Exception as e:  # noqa: BLE001
        if buying:
            alert("balance", "예치금 조회 실패", f"예치금을 못 읽어서 안 샀어요: {pension720.short_text(str(e), 120)}")
        else:
            print(f"[lotto] 예치금 조회 실패 (잔액 이상징후 감지는 건너뜀): {e}")
        return None
    return int(capture.balance.get("구매가능금액") or 0)


def site_summary(orders: list[dict], open_now: dict[str, int]) -> str:
    """'로또 1251회 0게임 · 연금 342회 5장' — 판매 중인 게임 전부 (이번 실행에 안 사는 게임도)."""
    return " · ".join(f"{GAME_LABEL[g]} {r}회 {tickets_in(orders, g, r)}{GAME_UNIT[g]}" for g, r in open_now.items())


def bought_ok(result: dict | None) -> bool:
    return bool(result) and not result.get("dry") and "error" not in result


def purchase_message(rounds: dict[str, int], results: dict[str, dict], week_won: int) -> str | None:
    """'이번 주 복권을 샀어요 🍀' — 이번 실행에서 실제로 산 게임만 줄로 (예약은 reserve_message 가 따로). 산 게 없으면 None."""
    lines = []
    pension = results.get(GAME_LABEL[PENSION]) or {}
    if pension.get("number") and bought_ok(pension):
        tickets = [(g, pension["number"]) for g in pension720.GROUPS]   # 모든 조 세트 = 1-5조 같은 번호
        lines.append(f"연금복권 {rounds[PENSION]}회: {pension720.compact_tickets(tickets)}")
    lotto = results.get(GAME_LABEL[LOTTO]) or {}
    if lotto.get("games") and bought_ok(lotto):
        picked = " / ".join(", ".join(str(n) for n in nums) for nums in lotto.get("numbers") or [])
        lines.append(f"로또 {rounds[LOTTO]}회: " + (picked or f"자동 {lotto['games']}게임 (번호는 구매내역에서)"))
    if not lines:
        return None
    return "\n".join(["이번 주 복권을 샀어요 🍀", *lines, f"(이번 주 {won(week_won)} · 설정한 한도 {won(WEEKLY_CAP_WON)} 안)"])


def reserve_message(rounds: list[int], paid_won: int) -> str:
    """'연금복권 5주치를 예약했어요 343-347회 · 2만 5,000원' — 회차는 예약내역에서 본 것, 금액은 실제로 줄어든 예치금."""
    return f"연금복권 {len(rounds)}주치를 예약했어요 {rounds[0]}-{rounds[-1]}회 · {won(paid_won)}"


def send_purchase(rounds: dict[str, int], results: dict[str, dict], week_won: int) -> None:
    """산 게 있으면 알림 1통 (🚨 없이). 경보는 alert() 가 따로.
    지난 로그인 뒤 예치금이 예상보다 늘어 있었으면(충전·입금) 한 줄 얹는다 — 발송이 성공하면 그 기록을 지운다."""
    text = purchase_message(rounds, results, week_won)
    if not text:
        return
    up = to_int(load_state().get("deposit_up"))
    if up > 0:
        text += f"\n참고: 예치금이 예상보다 {won(up)} 늘어 있었어요 (충전·당첨금 입금 — 직접 넣은 게 아니면 확인하세요)"
    notifier().write_brief("lotto", [(SECTOR, "구매", "꿈 사왔어", text)])
    if notifier().send(text) and up > 0:
        state = load_state()
        state.pop("deposit_up", None)
        save_state(state)


def verify_purchases(client: RawLotteryClient, capture: Capture, rounds: dict[str, int], results: dict[str, dict],
                     before: list[dict]) -> bool:
    """결제 요청을 보낸 뒤 구매내역을 다시 읽어 확인한다 (직접 구매한 로또·연금). 확인이 안 돼서 멈춘(halt) 회차가 있으면 True.
    - 성공했다는데 구매내역에 안 보이면 경보 + 그 회차는 손으로 풀 때까지 더 안 산다 (halt)
    - 로또가 실패로 끝났으면 구매내역으로 '실패(돈 안 나감)' · '결과 불명'을 가려서 경보
    결제는 어떤 경우에도 다시 시도하지 않는다."""
    stopped = False
    lotto, pension = results.get(GAME_LABEL[LOTTO]) or {}, results.get(GAME_LABEL[PENSION]) or {}
    try:
        after = fetch_orders(client, capture, BUY_LOOKBACK_DAYS)
        if unknown_rows(after):
            raise RuntimeError("구매내역에 못 읽은 줄")
    except Exception as e:  # noqa: BLE001
        after = None
        print(f"[lotto] 구매 뒤 재조회 실패: {e}")
    if after is not None:
        upsert_orders(after)
    if lotto.get("attempted"):
        r, games = rounds[LOTTO], lotto["games"]
        had = tickets_in(before, LOTTO, r)
        now_has = tickets_in(after, LOTTO, r) if after is not None else None
        # >= : 산 만큼(또는 그 이상) 늘었으면 보이는 것
        visible = now_has is not None and now_has >= had + games
        if "error" in lotto:
            # dhapi 가 응답을 못 읽었을 때('알 수 없는 오류')는 서버가 거절했다는 뜻이 아니다 — 결제가 됐는지 모른다
            no_reply = UNKNOWN_ERROR_MARK in str(lotto.get("detail") or "")
            if now_has is None:
                verdict = "결과 불명 — 구매내역도 못 읽었어요. 동행복권 구매내역 확인해 주세요"
            elif now_has > had:
                verdict = f"응답은 실패인데 구매내역엔 {now_has - had}게임이 새로 있어요 (돈 나감)"
            elif no_reply:
                verdict = "결과 불명 — 응답을 못 읽었고 지금 구매내역엔 없어요 (늦게 뜰 수 있으니 구매내역·예치금 확인해 주세요)"
            else:
                verdict = "실패 — 구매내역에 없어요 (돈 안 나감)"
            # 서버가 사유를 대며 거절했고 구매내역도 그대로일 때만 '실패'. 나머지는 전부 '결과 불명'
            refused = now_has == had and not no_reply
            alert("buy", "로또 구매 실패" if refused else "로또 구매 결과 불명 — 구매내역 확인",
                  f"{r}회 자동 {games}게임: {verdict}. ({lotto.get('detail')}) 이번 회차 이 {games}게임은 다시 사지 않아요 "
                  f"(다시 사려면 state.json lotto_attempts 의 {r} 을 지워).")
        elif not visible:
            stopped = True
            halt(LOTTO, r, "산 게 구매내역에 안 보임")
            alert("buy-verify", "로또 구매 확인 안 됨", f"{r}회 {games}게임을 샀다는데 구매내역에 안 보여"
                  f"{'' if after is not None else ' (구매내역 재조회 실패)'}. 이번 회차는 더 사지 않아요 — 구매내역 확인해 주세요.")
    if pension.get("attempted") and "error" not in pension:
        r = rounds[PENSION]
        # >= : 세트 5장이 다 보여야 확인된 것
        if after is None or tickets_in(after, PENSION, r) < PENSION_TICKETS_PER_SET:
            stopped = True
            halt(PENSION, r, "산 게 구매내역에 안 보임")
            alert("buy-verify-pension", "연금 구매 확인 안 됨", f"{r}회 세트를 샀다는데 구매내역에 {PENSION_TICKETS_PER_SET}장이 안 보여"
                  f"{'' if after is not None else ' (구매내역 재조회 실패)'}. 이번 회차는 더 사지 않아요 — 구매내역 확인해 주세요.")
    return stopped


def live_reserved(reservations: list[dict]) -> dict[int, int]:
    """예약내역 → {회차: 살아 있는(취소 아님) 예약 장 수}. 0장인 회차는 안 넣는다."""
    out: dict[int, int] = {}
    for r in reservations:
        if not r["cancelled"]:
            out[r["round"]] = out.get(r["round"], 0) + r["qty"]
    return out


def settle_reservation(client: RawLotteryClient, capture: Capture, rounds: list[int], result: dict,
                       before: list[dict], deposit_before: int) -> dict:
    """예약 요청을 보낸 뒤 예약내역·예치금을 다시 읽어 판정하고 state 에 결과를 적는다 (재시도 없음).
    before = 요청 전에 읽은 예약내역, deposit_before = 요청 직전 구매가능 예치금.
    ok      — 셋 다 맞음: ① 예약한 회차마다 딱 5장 ② 그 밖의 회차는 요청 전과 그대로 ③ 예치금이 딱 예약 값만큼 줄었음 → 예약 핑
    failed  — 서버가 코드로 거절했고, 예약내역이 요청 전과 그대로고, 예치금도 안 줄었음 → 그 회차들은 주간 직접 구매로 (예약은 다시 시도 안 함)
    unknown — 그 밖 전부 → 바로 경보, '예약 있음'으로 치고(안 사는 쪽) 사람이 확인"""
    span = f"{rounds[0]}-{rounds[-1]}회"
    cost = len(rounds) * SET_COST
    try:
        after = live_reserved(fetch_reservations(client))
    except Exception as e:  # noqa: BLE001
        after = None
        print(f"[lotto] 예약 뒤 예약내역 재조회 실패: {e}")
    deposit_after = read_balance(client, capture, buying=False)
    paid_won = None if deposit_after is None else deposit_before - deposit_after
    got, stray = {}, {}
    if after is not None:
        was = live_reserved(before)
        got = {r: after.get(r, 0) for r in rounds}
        # 예약하지 않은 회차의 장 수가 요청 전과 달라졌으면 '엉뚱한 회차' (늘었든 줄었든)
        stray = {r: after.get(r, 0) for r in sorted(set(was) | set(after)) if r not in got and after.get(r, 0) != was.get(r, 0)}
    # rejected = 서버가 코드로 거절했나 — 나중에(resolve_reserve_attempts) '실패'로 내려도 되는지의 조건
    seen = {"deposit_after": deposit_after, "rejected": bool(result.get("rejected"))}
    # == 셋 다 : 회차당 딱 5장 · 엉뚱한 회차 없음 · 줄어든 돈이 딱 예약 값 (더 많이도 더 적게도 아님)
    if after is not None and all(q == PENSION_MAX_TICKETS for q in got.values()) and not stray and paid_won == cost:
        set_reserve_result(rounds, "ok", **seen)
        text = reserve_message(sorted(got), paid_won)
        notifier().write_brief("lotto", [(SECTOR, "예약", "연금 예약", text)])
        notifier().send(text)
        return {"rounds": rounds, "attempted": True}
    untouched = after is not None and not any(got.values()) and not stray
    # <= 0 : 예치금이 그대로거나 늘었을 때만 '돈 안 나감'. 줄었으면(1원이라도) 실패로 못 적는다
    if untouched and result.get("rejected") and paid_won is not None and paid_won <= 0:
        set_reserve_result(rounds, "failed", **seen)
        alert("reserve-failed", "연금 예약 실패", f"{span} 예약을 서버가 거절했고 예약내역에도 없고 예치금도 그대로야 (돈 안 나감): {result.get('detail')}. "
              f"예약은 다시 시도 안 하고, 이 회차들은 그 주에 한 세트씩 직접 사요. 예약을 다시 걸려면 state.json pension_reserve_attempts 의 이 줄을 지워.")
        return {"error": result.get("error") or "예약 실패", "attempted": True}
    set_reserve_result(rounds, "unknown", **seen)
    full = [r for r, q in got.items() if q == PENSION_MAX_TICKETS]
    where = "예약내역을 못 읽었어" if after is None else (
        f"예약내역엔 {len(full)}/{len(rounds)}개 회차만 {PENSION_MAX_TICKETS}장 (회차별 {', '.join(f'{r}회 {q}장' for r, q in got.items())})"
        + (f" · 예약 안 한 회차가 바뀜 {', '.join(f'{r}회 {q}장' for r, q in stray.items())}" if stray else ""))
    money = "예치금을 못 읽었어" if paid_won is None else f"예치금 {won(deposit_before)} → {won(deposit_after)} (줄어든 돈 {won(paid_won)} · 예약 값 {won(cost)})"
    alert("reserve-unknown", "연금 예약 결과 불명 — 예약내역 확인", f"{span} 예약 요청을 보냈는데 확인이 안 맞아: {where} · {money} ({result.get('detail') or '응답은 성공'}). "
          f"예약된 걸로 치고 이 회차들은 사지 않아요 — 동행복권 예약구매 내역·예치금 확인해 주세요. {RESERVE_MARK_HINT}")
    return {"error": "예약 결과 불명", "attempted": True}


RESERVE_MARK_HINT = ("다음 로그인 때 예약내역에 회차마다 5장이 보이면 알아서 ok 로 적어요. 안 보이면 불명 그대로 둬요 — "
                     "직접 확인하고 state.json(Actions 는 state 브랜치) 그 줄의 result 를 \"ok\"(예약 있음)·\"failed\"(예약 없음)로 고쳐 주세요.")


def warn_pending_reserve() -> None:
    """시작할 때 — 예약 시도가 'pending' 으로 남아 있으면(요청 보내는 도중 프로세스가 끊김) 알린다. 다음 로그인 때 예약내역으로 가린다."""
    stuck = [a for a in reserve_attempts() if a.get("result") == "pending"]
    if stuck:
        spans = ", ".join(f"{a['rounds'][0]}-{a['rounds'][-1]}회" for a in stuck if a.get("rounds"))
        alert("reserve-pending", "연금 예약 요청 중 끊김 — 예약내역 확인", f"{spans} 예약 요청을 보내는 도중 실행이 끊겨서 됐는지 몰라. "
              f"예약된 걸로 치고 이 회차들은 사지 않아요. 동행복권 예약구매 내역·예치금 확인해 주세요. {RESERVE_MARK_HINT}")


def resolve_reserve_attempts(reservations: list[dict], balance: int | None) -> None:
    """로그인한 김에 — 판정이 안 끝난 예약 시도(pending·unknown)를 방금 읽은 예약내역으로 가린다 (요청은 안 보낸다).
    ok     — 그 회차들이 전부 딱 5장씩 살아 있음 → 예약 핑 (그때 못 보낸 것)
    failed — 그때 서버가 거절했다고 적혀 있고(rejected) + 그 회차들에 예약이 하나도 없고 + 돈도 안 나갔음 → 주간 직접 구매로 + 경보 한 줄
             (돈 안 나감 = 그때 요청 뒤 읽은 예치금이 요청 전 이상. 그때 못 읽었으면 지금 예치금이 요청 전 이상)
    그 밖  — unknown 그대로 (pending 은 unknown 으로). 서버가 성공이라 했거나 응답을 못 본(끊김) 시도는 예약내역에 안 보여도
             실패로 안 내린다 — 예약이 늦게 뜨는 경우에 같은 회차를 직접 또 사게 되니까. 사람이 state.json 에서 ok·failed 로 적는다"""
    live = live_reserved(reservations)
    for a in unresolved_reserve_attempts():
        rounds = [int(r) for r in a.get("rounds", [])]
        if not rounds:
            continue
        span = f"{rounds[0]}-{rounds[-1]}회"
        got = [live.get(r, 0) for r in rounds]
        before, after = a.get("deposit_before"), a.get("deposit_after")
        now_money = after if after is not None else balance
        # >= : 예치금이 요청 전과 같거나 많으면 돈이 안 나간 것. 요청 전 값을 모르면 판단 못 함
        money_kept = before is not None and now_money is not None and now_money >= before
        if all(q == PENSION_MAX_TICKETS for q in got):
            set_reserve_result(rounds, "ok")
            text = reserve_message(rounds, len(rounds) * SET_COST) + " (지난번 결과 불명이던 예약 — 예약내역에서 확인됨)"
            notifier().write_brief("lotto", [(SECTOR, "예약", "연금 예약 확인", text)])
            notifier().send(text)
        # is True : 거절 기록이 없는 옛 줄·끊긴 줄(pending)은 거절로 안 친다
        elif not any(got) and money_kept and a.get("rejected") is True:
            set_reserve_result(rounds, "failed")
            alert("reserve-failed", "연금 예약 안 된 걸로 확인", f"결과 불명이던 {span} 예약: 서버가 거절했었고 예약내역에 없고 예치금도 안 줄었어 (돈 안 나감). "
                  f"실패로 적었고 이 회차들은 그 주에 한 세트씩 직접 사요. 예약을 다시 걸려면 state.json pension_reserve_attempts 의 이 줄을 지워.")
        elif a.get("result") == "pending":
            set_reserve_result(rounds, "unknown")
            alert("reserve-unknown", "연금 예약 결과 불명 — 예약내역 확인", f"끊겼던 {span} 예약 요청: 예약내역엔 회차별 {', '.join(f'{r}회 {q}장' for r, q in zip(rounds, got))}. "
                  f"예약된 걸로 치고 이 회차들은 사지 않아요 — 동행복권 예약구매 내역·예치금 확인해 주세요. {RESERVE_MARK_HINT}")


def cmd_buy() -> None:
    validate_caps()   # 누가 런타임에 상수를 바꿨어도 여기서 멈춘다
    warn_pending_reserve()
    rounds, closed = open_rounds(now_kst())
    if not rounds:
        emit(skipped=f"판매 시간 아님 — {' · '.join(closed)}")
        return
    todo = wanted(rounds)
    if not todo:
        emit(skipped=f"DB·기록 기준 할 일 없음 ({', '.join(f'{GAME_LABEL[g]} {r}회' for g, r in rounds.items())}) — 로그인 안 함")
        return
    stop = login_stop_reason(now_kst())
    if stop:
        emit(skipped=stop)
        return
    if todo == {LOTTO}:
        # 할 일이 로또뿐이면 로그인 전에 회차부터 대조한다 (공개 API) — 회차가 어긋난 주에 칸마다 로그인하지 않게.
        # 같이 살 게 있으면 그대로 로그인하고 buy_lotto 가 사기 직전에 대조한다 (그 뒤 칸은 할 일이 로또뿐이라 여기서 멈춘다)
        round_stop = lotto_round_stop(rounds[LOTTO])
        if round_stop:
            emit(ok=False, error=f"로또 {round_stop} — 로그인 안 함")
            return
    jitter()
    open_now, closed = open_rounds(now_kst())   # 대기 뒤 다시 — 토 19:00·목 16:10 + 30분 대기가 마감을 넘길 수 있다
    rounds = {g: r for g, r in open_now.items() if g in todo}
    cur = open_now.get(PENSION)                  # 연금 지금 판매 중인 회차 (연금 창이 닫혔으면 None — 예약도 안 함)
    reserve_wanted = RESERVE in todo and cur is not None
    if not rounds and not reserve_wanted:
        emit(skipped=f"대기 뒤 판매 시간 지남 — {' · '.join(closed)}")
        return
    session = login()
    if session is None:
        emit(ok=False, error="로그인 실패 (경보 보냄)")
        return
    client, capture = session
    try:
        orders = fetch_orders(client, capture, BUY_LOOKBACK_DAYS)
    except Exception as e:  # noqa: BLE001
        alert("buy-list", "구매내역 조회 실패", f"구매내역을 못 읽어서 안 샀어요 (중복 구매 방지): {pension720.short_text(str(e), 120)}")
        emit(ok=False, error="구매내역 조회 실패")
        return
    if not DRY:
        upsert_orders(orders)
    buyable = None
    if not unknown_rows(orders):   # 못 읽은 줄이 있으면 어차피 아래에서 안 산다 (판정할 재료가 없다)
        buyable = read_balance(client, capture)
        if detect_anomalies(orders, buyable, now_kst()):
            # 실패(ok:false)가 아니라 건너뜀 — 알림은 이미 따로 나갔다
            emit(skipped="이상징후 감지 — 이번 실행은 구매 중단 (알림 보냄)")
            return

    # 예약내역은 ① 연금을 건드릴 때 ② 판정이 안 끝난 예약 시도가 있을 때만 읽는다.
    # 못 읽으면 연금은 직접 구매도 예약도 안 한다 (로또는 예약과 무관해서 그대로)
    unresolved = unresolved_reserve_attempts()
    reservations, pension_ok, reservations_read = [], True, False
    if (cur is not None and (PENSION in rounds or reserve_wanted)) or unresolved:
        try:
            reservations = fetch_reservations(client)
            reservations_read = True
        except Exception as e:  # noqa: BLE001
            pension_ok = False
            alert("reserve-list", "연금 예약내역 조회 실패 — 연금 안 삼",
                  f"예약내역을 못 읽어서 연금은 사지도 예약하지도 않았어 (중복 방지): {pension720.short_text(str(e), 120)}")
    if reservations_read and unresolved and not DRY:
        resolve_reserve_attempts(reservations, buyable)   # round_have 보다 먼저 — 실패로 가려진 회차는 '있음'에서 빠진다
    pension_rounds = [cur, *reserve_rounds_after(cur)] if cur is not None else []
    try:
        have = round_have(orders, reservations, rounds.get(LOTTO), pension_rounds)
    except CapError as e:
        alert("cap-count", "이미 산 양을 못 셈 — 안 삼", f"{e}. 설정한 한도({won(WEEKLY_CAP_WON)})를 지키려고 이번 실행은 안 샀어요. 구매내역 확인해 주세요.")
        emit(ok=False, error=f"구매량 못 셈: {e}")
        return
    if reservations_read and cur is not None and not DRY:
        # 사이트에서 본 살아 있는 예약 회차를 적어 둔다 — 다음 실행이 로그인 없이 '예약 있음'을 안다.
        # 예약내역을 실제로 읽었을 때만 (안 읽은 실행이 빈 목록으로 덮어쓰면 있는 예약을 잊는다)
        remember_reserved_seen({r for r in pension_rounds if reserved_in(reservations, r) > 0})

    lotto_need = MAX_GAMES_PER_ROUND - have[(LOTTO, rounds[LOTTO])] if LOTTO in rounds else 0
    lotto_need = max(lotto_need, 0)
    # == 0 : 이번 회차에 한 장이라도(구매·예약·시도) 있으면 세트를 더 못 산다 (5장 상한)
    pension_need = PENSION_SETS_PER_ROUND if PENSION in rounds and pension_ok and have[(PENSION, cur)] == 0 else 0
    reserve_rounds = reserve_rounds_after(cur) if reserve_wanted and pension_ok else []
    if reserve_rounds and any(have[(PENSION, r)] > 0 for r in reserve_rounds):
        covered = [r for r in reserve_rounds if have[(PENSION, r)] > 0]
        print(f"[lotto] 연금 예약 안 함: 다음 {PENSION_RESERVE_ROUNDS}회차 중 {covered} 에 이미 예약·구매가 있음 (빈 회차는 그 주에 직접 구매)")
        reserve_rounds = []
    if PENSION in open_now and pension_need == 0:
        print(f"[lotto] 연금 {cur}회 직접 구매 안 함: 이미 있음(구매·예약·결제 시도) 또는 예약내역 못 읽음 · 사이트 기준 {site_summary(orders, open_now)}")
    if lotto_need == 0 and pension_need == 0 and not reserve_rounds:
        emit(ok=pension_ok, rounds=rounds, note="살 것 없음 (이미 다 있음)", error=None if pension_ok else "예약내역 조회 실패")
        return

    if buyable is None:
        emit(ok=False, error="예치금 조회 실패")
        return
    sets, games, short = split_budget(buyable, lotto_need, pension_need)
    left = max(buyable, 0) - sets * SET_COST - games * GAME_PRICE
    reserve_cost = len(reserve_rounds) * SET_COST
    # < : 남은 돈이 예약 값과 딱 같으면 예약한다
    reserve_short = bool(reserve_rounds) and left < reserve_cost
    if reserve_short:
        if short == 0 and not DRY:
            # 모자란 게 예약 값뿐 — 이번 주엔 예약 때문에 다시 로그인·경보하지 않는다 (주 1번)
            state = load_state()
            state["reserve_short_week"] = week_key(now_kst().date())
            save_state(state)
        short += reserve_cost
        reserve_rounds = []
    if short > 0:
        alert("deposit", "예치금 부족 — 가상계좌 충전 필요",
              f"연금 {pension_need}세트·로또 {lotto_need}게임{f'·연금 예약 {won(reserve_cost)}' if reserve_cost else ''} 사려는데 구매가능 {won(buyable)} → "
              f"연금 {sets}세트·로또 {games}게임{'·예약' if reserve_rounds else ''}만 샀어요 ({won(short)} 모자람). 동행복권 가상계좌로 충전해 주세요. 오늘은 더 로그인 안 해"
              f"{' (예약은 이번 주엔 다시 안 봐요 — 다음 주 월요일에)' if reserve_short else ''}.")
        if not DRY:
            mark_deposit_short(now_kst())
    if DRY:
        print(f"[DRY] 구매가능 {won(buyable)} · 사이트 기준 이미 {site_summary(orders, open_now)}"
              f"{f' · 살아 있는 예약 회차 {sorted(r for r in pension_rounds if reserved_in(reservations, r) > 0)}' if cur is not None else ''}")

    results = {}
    # 구매 알림의 '이번 주 얼마' = 이번 주 두 회차(로또 이번 토 · 연금 이번 목)에 사이트 기준 이미 있는 것 + 이번에 산 것
    today = now_kst().date()
    week = {LOTTO: tickets_in(orders, LOTTO, sales_round(today)), PENSION: tickets_in(orders, PENSION, pension_week_round(today))}
    if sets:
        assert_round_caps(have, {(PENSION, cur): sets * PENSION_TICKETS_PER_SET})      # 결제 직전
        results[GAME_LABEL[PENSION]] = buy_pension(client, cur)
        # 결제 요청을 보냈으면(결과 불명 포함) 있는 걸로 친다
        if results[GAME_LABEL[PENSION]].get("attempted"):
            have[(PENSION, cur)] += sets * PENSION_TICKETS_PER_SET
            week[PENSION] += (sets * PENSION_TICKETS_PER_SET if bought_ok(results[GAME_LABEL[PENSION]]) else 0)
    if games:
        assert_round_caps(have, {(LOTTO, rounds[LOTTO]): games})                         # 결제 직전
        results[GAME_LABEL[LOTTO]] = buy_lotto(client, capture, rounds[LOTTO], games)
        if results[GAME_LABEL[LOTTO]].get("attempted"):
            have[(LOTTO, rounds[LOTTO])] += games
            week[LOTTO] += (games if bought_ok(results[GAME_LABEL[LOTTO]]) else 0)
    unverified = False
    if not DRY:
        send_purchase(rounds, results, sum(week.values()) * GAME_PRICE)
        if any(r.get("attempted") for r in results.values()):
            unverified = verify_purchases(client, capture, rounds, results, orders)
    if reserve_rounds:
        results["연금 예약"] = reserve_step(client, capture, cur, reserve_rounds, reservations, have, left,
                                        trouble=unverified or any("error" in r for r in results.values()))
    failed = any("error" in r for r in results.values()) or not pension_ok
    emit(ok=not failed, dry=DRY or None, rounds=rounds, balance=buyable, bought=results, short=short or None,
         week_won=None if DRY else sum(week.values()) * GAME_PRICE)


def reserve_step(client: RawLotteryClient, capture: Capture, cur: int, rounds: list[int], reservations: list[dict],
                 have: dict[tuple[str, int], int], expected_deposit: int, trouble: bool) -> dict:
    """예약 한 번 (이번 실행의 맨 마지막 — 25,000원이 한 번에 나간다). 조금이라도 이상하면 이번 실행엔 안 넣는다:
    ① 앞 단계(직접 구매)가 실패·결과 불명·확인 안 됨이었으면 건너뜀 — 상태가 어지러울 때 큰 요청을 얹지 않는다
    ② 요청 직전에 예치금을 다시 읽어, 계산한 값(시작 예치금 − 방금 산 것)과 다르면 건너뜀
    그 뒤 예약 요청 → settle_reservation 이 예약내역·예치금으로 판정."""
    if trouble:
        print("[lotto] 연금 예약 안 함: 이번 실행의 앞 단계에 실패·결과 불명·확인 안 됨이 있어요 (다음 실행이 다시 본다)")
        return {"skipped": "앞 단계 문제 — 이번 실행은 예약 안 함"}
    deposit = expected_deposit
    if not DRY:
        deposit = read_balance(client, capture, buying=False)
        # != : 1원이라도 다르면 안 넣는다 (못 읽은 것도 — None)
        if deposit != expected_deposit:
            seen = "못 읽었어" if deposit is None else won(deposit)
            alert("reserve-deposit", "연금 예약 미룸 — 예치금이 계산과 다름", f"예약 직전 예치금: 계산 {won(expected_deposit)} · 사이트 {seen}. "
                  f"{rounds[0]}-{rounds[-1]}회 예약은 이번 실행에 안 넣었어 (돈 안 나감) — 다음 실행이 다시 본다.")
            return {"error": "예약 직전 예치금 불일치"}
    assert_round_caps(have, {(PENSION, r): PENSION_TICKETS_PER_SET for r in rounds})   # 결제 직전
    reserved = reserve_pension(client, cur, rounds, deposit)
    if reserved.get("attempted"):
        reserved = settle_reservation(client, capture, rounds, reserved, reservations, deposit)
    return reserved


# ---------- check: 결과 알림 (🚨 없이 1통 · 첫 줄 = 제일 높은 등수) ----------
LOTTO_DRAW_URL = "https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do?srchLtEpsd={}"   # 로그인 없이 회차 당첨번호
TG_LIMIT = 4000                    # 텔레그램 한 통 4,096자 — 여유 두고 자른다
CLAIM_NOTE = "수령은 동행복권 당첨금 지급 안내 확인 (금액별로 받는 곳이 달라)"
PNL_NOTE = "손익은 세후 · 연금식 당첨금은 실제 받은 돈만 셈"

# ---- 세금 (세후 먼저, 세전 괄호) — 소득세+지방세 합산, 1장(1게임)당 당첨금 기준 ----
TAX_FREE_MAX = 2_000_000           # 200만원 이하 비과세 (소득세법 과세최저한 — 당첨금 기준, <= 라 200만원 정각도 비과세)
TAX_BRACKET = 300_000_000          # 3억원까지 22%(소득세 20%+지방세 2%), 넘는 부분은 33%(30%+3%)
TAX_LOW_PCT, TAX_HIGH_PCT = 20, 30  # 소득세율 — 지방세는 소득세의 10%
TICKET_COST_DEDUCT = 1_000         # 필요경비 = 당첨 복권 구입금 (1게임 1,000원) — 과세 대상에서 뺀다
ANNUITY_NET_PCT = 78               # 연금형(연금720+ 1·2등·보너스)은 매달 받는 금액에 22% → 78% 수령 (공식 안내 1등 월 546만원)
FIXED_LOTTO_PRIZE = {"4등": LOTTO_4TH_PRIZE, "5등": 5_000}   # 로또 4·5등은 고정 금액, 1-3등은 회차마다 다름(사이트 값)


def lotto_tax(gross: int) -> int:
    """1장 당첨금 → 세금(원, 원 미만 버림). 200만원 이하 0."""
    if gross <= TAX_FREE_MAX:
        return 0
    base = gross - TICKET_COST_DEDUCT
    income_tax = (min(base, TAX_BRACKET) * TAX_LOW_PCT + max(base - TAX_BRACKET, 0) * TAX_HIGH_PCT) // 100
    return income_tax + income_tax // 10


def after_tax(gross: int) -> int:
    return gross - lotto_tax(gross)


def annuity_net(monthly: int) -> int:
    return monthly * ANNUITY_NET_PCT // 100


def money_text(gross: int, net: int) -> str:
    """세후 먼저, 세전 괄호. 비과세면 '+5,000원 (비과세)'. 세후는 만원 아래를 버리고 '약'."""
    if gross == net:
        return f"{won(gross, signed=True)} (비과세)"
    return f"세후 약 {won(net // MAN * MAN)} (세전 {won(gross)})"


# 등수 → 등급. 알림 톤·첫 줄은 한 통에서 제일 높은 등급 하나로 정한다
TIER_ORDER = ("miss", "small", "win", "third", "second", "first")
TIER_OF_RANK = {
    LOTTO: {"1등": "first", "2등": "second", "3등": "third", "4등": "win", "5등": "small"},
    PENSION: {"1등": "first", "2등": "second", "보너스": "second", "3등": "third",
              "4등": "win", "5등": "win", "6등": "small", "7등": "small"},
}
LOTTO_RANKS = ("1등", "2등", "3등", "4등", "5등")
PENSION_RANKS = tuple(pension720.PRIZE)   # 1등 2등 보너스 3등 … 7등
GAME_NAME = {LOTTO: "로또", PENSION: "연금복권"}

# 등급별 톤. 첫 줄 = head ({name} '로또 1등' · {money} 세후/세전 글).
# 이모지 줄 = emoji × repeat, head 위 above 줄 · 아래 below 줄 (장식만). loud = 알림음 켬
TONE = {
    "miss":   {"head": "결과가 나왔어요. 이번 주는 낙첨이에요 😢 다음 주에 또!", "emoji": "", "repeat": 0, "above": 0, "below": 0, "loud": False},
    "small":  {"head": "{name} 당첨! {money}", "emoji": "", "repeat": 0, "above": 0, "below": 0, "loud": False},
    "win":    {"head": "{name} 당첨!! {money}", "emoji": "🎉🎊", "repeat": 4, "above": 1, "below": 0, "loud": False},
    "third":  {"head": "!!!! {name} 당첨!!!! {money}", "emoji": "🎉🎊🍀💰", "repeat": 4, "above": 2, "below": 1, "loud": True},
    "second": {"head": "!!!!!!!! {name} 당첨!!!!!!!! 대박!!!!!!!! {money}", "emoji": "🎉🎊💰🔥🍾", "repeat": 3,
               "above": 4, "below": 4, "loud": True},
    "first":  {"head": "!!!!!!!!!!!!! {name} 당첨!!!!!!!! 인생 역전!!!!!!!! {money}", "emoji": "🎉🎊💰💸🔥🍾🏆👑🌈✨",
               "repeat": 2, "above": 5, "below": 5, "loud": True},
}
CLAIM_FROM_TIER = "win"            # 4등(로또)·4-5등(연금)부터 수령 안내 한 줄


def tier_rank(tier: str) -> int:
    return TIER_ORDER.index(tier)


def lotto_numbers_of(text: str | None) -> list[list[int]]:
    """DB 로또 번호 '[A] 자동: 1 2 3 4 5 6' 줄들 → [[1, 2, 3, 4, 5, 6], …]."""
    games = []
    for line in (text or "").splitlines():
        if not line.startswith("["):
            continue
        nums = [int(n) for n in re.findall(r"\d+", line.split(":", 1)[-1])]
        if len(nums) == 6:
            games.append(sorted(nums))
    return games


def lotto_rank(nums: list[int], win: dict) -> str | None:
    """1등 6개 · 2등 5개+보너스 · 3등 5개 · 4등 4개 · 5등 3개."""
    hit = len(set(nums) & set(win["numbers"]))
    if hit == 6:
        return "1등"
    if hit == 5:
        return "2등" if win["bonus"] in nums else "3등"
    return {4: "4등", 3: "5등"}.get(hit)


def tier_from_lotto_prize(prize: int) -> str:
    """당첨번호를 못 받았을 때만 — 사이트 당첨금으로 대충 등급을 정한다 (4등 5만 · 5등 5천은 고정 금액)."""
    if prize <= 0:
        return "miss"
    if prize <= 5_000:
        return "small"
    if prize <= 50_000:
        return "win"
    if prize < 10_000_000:      # 3등은 보통 100-200만
        return "third"
    if prize < 500_000_000:     # 2등은 보통 수천만
        return "second"
    return "first"


def lotto_winning(round_no: int) -> dict | None:
    """로또 회차 당첨번호 {'numbers': [..6], 'bonus': n}. lotto_draw 표 먼저, 없으면 공개 API (dry-run 이 아니면 표에 쌓음)."""
    with db.conn() as c:
        row = c.execute("select * from lotto_draw where round = %s", (round_no,)).fetchone()
    if row:
        return {"numbers": [row[f"n{i}"] for i in range(1, 7)], "bonus": row["bonus"]}
    try:
        import requests  # noqa: PLC0415  (dhapi 의존성 — 늘 있음)
        resp = requests.get(LOTTO_DRAW_URL.format(round_no), timeout=pension720.TIMEOUT,
                            headers={"User-Agent": pension720.DESKTOP_UA, "Accept": "application/json"})
        rows = [x for x in ((resp.json() or {}).get("data") or {}).get("list") or [] if int(x.get("ltEpsd", -1)) == round_no]
        if not rows:
            return None
        x = rows[0]
        nums = sorted(int(x[f"tm{i}WnNo"]) for i in range(1, 7))
        bonus = int(x["bnsWnNo"])
    except Exception as e:  # noqa: BLE001  (번호 없이 사이트 결과로 보낸다)
        print(f"[lotto] 로또 {round_no}회 당첨번호 조회 실패: {e}")
        return None
    if not DRY:
        ymd = str(x.get("ltRflYmd") or "")
        with db.conn() as c:
            c.execute("insert or ignore into lotto_draw (round, draw_date, n1, n2, n3, n4, n5, n6, bonus, first_prize_each, "
                      "first_winners, sales_amount, source) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'dhlottery')",
                      (round_no, f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}" if len(ymd) == 8 else None, *nums, bonus,
                       to_int(x.get("rnk1WnAmt")), to_int(x.get("rnk1WnNope")), to_int(x.get("wholEpsdSumNtslAmt"))))
    return {"numbers": nums, "bonus": bonus}


def count_text(ranks: list[str], order: tuple[str, ...], unit: str) -> str:
    """['5등', '5등', '4등'] → '4등 1게임 · 5등 2게임'."""
    return " · ".join(f"{r} {ranks.count(r)}{unit}" for r in order if r in ranks)


def top_rank(game: str, ranks: list[str]) -> tuple[str, str | None]:
    """(등급, 제일 높은 등수). 당첨 없으면 ('miss', None)."""
    if not ranks:
        return "miss", None
    best = max(ranks, key=lambda r: tier_rank(TIER_OF_RANK[game][r]))
    return TIER_OF_RANK[game][best], best


def lotto_game_prizes(ranks: list[str], prize: int) -> list[int]:
    """한 주문의 당첨 게임별 세전 당첨금. 4·5등은 고정 금액, 남은 사이트 당첨금은 1-3등 게임에 똑같이 나눈다.
    맞지 않으면(사이트 금액이 고정 금액 합보다 작음 등) 주문 전체를 한 장으로 본다 — 세금이 많게 잡히는 쪽."""
    fixed = [FIXED_LOTTO_PRIZE[r] for r in ranks if r in FIXED_LOTTO_PRIZE]
    big = [r for r in ranks if r not in FIXED_LOTTO_PRIZE]
    rest = prize - sum(fixed)
    if rest < 0 or (rest > 0 and not big) or (big and rest == 0):
        return [prize] if prize else []
    return fixed + ([rest // len(big)] * len(big) if big else [])


def lotto_entry(round_no: int, rows: list[dict], win: dict | None) -> dict | None:
    """로또 한 회차 → 알림 재료. 등수는 내 번호 × 당첨번호로 직접, 당첨금(세전)은 사이트 값. 줄마다 prize_net(세후)을 채운다.
    사이트 당첨금이 0 인데 당첨이면: 4·5등뿐이면 고정 금액으로 채우고, 1-3등이 끼었거나 등수를 모르면 None (판정 보류 — 금액이 뜰 때까지 알림 안 보냄)."""
    ranks, gross, net = [], 0, 0
    for r in rows:
        games = lotto_numbers_of(r["numbers"])
        row_ranks = [x for x in (lotto_rank(g, win) for g in games) if x] if win and games else []
        row_prize = r["prize"] or 0
        # <= 0 : 사이트 당첨금이 아직 안 떴다 (금액 반영이 결과 문구보다 늦을 수 있음)
        if row_prize <= 0 and (row_ranks or "".join(str(r.get("result") or "").split()) == "당첨"):
            if not row_ranks or any(x not in FIXED_LOTTO_PRIZE for x in row_ranks):
                return None
            row_prize = sum(FIXED_LOTTO_PRIZE[x] for x in row_ranks)
            r["prize"] = row_prize
        r["prize_net"] = sum(after_tax(p) for p in lotto_game_prizes(row_ranks, row_prize))
        ranks += row_ranks
        gross += row_prize
        net += r["prize_net"]
    if win and any(lotto_numbers_of(r["numbers"]) for r in rows):
        tier, best = top_rank(LOTTO, ranks)
        rank_text = count_text(ranks, LOTTO_RANKS, "게임")
        paren = f"당첨번호 {', '.join(str(n) for n in win['numbers'])} + 보너스 {win['bonus']}"
    else:
        # 당첨번호·내 번호를 못 읽으면 사이트 당첨금으로만 (등수는 대충)
        tier, best = tier_from_lotto_prize(gross), None
        rank_text = "" if tier == "miss" else "당첨"
        paren = "당첨번호 못 받음 — 사이트 결과 기준"
    return {"game": LOTTO, "label": f"로또 {round_no}회", "best": best, "ranks": rank_text, "prize": gross, "net": net,
            "money": money_text(gross, net), "tier": tier, "paren": paren}


def pension_money(ranks: list[str]) -> tuple[int, str]:
    """연금 당첨 → (손익에 넣을 세후 금액, 글). 연금형은 글로만 '월 546만원 × 20년 (세전 월 700만원)' — 손익엔 **실제 받은 돈만** 넣는다.
    연금형(1·2등·보너스)은 아직 한 푼도 안 받았으니 손익에 0 (20년치 총액을 미리 벌었다고 적지 않는다). 일시금은 장마다 세금."""
    annuity = [(r, ranks.count(r)) for r in pension720.ANNUITY if r in ranks]
    lumps = [pension720.PRIZE[r] for r in ranks if r not in pension720.ANNUITY]
    lump_gross = sum(lumps)
    lump_net = sum(after_tax(p) for p in lumps)
    if not annuity:
        return lump_net, money_text(lump_gross, lump_net) if lumps else "0원"
    net_parts = [f"월 {won(annuity_net(pension720.ANNUITY[r][0] * c))} × {pension720.ANNUITY[r][1]}년" for r, c in annuity]
    gross_parts = [f"월 {won(pension720.ANNUITY[r][0] * c)}" for r, c in annuity]
    if lumps:
        net_parts.append(won(lump_net))
        gross_parts.append(won(lump_gross))
    return lump_net, f"{' + '.join(net_parts)} (세전 {' + '.join(gross_parts)})"


def expected_annuity_line(ranks: list[str]) -> str | None:
    """연금형 당첨이 있으면 '예상 연금' 한 줄 (손익과 따로 보여준다). 없으면 None."""
    parts = [f"월 {won(annuity_net(pension720.ANNUITY[r][0] * ranks.count(r)))} × {pension720.ANNUITY[r][1]}년"
             for r in pension720.ANNUITY if r in ranks]
    if not parts:
        return None
    return f"예상 연금(세후): {' + '.join(parts)} — 손익엔 받은 돈만 넣어서 아직 0원"


def pension_entry(round_no: int, rows: list[dict], win: dict) -> dict:
    """연금 한 회차 → 알림 재료. rows 의 result·prize(세전)는 judge_pension 이 채운 뒤. 줄마다 prize_net(세후)을 채운다."""
    all_ranks, all_tickets = [], []
    for r in rows:
        tickets = pension720.parse_tickets(r["numbers"])
        ranks = [x for x in (pension720.rank_of(g, n, win) for g, n in tickets) if x]
        r["prize_net"] = pension_money(ranks)[0]
        all_ranks += ranks
        all_tickets += tickets
    net, money = pension_money(all_ranks)
    tier, best = top_rank(PENSION, all_ranks)
    return {"game": PENSION, "label": f"연금복권 {round_no}회", "best": best,
            "ranks": count_text(all_ranks, PENSION_RANKS, "장"), "prize": sum(r["prize"] or 0 for r in rows), "net": net,
            "money": money, "tier": tier, "note": expected_annuity_line(all_ranks),
            "paren": f"당첨번호 {win['group']}조 {win['number']} · 내 번호 {pension720.compact_tickets(all_tickets)}"}


def render_result(entries: list[dict], year_pnl: int, total_pnl: int, extra: list[str] | None = None) -> tuple[str, bool]:
    """(알림 글, 알림음 켤까). 두괄식 — 첫 줄(이모지 장식 빼고)에 제일 높은 게임·등수·금액. 손익은 세후."""
    top = max(entries, key=lambda e: tier_rank(e["tier"]))
    tone = TONE[top["tier"]]
    name = f"{GAME_NAME[top['game']]} {top['best']}" if top["best"] else f"{GAME_NAME[top['game']]}"
    head = tone["head"].format(name=name, money=top["money"])
    lines = [f"{e['label']}: {e['ranks'] + ' ' + e['money'] if e['tier'] != 'miss' else '낙첨'} ({e['paren']})" for e in entries]
    emoji_line = tone["emoji"] * tone["repeat"]
    above, below = tone["above"], tone["below"]
    while True:
        out = [emoji_line] * above + [head] + [emoji_line] * below + lines
        out += [e["note"] for e in entries if e.get("note")]   # 예상 연금 (손익과 따로)
        if tier_rank(top["tier"]) >= tier_rank(CLAIM_FROM_TIER):
            out.append(CLAIM_NOTE)
        out += extra or []                                       # 연금 예약 N주 남음
        out.append(f"올해 {won(year_pnl, signed=True)} · 누적 {won(total_pnl, signed=True)} ({PNL_NOTE})")
        text = "\n".join(out)
        # 4,000자 넘으면 이모지 줄부터 줄인다 (정보 줄은 안 자름)
        if len(text) <= TG_LIMIT or (above <= 1 and below <= 1):
            return text[:TG_LIMIT], tone["loud"]
        above, below = max(above - 1, 1), max(below - 1, 1)


def pnl_lines(new_rows: list[dict], today: date) -> tuple[int, int]:
    """(올해 손익, 누적 손익) — 두 게임 합, 세후. new_rows = 이번에 판정한 줄 (DB 값보다 우선)."""
    by_no = {r["order_no"]: r for r in drawn_rows()}
    by_no.update({r["order_no"]: r for r in new_rows})
    rows = list(by_no.values())
    year = [r for r in rows if (r["draw_date"] or "").startswith(str(today.year))]
    return pnl(year), pnl(rows)


def reserve_left_line(now: datetime) -> str | None:
    """연금 예약이 1주 이하로 남았으면 한 줄 — 다음 구매 때 5주치를 다시 예약하니 예치금을 미리 채우라는 뜻.
    예약 기록이 아예 없으면(예약을 한 번도 안 함) None."""
    known = reserved_known()
    if not known:
        return None
    left = len([r for r in known if r > latest_drawn_pension(now)])
    # > 1 : 남은 예약이 2주 이상이면 조용히. 1주·0주면 알린다
    if left > 1:
        return None
    if not reserve_enabled():
        return f"연금 예약 {left}주 남음 — 자동 예약은 꺼져 있어서 그 뒤론 매주 한 세트씩 직접 사요"
    return f"연금 예약 {left}주 남음 — 다음 구매 때 {PENSION_RESERVE_ROUNDS}주치 {won(RESERVE_COST)} 다시 예약 (예치금 확인)"


def send_result(entries: list[dict], new_rows: list[dict], now: datetime) -> bool:
    """결과 한 통. 실제로 나갔으면 True (dry-run 은 False) — 부른 쪽은 True 일 때만 '보냄'으로 적는다."""
    extra = []
    if any(e["game"] == PENSION for e in entries):
        line = reserve_left_line(now)
        extra = [line] if line else []
    text, loud = render_result(entries, *pnl_lines(new_rows, now.date()), extra)
    # 3등 이상만 알림음을 확실히 켠다. 요란함은 글로만 — 1통
    delivered = notifier().send(text, silent=False if loud else None)
    if delivered or DRY:
        notifier().write_brief("lotto", [(SECTOR, "결과", next(l for l in text.splitlines() if not l.startswith("🎉")), text)])
    return bool(delivered)


def unnotified_by_round() -> dict[tuple[str, int], list[dict]]:
    """결과 핑을 아직 안 보낸 줄을 (게임, 회차)로 묶는다. 조회 창 밖으로 나간 로또 회차(stale_lotto_rounds)는 뺀다 — 더는 결과를 받을 길이 없다."""
    with db.conn() as c:
        rows = c.execute("select * from lotto_order where notified_at is null order by game, round, order_no").fetchall()
    by: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        by.setdefault((r["game"], r["round"]), []).append(r)
    stale = stale_lotto_rounds(by, now_kst().date())
    return {key: group for key, group in by.items() if key[1] not in stale or key[0] != LOTTO}


def stale_lotto_rounds(by: dict[tuple[str, int], list[dict]], today: date) -> set[int]:
    """결과 핑을 못 보낸 채 구매내역 조회 창(CHECK_LOOKBACK_DAYS)을 벗어난 로또 회차. 로또 결과는 사이트 구매내역에만 있어서
    창을 벗어나면 로그인해도 못 받는다 → 포기하고 그 회차 때문에 더 로그인하지 않는다. (연금은 DB 번호 + 공개 당첨번호로 판정해서 해당 없음)"""
    start = (today - timedelta(days=CHECK_LOOKBACK_DAYS)).isoformat()   # 조회 창의 첫날 (이 날 산 것까지는 조회된다)
    # < : 그 회차의 가장 늦은 구입일이 창 첫날보다 앞이면 창 밖. 구입일을 모르면 추첨일로
    return {round_no for (game, round_no), rows in by.items()
            if game == LOTTO and max((r["bought_on"] or r["draw_date"] or "") for r in rows) < start}


def abandon_stale_lotto(now: datetime) -> None:
    """창 밖으로 나간 로또 회차를 한 번 알리고 state 에 적는다 (회차당 1번). 건너뛰는 것 자체는 unnotified_by_round·login_reasons 가 DB 로 한다."""
    with db.conn() as c:
        rows = c.execute("select * from lotto_order where notified_at is null and game = %s", (LOTTO,)).fetchall()
    by: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        by.setdefault((r["game"], r["round"]), []).append(r)
    state = load_state()
    told = state.setdefault("abandoned", {})
    fresh = sorted(r for r in stale_lotto_rounds(by, now.date()) if str(r) not in told)
    if not fresh:
        return
    alert("abandoned", "로또 결과 확인 포기 — 직접 확인", f"{', '.join(f'{r}회' for r in fresh)}는 구입한 지 {CHECK_LOOKBACK_DAYS}일이 넘어 "
          "구매내역 조회에 안 잡혀요. 이 회차 때문에 더 로그인하지 않을게요 — 결과는 동행복권 구매내역에서 직접 봐 주세요 (손익엔 안 들어가요).")
    if DRY:
        return
    for r in fresh:
        told[str(r)] = stamp()
    save_state(state)


def buy_job_enabled() -> bool:
    """자동 구매가 켜져 있나 (config enabled). 못 읽으면 False — 꺼진 걸로 보고 '안 샀어요' 알림을 안 낸다."""
    try:
        return settings.load_config().get("enabled") is True
    except settings.SettingsError:
        return False


def warn_not_bought(now: datetime) -> None:
    """'이번 주 안 샀어요' — 방금 추첨이 끝난 회차에 DB 기준 한 장도 없으면 한 줄 (게임·회차당 1번). 자동 구매는 켜져 있는데 그 주를
    조용히 건너뛴 걸 알게. 자동 구매가 켜져 있을 때만 (꺼 둔 동안엔 안 사는 게 정상)."""
    if not buy_job_enabled():
        return
    state = load_state()
    told = state.setdefault("not_bought_told", {})
    latest = {game: latest_of(now) for game, latest_of in LATEST_DRAWN.items() if game in ENABLED_GAMES}
    # 처음 보는 게임은 지금 회차를 기준으로만 적는다 (알림 없음) — 자동 구매를 켜기 전 회차까지 '안 샀어요'라고 하지 않게
    first_seen = [g for g in latest if g not in told]
    # == 0 : 한 장이라도 있으면 산 주다 (덜 산 건 구매 쪽 경보가 따로 알린다)
    missed = [(g, r) for g, r in latest.items() if g in told and told[g] != r and db_tickets(g, r) == 0]
    if missed:
        alert("not-bought", "이번 주 안 샀어요", f"{' · '.join(f'{GAME_LABEL[g]} {r}회' for g, r in missed)} — 추첨이 끝났는데 구매 기록이 한 장도 없어요 "
              "(자동 구매는 켜져 있음). 예치금·로그인 멈춤(login_blocked)·Actions 실행 기록을 확인해 주세요.")
    if DRY or not (missed or first_seen):
        return
    for g in [*first_seen, *(g for g, _ in missed)]:
        told[g] = latest[g]
    save_state(state)


def mark_notified(game: str, round_no: int) -> None:
    with db.conn() as c:
        c.execute("update lotto_order set notified_at = %s where game = %s and round = %s and notified_at is null",
                  (now_kst().isoformat(timespec="seconds"), game, round_no))


def result_is_late(draw_day: str, today: date, late_days: int = RESULT_LATE_DAYS) -> bool:
    # >= : 토 추첨이면 월요일(2일 뒤)부터, 목 추첨이면 금요일(1일 뒤)부터 늦은 것
    return (today - date.fromisoformat(draw_day)).days >= late_days


def login_reasons(now: datetime) -> list[str]:
    """로그인할 이유들. 빈 목록이면 로그인 안 함.
    ① 로또: 추첨 끝난 회차인데 결과 핑을 아직 안 보낸 줄 (결과는 사이트 구매내역에만 있음)
    ② 두 게임 각각: 최근 추첨 회차가 DB 에 아예 없음 (구매 기록 누락일 수 있음 — 게임·회차당 1번만).
    연금 결과는 공개 당첨번호로 판정하니 그것만으론 로그인 안 한다."""
    reasons = []
    checked = load_state().get("check_missing", {})
    latest_lotto = latest_drawn_round(now)
    # <= : 아직 추첨 전인 이번 주 구매는 볼 게 없다.
    # 조회 창 밖으로 나간 회차는 unnotified_by_round 가 이미 뺐다 — 그 회차 때문에 로그인하지 않는다
    pending = sorted(r for g, r in unnotified_by_round() if g == LOTTO and r <= latest_lotto)
    if pending:
        reasons.append(f"로또 결과 핑 안 보낸 회차 {', '.join(str(r) for r in pending)}")
    with db.conn() as c:
        for game, latest_of in LATEST_DRAWN.items():
            latest = latest_of(now)
            has = c.execute("select 1 from lotto_order where game = %s and round = %s limit 1", (game, latest)).fetchone()
            if not has and checked.get(game) != latest:
                reasons.append(f"{GAME_LABEL[game]} {latest}회 DB 기록 없음 — 사이트 한 번 확인")
    return reasons


def remember_missing_checks(now: datetime) -> None:
    """최근 회차가 DB 에 없어서 사이트를 본 것을 게임별로 기록 — 그 회차엔 다시 안 본다."""
    state = load_state()
    checked = state.setdefault("check_missing", {})
    with db.conn() as c:
        for game, latest_of in LATEST_DRAWN.items():
            latest = latest_of(now)
            if not c.execute("select 1 from lotto_order where game = %s and round = %s limit 1", (game, latest)).fetchone():
                checked[game] = latest
    save_state(state)


def pension_due(now: datetime) -> list[int]:
    """추첨이 끝났는데 결과 핑을 안 보낸 연금 회차."""
    latest = latest_drawn_pension(now)
    return sorted(r for g, r in unnotified_by_round() if g == PENSION and r <= latest)


def judge_pension(rows: list[dict], win: dict) -> bool:
    """연금 줄마다 결과·당첨금을 채운다. 번호를 못 읽은 줄이 있으면 False (판정 보류)."""
    for r in rows:
        tickets = pension720.parse_tickets(r["numbers"])
        if not tickets:
            return False
        r["result"], r["prize"] = pension720.judge(tickets, win)
    return True


def notify_results(now: datetime) -> tuple[list[str], list[str]]:
    """추첨 끝났는데 결과 핑 안 보낸 회차를 모아 한 통. (보낸 것, 늦은 것) — 'pension720:342' 꼴.
    '보냄' 표시는 텔레그램이 실제로 받았을 때만 — 발송이 실패하면 다음 실행이 다시 보낸다."""
    today = now.date()
    winning: dict[int, dict] | None = None
    entries, new_rows, done, late, odd = [], [], [], [], []
    for (game, round_no), rows in sorted(unnotified_by_round().items(), key=lambda kv: (kv[0][0] != PENSION, kv[0][1])):
        tag = f"{game}:{round_no}"
        if game == PENSION:
            # > : 추첨 전 회차는 건너뛴다
            if round_no > latest_drawn_pension(now):
                continue
            if winning is None:
                try:
                    winning = pension720.fetch_winning()
                except Exception as e:  # noqa: BLE001
                    print(f"[lotto] 연금 당첨번호 조회 실패: {e}")
                    winning = {}
            win = winning.get(round_no)
            if not win or not judge_pension(rows, win):
                if result_is_late(rows[0]["draw_date"], today, PENSION_RESULT_LATE_DAYS):
                    late.append(tag)
                continue
            entry = pension_entry(round_no, rows, win)   # prize_net 채움
        else:
            # > : 아직 추첨 전인 회차(이번 주 구매)는 사이트 문구가 뭐든 결과로 안 본다
            if round_no > latest_drawn_round(now):
                continue
            states = {result_state(r["result"]) for r in rows}
            if "unknown" in states:
                word = next(r["result"] for r in rows if result_state(r["result"]) == "unknown")
                odd.append(f"{round_no}회 '{pension720.short_text(word, 20)}'")
                continue
            entry = lotto_entry(round_no, rows, lotto_winning(round_no)) if states == {"drawn"} else None   # prize_net 채움
            if entry is None:   # 아직 추첨 전 문구거나, 당첨인데 금액이 안 떠서 보류
                if result_is_late(rows[0]["draw_date"], today):
                    late.append(tag)
                continue
        entries.append(entry)
        if not DRY:
            save_results(rows)
        new_rows += rows
        done.append((game, round_no, tag))
    if odd:
        alert("result-word", "로또 결과 문구가 낯섦", f"추첨이 끝난 {', '.join(odd)} — 아는 문구({'·'.join(sorted(DRAWN_RESULTS))})가 아니라 결과를 안 보냈어. "
              "사이트 구매내역 확인 필요 (문구가 바뀌었으면 lotto.py DRAWN_RESULTS).")
    if not entries:
        return [], late
    delivered = send_result(entries, new_rows, now)
    if DRY:
        return [tag for _, _, tag in done], late   # dry-run: 보낼 회차만 알려주고 '보냄' 표시는 안 한다
    if not delivered:
        print("[lotto] 결과 핑 발송 실패 — '보냄' 표시 안 함 (다음 실행이 다시 보냄)")
        return [], late
    for game, round_no, _ in done:
        mark_notified(game, round_no)
    return [tag for _, _, tag in done], late


def cmd_check() -> None:
    now = now_kst()
    warn_pending_reserve()
    abandon_stale_lotto(now)
    reasons = login_reasons(now)
    due = pension_due(now)
    if not reasons and not due:
        emit(skipped="확인할 것 없음 — 로그인 안 함")
        return
    if reasons:
        print(f"[lotto] 로그인 이유: {' · '.join(reasons)}")
    stop = login_stop_reason(now, buying=False) if reasons else None
    if stop:
        print(f"[lotto] {stop} (연금 결과는 로그인 없이 계속)")
    elif reasons:
        jitter()
        session = login()
        if session is None:
            emit(ok=False, error="로그인 실패 (경보 보냄)")
            return
        client, capture = session
        try:
            orders = fetch_orders(client, capture, CHECK_LOOKBACK_DAYS)
        except Exception as e:  # noqa: BLE001
            alert("check-list", "결과 조회 실패", f"구매내역(결과)을 못 읽었어: {pension720.short_text(str(e), 120)}")
            emit(ok=False, error="구매내역 조회 실패")
            return
        if DRY:
            print(f"[DRY] 최근 {CHECK_LOOKBACK_DAYS}일 주문 {len(orders)}건 (DB 안 씀)")
            for o in orders:
                if o.get("unknown") or o.get("other"):
                    print(f"  {'다른 복권' if o.get('other') else '못 읽은 줄'}: {row_label(o)}")
                    continue
                print(f"  {GAME_LABEL[o['game']]} {o['round']}회 {o['games']}{GAME_UNIT[o['game']]} 결과={o['result']!r} 당첨금={o['prize']}")
        else:
            upsert_orders(orders)
            remember_missing_checks(now)
        unreadable = unknown_rows(orders)
        if unreadable:
            # buy 와 같게 알린다 — 못 읽은 줄이 있으면 이상징후 감지를 못 하는데, 조용히 건너뛰면 감지가 꺼진 걸 아무도 모른다
            alert("check-unknown", "구매내역에 못 읽은 줄 — 이상징후 감지 건너뜀",
                  f"구매내역에 못 읽은 줄 {len(unreadable)}개 ({', '.join(row_label(o) for o in unreadable)}). "
                  "이번 실행은 계정 이상징후를 못 봤어 — 동행복권 구매내역 확인해 주세요.")
        else:
            detect_anomalies(orders, read_balance(client, capture, buying=False), now)   # 알림만 — check 는 사는 게 없다
    warn_not_bought(now)
    sent, late = notify_results(now)
    if late:
        alert("result-late", "결과 못 가져옴", f"{', '.join(late)} 추첨 끝났는데 결과가 아직 비어 있어요. 사이트 확인 필요.")
    emit(ok=True, dry=DRY or None, notified=sent, late=late)


def persist(command: str) -> None:
    """Actions: 실행 끝에 DB·설정까지 state 브랜치에 기록. 실패해도 이미 결제 직전 기록은 남아 있다 — 경보만."""
    st = backend().state
    if isinstance(st, GitBranchStore) and not DRY:
        db.close()   # DB 파일을 닫아서 완전한 상태로 커밋
        try:
            st.persist(f"{command} {stamp()}")
        except StateError as e:
            alert("persist", "상태 기록 실패", f"구매 기록 DB 를 state 브랜치에 못 남겼어요 — 다음 결과 알림이 한 번 더 갈 수 있어요: {e}")


def main() -> None:
    global DRY, NO_JITTER
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="로또 6/45 + 연금복권720+ 주간 자동 구매·결과 확인 (안전장치 먼저)")
    ap.add_argument("command", choices=["buy", "check"])
    ap.add_argument("--dry-run", action="store_true", help="사지도 예약하지도 보내지도 않고 할 일만 출력 (연금은 번호 배정·예약 계획까지만)")
    ap.add_argument("--no-jitter", action="store_true", help="로그인 전 랜덤 대기 안 함 (손 실행용)")
    args = ap.parse_args()
    DRY = args.dry_run
    NO_JITTER = args.no_jitter
    channel.DRY_RUN = DRY
    try:
        settings.mask_secrets()
        backend()
        cfg = settings.load_config()
        apply_config(cfg)
    except (StateError, CapError, settings.SettingsError) as e:   # 설정·저장소가 이상하면 아무것도 안 하고 끝 (안 삼)
        emit(ok=False, error=str(e))
        return
    if args.command == "buy" and not cfg["enabled"] and not DRY:
        emit(skipped="자동 구매가 꺼져 있음 — Actions 의 '① 설정' 을 실행하면 켜져요")
        return
    db.init()
    if not acquire_lock():
        # buy 와 check 가 겹치거나 같은 명령이 두 번 떴다 — 기다리지 않고 조용히 끝낸다 (먼저 뜬 쪽이 일을 한다)
        emit(skipped="다른 lotto 실행이 도는 중 — 이번 실행은 건너뜀")
        return
    try:
        cmd_buy() if args.command == "buy" else cmd_check()
    except Exception as e:  # noqa: BLE001  (예상 못 한 실패 — 경보 + 실패로 끝)
        detail = f"{type(e).__name__}: {pension720.short_text(str(e), 200)}"
        alert(f"crash-{args.command}", f"{args.command} 오류", detail)
        emit(ok=False, error=detail)
    finally:
        persist(args.command)


if __name__ == "__main__":
    main()

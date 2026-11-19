"""lotto.py · pension720.py 오프라인 테스트 — 사이트에 안 붙는다 (로그인·구매·예약·알림 전부 가짜).

    python tests/test_lotto.py
임시 상태 폴더(LOTTO_STATE_DIR)·임시 DB(LOTTO_DB)를 쓴다. 실제 상태 폴더는 안 건드린다.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lotto-test-"))
os.environ["LOTTO_DB"] = str(TMP / "test.sqlite")
os.environ["LOTTO_STATE_DIR"] = str(TMP / "state")
for _name in ("LOTTO_JITTER", "LOTTO_BACKEND", "LOTTO_SECRETS", "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY"):
    os.environ.pop(_name, None)
HERE = Path(__file__).resolve().parent
LIB = HERE.parent / "lotto_safe"
sys.path.insert(0, str(LIB))

import lotto  # noqa: E402
import pension720  # noqa: E402
import settings  # noqa: E402

# 테스트 기준 설정: 로또 3게임 + 연금(예약) — 원본 테스트가 이 한도로 짜여 있다
BASE_CONFIG = {"enabled": True, "games": [lotto.LOTTO, lotto.PENSION], "lotto_games": 3, "pension_mode": "reserve"}
lotto.apply_config(BASE_CONFIG)

KST = lotto.KST
L, P = lotto.LOTTO, lotto.PENSION
MON = datetime(2026, 11, 16, 10, 0, tzinfo=KST)   # 월요일 — 로또 이번 주 회차 · 연금 342회(11-19 추첨)
LOTTO_ROUND = lotto.sales_round(MON.date())
PENSION_ROUND = 342
NEXT5 = [343, 344, 345, 346, 347]                 # 342회가 판매 중일 때 예약 한 번이 덮는 회차
pings: list = []      # 🚨 경보 (channel.notify) — [(섹터, 이름, 사유, 상세)]
sends: list = []      # 구매·예약·결과 알림 (channel.send) — (글, silent)
briefs: list = []
calls: list = []
site: list = []       # 가짜 사이트 구매내역 (주문 dict)
rsv_rows: list = []   # 가짜 사이트 예약내역 (pension720.parse_reservations 가 돌려주는 모양)
wallet = {"won": 0}   # 가짜 구매가능 예치금 — 가짜 결제·예약이 실제처럼 깎는다 (예약 전후 예치금 대조용)
PATCHED = ("fetch_orders", "fetch_reservations", "read_balance", "buy_pension", "buy_lotto", "reserve_pension", "login",
           "now_kst", "lotto_winning", "read_user", "RawLotteryClient", "site_lotto_round", "buy_job_enabled")
ORIGINAL = {name: getattr(lotto, name) for name in PATCHED}
ORIGINAL_FETCH_WINNING = pension720.fetch_winning
ORIGINAL_GAME = pension720.PensionGame
CAPS = {n: getattr(lotto, n) for n in ("MAX_GAMES_PER_ROUND", "PENSION_MAX_TICKETS", "WEEKLY_CAP_WON", "PENSION_RESERVE_ROUNDS")}
ROUND_CAP = dict(lotto.ROUND_CAP)
REQUEST_MAX_ROUNDS = dict(lotto.REQUEST_MAX_ROUNDS)


def rsv_row(round_no: int, qty: int = 5, status: str = "1") -> dict:
    return {"round": round_no, "qty": qty, "status": status, "cancelled": status in pension720.RESERVE_CANCELLED_CODES}


def release_lock() -> None:
    if lotto._lock_handle is not None:
        lotto._lock_handle.close()
        lotto._lock_handle = None


def write_config(**over) -> None:
    settings.save_config({**BASE_CONFIG, "telegram": False, **over})


def reset(now: datetime = MON, reserved: bool = True) -> None:
    """reserved=True (기본): 다음 5회차 예약이 이미 걸려 있는 평소 주 — state 에도 사이트 예약내역에도.
    reserved=False: 예약이 하나도 없는 주 (예약 테스트용)."""
    for name, fn in ORIGINAL.items():
        setattr(lotto, name, fn)
    pension720.fetch_winning = ORIGINAL_FETCH_WINNING
    pension720.PensionGame = ORIGINAL_GAME
    release_lock()
    lotto._BACKEND = None                                    # 어댑터는 매 테스트 기본(local)부터 — 클라우드 테스트가 바꿔 끼운 걸 지운다
    for name in (lotto.BACKEND_ENV, "DH_ID", "DH_PW", "LOTTO_SECRETS"):
        os.environ.pop(name, None)
    lotto.STATE = TMP / "state.json"
    lotto.LOCK = TMP / "lotto.lock"
    lotto.STATE.unlink(missing_ok=True)
    lotto.PENSION_RESERVE_ENABLED = True                     # 예약 흐름 테스트용 — 코드 기본값(꺼짐)은 test_reserve_switch_default_off 가 본다
    os.environ.pop("LOTTO_JITTER", None)
    lotto.site_lotto_round = lambda: lotto.sales_round(lotto.now_kst().date())   # 사이트 회차 = 계산 회차 (공개 API 안 부름)
    lotto.buy_job_enabled = lambda: False                    # '이번 주 안 샀어' 알림은 그 테스트에서만 켠다
    wallet["won"] = 0
    lotto.DRY = False
    lotto.NO_JITTER = False
    lotto.channel.DRY_RUN = False
    lotto.now_kst = lambda: now
    with lotto.db.conn() as c:
        c.execute("delete from lotto_order")
    lotto.ENABLED_GAMES = {L, P}
    write_config()
    for name, value in CAPS.items():
        setattr(lotto, name, value)
    lotto.ROUND_CAP = dict(ROUND_CAP)
    lotto.REQUEST_MAX_ROUNDS = dict(REQUEST_MAX_ROUNDS)
    for box in (pings, sends, briefs, calls, site, rsv_rows):
        box.clear()
    FakeClient.fail = None
    FakeClient.visible = True
    FakeGame.mode = "ok"
    lotto.channel.notify = lambda source, items: pings.append(items) or True
    lotto.channel.send = lambda text, silent=None: sends.append((text, silent)) or True
    lotto.channel.write_brief = lambda source, items: briefs.append(items)
    lotto.lotto_winning = lambda round_no: None
    lotto.login = fake_login
    if reserved and now.weekday() <= lotto.PENSION_LAST_BUY_WEEKDAY:
        cur = lotto.pension_week_round(now.date())
        future = lotto.reserve_rounds_after(cur)
        lotto.remember_reserved_seen(set(future))
        rsv_rows.extend(rsv_row(r) for r in future)


class FakeClient:
    """가짜 dhapi 클라이언트 — 로또 결제(execBuy) 자리.
    fail: None · 'reject'(서버가 거절 — 안 사짐) · 'after'(사졌는데 응답을 못 읽음) · 'lost'(응답을 못 읽음 — 안 사짐)."""
    _session = None
    fail: str | None = None
    visible = True        # 산 게 구매내역에 보이나

    def __init__(self, capture):
        self.capture = capture

    def buy_lotto645(self, tickets):
        calls.append(("execBuy", len(tickets)))
        bought = FakeClient.fail in (None, "after")
        if bought:
            wallet["won"] -= 1000 * len(tickets)
        if bought and FakeClient.visible:
            site.append(lotto_order(lotto.sales_round(lotto.now_kst().date()), len(tickets), no=f"NEW{len(site)}"))
        if FakeClient.fail == "reject":
            raise RuntimeError("❗ 로또6/45 구매에 실패했습니다. (사유: 구매한도를 초과했습니다)")   # 서버가 사유를 대며 거절
        if FakeClient.fail:
            raise RuntimeError("❗ 로또6/45 구매에 실패했습니다. (사유: 알 수 없는 오류)")          # dhapi 가 응답을 못 읽음
        self.capture.bought_slots = [{"slot": "A", "mode": "자동", "numbers": ["03", "11", "17", "25", "38", "42"]}] * len(tickets)


def fake_login():
    calls.append("login")
    capture = lotto.Capture()
    return FakeClient(capture), capture


def lotto_order(round_no: int, games: int, no: str = "L1") -> dict:
    return {"order_no": no, "game": L, "round": round_no, "bought_on": "2026-11-16", "games": games,
            "numbers": "\n".join(f"[{'ABCDE'[i]}] 자동: 1 2 3 4 5 6" for i in range(games)), "result": "미추첨", "prize": 0,
            "draw_date": "2026-11-21"}


def pension_order(round_no: int, tickets: int = 5, number: str = "123456", no: str = "LP72-P1") -> dict:
    return {"order_no": no, "game": P, "round": round_no, "bought_on": "2026-11-16", "games": tickets,
            "numbers": pension720.format_tickets([(g, number) for g in range(1, tickets + 1)]), "result": "",
            "prize": 0, "draw_date": pension720.draw_date_of(round_no).isoformat()}


def tuples() -> list[tuple]:
    return [c for c in calls if isinstance(c, tuple)]


def paid(kind: str) -> list[tuple]:
    """돈 나가는 호출만: 'pension'(이번 회차 직접) · 'execBuy'(로또 결제) · 'reserve'(예약)."""
    return [c for c in tuples() if c[0] == kind]


def run_buy(site_orders, balance: int = 40000, fetch_fails: bool = False, refetch_fails: bool = False,
            rsv_fails: bool = False, real_reserve: bool = False, ack: bool = True,
            balance_reads_ok: int | None = None, drift: int = 0) -> None:
    """가짜 사이트로 cmd_buy 한 번. 로또는 진짜 buy_lotto + FakeClient, 연금 직접 구매·예약은 가짜 (real_reserve 면 진짜 reserve_pension).
    ack=True (기본): 처음부터 사이트에 있던 주문은 '이미 알린 것'으로 해 둔다 — 이상징후 감지가 아니라 구매 흐름을 보는 테스트라서.
    balance_reads_ok=N: 예치금 조회가 N번까지만 되고 그 뒤론 못 읽음(None). drift: 첫 조회 뒤 예치금이 그만큼 몰래 줄어든다."""
    site[:] = list(site_orders)
    wallet["won"] = balance
    if ack and site:
        state = lotto.load_state()
        for found in lotto.order_anomalies(site):
            state.setdefault("anomaly_seen", {})[found["key"]] = "test"
        lotto.save_state(state)

    def fetch(client, capture, days):
        calls.append("fetch")
        if fetch_fails or (refetch_fails and calls.count("fetch") > 1):
            raise RuntimeError("down")
        return list(site)

    def reservations(client):
        calls.append("rsv-list")
        if rsv_fails:
            raise pension720.PensionError("예약 내역 조회 실패 (HTTP 500)")
        return list(rsv_rows)

    def buy_pension(client, r):
        calls.append(("pension", r))
        if lotto.DRY:
            return {"dry": True, "number": "123456"}
        lotto.mark_pension_attempt(r)
        wallet["won"] -= 5000
        site.append(pension_order(r, no=f"LP72-NEW{len(site)}"))
        return {"number": "123456", "tickets": 5, "attempted": True}

    def reserve_pension(client, cur, rounds, deposit):
        calls.append(("reserve", cur, tuple(rounds)))
        if lotto.DRY:
            return {"dry": True, "rounds": rounds}
        lotto.mark_reserve_attempt(rounds, deposit)
        wallet["won"] -= 5000 * len(rounds)
        rsv_rows.extend(rsv_row(r) for r in rounds)
        return {"rounds": rounds, "attempted": True}

    def buy_lotto(client, capture, r, n):
        calls.append(("lotto", r, n))
        return ORIGINAL["buy_lotto"](client, capture, r, n)

    lotto.fetch_orders = fetch
    lotto.fetch_reservations = reservations
    def read_balance(client, capture, buying=True):
        calls.append("balance")
        # > : N번째까지는 읽힌다
        if balance_reads_ok is not None and calls.count("balance") > balance_reads_ok:
            return None
        seen = wallet["won"]
        if calls.count("balance") == 1:
            wallet["won"] -= drift
        return seen

    lotto.read_balance = read_balance
    lotto.buy_pension = buy_pension
    lotto.buy_lotto = buy_lotto
    if not real_reserve:
        lotto.reserve_pension = reserve_pension
    lotto.cmd_buy()


class Out(io.StringIO):
    def reconfigure(self, **kw):   # main() 이 sys.stdout.reconfigure 를 부른다
        pass


def captured(fn, *a, **kw) -> tuple[str, dict]:
    """(찍힌 글 전부, 마지막 줄 JSON) — 실행 결과 판정 줄."""
    out = Out()
    with contextlib.redirect_stdout(out):
        fn(*a, **kw)
    text = out.getvalue()
    return text, json.loads(text.strip().splitlines()[-1])


def captured_text(fn, *a, **kw) -> tuple[str, object]:
    """(찍힌 글 전부, 돌려준 값) — 마지막 줄이 JSON 이 아닌 것용."""
    out = Out()
    with contextlib.redirect_stdout(out):
        value = fn(*a, **kw)
    return out.getvalue(), value


def raises(kind, fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except kind:
        return True
    return False


# ---------- 판매 시간 · 회차 ----------
def test_pension_windows():
    at = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=KST)  # noqa: E731  16=월 … 19=목 20=금
    assert lotto.pension_window_closed(at(16, 10, 0)) is None
    assert lotto.pension_window_closed(at(19, 16, 29)) is None
    assert lotto.pension_window_closed(at(19, 16, 30)) is not None       # 16:30 정각부터 막음
    assert lotto.pension_window_closed(at(20, 10, 0)) is not None        # 금
    assert lotto.pension_window_closed(at(22, 10, 0)) is not None        # 일
    assert lotto.pension_week_round(at(16, 10, 0).date()) == 342
    assert lotto.pension_week_round(at(19, 10, 0).date()) == 342
    assert lotto.pension_week_round(datetime(2026, 11, 23).date()) == 343
    assert lotto.latest_drawn_pension(at(18, 10, 0)) == 341
    assert lotto.latest_drawn_pension(at(19, 19, 59)) == 341
    assert lotto.latest_drawn_pension(at(19, 20, 0)) == 342              # 20:00 정각부터 이번 목
    assert lotto.latest_drawn_pension(at(21, 10, 0)) == 342
    assert pension720.round_of_draw(datetime(2026, 11, 12).date()) == 341  # 사이트 실값
    rounds, _ = lotto.open_rounds(at(20, 10, 0))
    assert set(rounds) == {L}                                              # 금요일은 로또만


# ---------- 돈 나누기 · 회차 상한 ----------
def test_split_budget():
    assert lotto.split_budget(19000, 3, 1) == (1, 3, 0)
    assert lotto.split_budget(8000, 3, 1) == (1, 3, 0)       # 딱 맞으면 다 삼
    assert lotto.split_budget(7000, 3, 1) == (1, 2, 1000)
    assert lotto.split_budget(5000, 3, 1) == (1, 0, 3000)
    assert lotto.split_budget(4999, 3, 1) == (0, 3, 5000)    # 연금 못 사면 로또라도
    assert lotto.split_budget(0, 3, 1) == (0, 0, 8000)
    assert lotto.split_budget(-500, 3, 1) == (0, 0, 8000)
    assert lotto.split_budget(9000, 1, 0) == (0, 1, 0)


def test_cap_per_round():
    reset()
    cap = lotto.assert_round_caps
    empty = {(L, 1251): 0, **{(P, r): 0 for r in [342, *NEXT5]}}
    cap(empty, {(L, 1251): 3})                                              # 로또 한 회차 3게임 = 딱 상한
    cap(empty, {(P, 342): 5})                                               # 연금 한 회차 1세트
    cap(empty, {(P, r): 5 for r in NEXT5})                                  # 예약 5회차 × 5장 = 25,000원 허용
    assert lotto.RESERVE_COST == 25000 and lotto.SET_COST == 5000
    refused = [
        (empty, {(L, 1251): 4}),                                            # 로또 4게임
        (empty, {(P, 342): 10}),                                            # 한 회차에 2세트
        ({**empty, (P, 342): 5}, {(P, 342): 5}),                            # 이미 1세트 있는 회차에 또 1세트
        ({**empty, (P, 345): 5}, {(P, r): 5 for r in NEXT5}),               # 예약 5회차 중 하나가 이미 있음
        ({**empty, (P, 344): 1}, {(P, r): 5 for r in NEXT5}),               # 한 장만 있어도 세트는 못 얹음
        ({**empty, (P, 348): 0}, {(P, r): 5 for r in [*NEXT5, 348]}),       # 6회차 예약
        ({**empty, (L, 1251): 3}, {(L, 1251): 1}),                          # 로또 이미 3게임
        (empty, {(L, 1251): 0}), (empty, {(L, 1251): -1}),                  # 0·음수 구매
        (empty, {(L, 1251): 3, (P, 342): 5}),                               # 한 요청에 두 게임
        (empty, {(P, 399): 5}),                                             # 이미 있는 양을 안 센 회차
        (empty, {}),
    ]
    for have, add in refused:
        assert raises(lotto.CapError, cap, have, add), add


def test_round_have_takes_max_of_site_reservation_state():
    reset(reserved=False)
    have = lotto.round_have([lotto_order(1251, 1), pension_order(342, 5)], [rsv_row(344), rsv_row(345, status="3")], 1251, [342, *NEXT5])
    assert have == {(L, 1251): 1, (P, 342): 5, (P, 343): 0, (P, 344): 5, (P, 345): 0, (P, 346): 0, (P, 347): 0}, have   # 취소(3)는 안 셈
    lotto.mark_lotto_attempt(1251, 3)                # 사이트엔 1게임인데 결제 시도는 3게임 → 큰 값
    lotto.mark_pension_attempt(343)
    lotto.mark_reserve_attempt([346, 347])
    have = lotto.round_have([lotto_order(1251, 1)], [], 1251, [342, *NEXT5])
    assert have[(L, 1251)] == 3 and have[(P, 343)] == 5 and have[(P, 346)] == 5 and have[(P, 347)] == 5 and have[(P, 342)] == 0, have
    lotto.set_reserve_result([346, 347], "failed")   # 실패로 판정된 예약은 '있음'이 아니다
    assert lotto.round_have([], [], None, [346])[(P, 346)] == 0
    assert raises(lotto.CapError, lotto.round_have, [lotto.unknown_order({"ltGdsNm": "로또6/45"})], [], 1251, [])


def test_cap_constants_stop_at_start():
    for name, value in (("MAX_GAMES_PER_ROUND", 4), ("MAX_GAMES_PER_ROUND", 2), ("PENSION_MAX_TICKETS", 10), ("WEEKLY_CAP_WON", 7000),
                        ("PENSION_RESERVE_ROUNDS", 6), ("PENSION_RESERVE_ROUNDS", 0)):
        reset()
        setattr(lotto, name, value)
        assert raises(lotto.CapError, lotto.validate_caps), name
        assert raises(lotto.CapError, lotto.cmd_buy), name
        assert "login" not in calls, (name, calls)
    reset()
    lotto.ROUND_CAP[P] = 10                                    # 표만 몰래 올려도 멈춘다
    assert raises(lotto.CapError, lotto.validate_caps)
    reset()
    lotto.validate_caps()
    assert (lotto.MAX_GAMES_PER_ROUND + lotto.PENSION_MAX_TICKETS) * lotto.GAME_PRICE == lotto.WEEKLY_CAP_WON == 8000
    assert not hasattr(lotto, "HARD_WEEKLY_CAP_WON")           # 금액 상한 상수는 하나


# ---------- buy 흐름 ----------
def test_db_complete_skips_login():
    reset()
    lotto.upsert_orders([lotto_order(LOTTO_ROUND, 3), pension_order(PENSION_ROUND)])
    run_buy([])
    assert "login" not in calls, calls


def test_attempt_marker_skips_login():
    reset()
    lotto.upsert_orders([lotto_order(LOTTO_ROUND, 3)])
    lotto.mark_pension_attempt(PENSION_ROUND)
    run_buy([])
    assert "login" not in calls, calls


def test_site_shows_pension_bought():
    reset()
    run_buy([pension_order(PENSION_ROUND, 5), lotto_order(LOTTO_ROUND, 1)])
    assert not paid("pension"), calls
    assert ("lotto", LOTTO_ROUND, 2) in calls and ("execBuy", 2) in calls, calls


def test_site_shows_partial_pension():
    reset()
    run_buy([pension_order(PENSION_ROUND, 1)])                 # 손으로 1장 산 주 → 세트(5장)는 상한 넘음
    assert not paid("pension"), calls


def test_list_fetch_fails_no_buy():
    reset()
    _, last = captured(run_buy, [], fetch_fails=True)
    assert not tuples() and "balance" not in calls, calls
    assert pings and "구매내역 조회 실패" in pings[-1][0][2]
    assert last["ok"] is False


def test_buys_both_with_one_login():
    reset()
    run_buy([])
    assert calls.count("login") == 1
    assert ("pension", PENSION_ROUND) in calls and ("execBuy", 3) in calls and not paid("reserve"), calls
    state = lotto.load_state()
    assert state["lotto_attempts"][str(LOTTO_ROUND)]["games"] == 3 and str(PENSION_ROUND) in state["pension_attempts"], state


def test_short_deposit_prefers_pension_and_stops_logins_today():
    reset()
    run_buy([], balance=6000)
    assert ("pension", PENSION_ROUND) in calls and ("execBuy", 1) in calls, calls
    assert any("예치금 부족" in p[0][2] for p in pings)
    assert lotto.load_state()["deposit_short_day"] == "2026-11-16"
    calls.clear()
    _, last = captured(run_buy, site)                          # 같은 날 다음 실행 — 로또 2게임이 남았어도 로그인 안 함
    assert "login" not in calls and "skipped" in last, (calls, last)
    lotto.now_kst = lambda: datetime(2026, 11, 17, 10, 0, tzinfo=KST)   # 다음 날은 다시 본다
    run_buy(site)
    assert "login" in calls and ("execBuy", 2) in calls, calls


def test_friday_lotto_only_db_complete():
    fri = datetime(2026, 11, 20, 10, 0, tzinfo=KST)
    reset(fri)
    lotto.upsert_orders([lotto_order(lotto.sales_round(fri.date()), 3)])
    run_buy([])                                                 # 연금 기록·예약이 없어도 금요일엔 로그인 안 함
    assert "login" not in calls, calls


def test_lotto_state_attempt_counts_with_site():
    reset()
    lotto.mark_lotto_attempt(LOTTO_ROUND, 2)                    # 결제 요청은 2게임 보냈는데 사이트엔 안 보이는 상태
    run_buy([pension_order(PENSION_ROUND)])
    assert paid("execBuy") == [("execBuy", 1)], calls            # max(사이트 0, 시도 2) = 2 → 1게임만
    reset()
    lotto.mark_lotto_attempt(LOTTO_ROUND, 3)
    lotto.upsert_orders([pension_order(PENSION_ROUND)])
    run_buy([])
    assert "login" not in calls, calls                           # 시도 3게임이면 로그인도 안 함


# ---------- 잠금 · state ----------
def test_lock_one_process_at_a_time():
    reset()
    assert lotto.acquire_lock() is True
    code = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import lotto; "
            "lotto.LOCK = Path(sys.argv[2]); print(lotto.acquire_lock())")
    other = lambda: subprocess.run([sys.executable, "-c", code, str(LIB), str(lotto.LOCK)],  # noqa: E731
                                   capture_output=True, text=True, timeout=60).stdout.strip()
    assert other() == "False"                                   # 다른 프로세스는 못 잡는다
    release_lock()
    assert other() == "True"                                    # 놓으면(프로세스가 끝나면) 잡힌다


def test_two_buys_second_skips_quietly():
    reset()
    lotto.LOCK.parent.mkdir(parents=True, exist_ok=True)
    held = open(lotto.LOCK, "a+b")                              # 먼저 뜬 buy 가 잠금을 쥔 상태 흉내
    if os.name == "nt":
        import msvcrt
        held.seek(0)
        msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    argv = sys.argv
    try:
        for command in ("buy", "check"):                        # buy·check 공용 잠금
            sys.argv = ["lotto.py", command, "--no-jitter"]
            _, last = captured(lotto.main)
            assert "skipped" in last and "login" not in calls and not pings and not sends, (command, last, calls)
    finally:
        sys.argv = argv
        held.close()
    assert not lotto.STATE.exists() or "lotto_attempts" not in lotto.load_state()


def test_corrupt_state_never_buys():
    reset()
    lotto.STATE.write_text('{"pension_attempts": {"342": "x"', encoding="utf-8")   # 쓰다 만 파일
    assert raises(lotto.StateError, lotto.load_state)
    assert raises(lotto.StateError, lotto.pension_attempted, 342)
    assert raises(lotto.StateError, run_buy, [])
    assert "login" not in calls and not tuples(), calls
    lotto.alert("x", "사유", "상세")                            # state 가 깨져도 경보는 나간다
    assert pings
    lotto.STATE.write_text("[1, 2]", encoding="utf-8")          # JSON 이지만 모양이 다름
    assert raises(lotto.StateError, lotto.load_state)
    argv = sys.argv
    try:                                                        # main 을 거치면: 경보 + ok false, 구매 없음
        sys.argv = ["lotto.py", "buy", "--no-jitter"]
        _, last = captured(lotto.main)
    finally:
        sys.argv = argv
    assert last["ok"] is False and "StateError" in last["error"] and "login" not in calls, last


def test_state_write_is_atomic_and_marks_refuse_twice():
    reset(reserved=False)
    lotto.mark_pension_attempt(342)
    assert not lotto.STATE.with_name("state.json.tmp").exists() and json.loads(lotto.STATE.read_text(encoding="utf-8"))
    assert raises(lotto.StateError, lotto.mark_pension_attempt, 342)       # 같은 회차 두 번째 결제 시도
    lotto.mark_lotto_attempt(1251, 2)
    lotto.mark_lotto_attempt(1251, 1)                                      # 합 3 = 딱 상한
    assert raises(lotto.StateError, lotto.mark_lotto_attempt, 1251, 1)     # 합 4
    assert raises(lotto.StateError, lotto.mark_lotto_attempt, 1252, 0)
    assert lotto.lotto_attempted_games(1251) == 3
    lotto.mark_reserve_attempt(NEXT5)
    assert raises(lotto.StateError, lotto.mark_reserve_attempt, [347, 348])   # 한 회차라도 겹치면
    assert raises(lotto.StateError, lotto.mark_reserve_attempt, [342])        # 직접 결제 시도한 회차도
    assert raises(lotto.StateError, lotto.mark_reserve_attempt, [])
    real_replace = os.replace
    try:                                                                   # 바꿔치기가 실패하면 원래 파일 그대로 + 예외
        def boom(a, b):
            raise OSError("disk")
        os.replace = boom
        before = lotto.STATE.read_text(encoding="utf-8")
        assert raises(OSError, lotto.mark_pension_attempt, 399)
        assert lotto.STATE.read_text(encoding="utf-8") == before
    finally:
        os.replace = real_replace


def test_alert_recorded_only_when_delivered():
    reset()
    lotto.channel.notify = lambda source, items: pings.append(items) and False     # 텔레그램 실패
    lotto.alert("k", "사유", "상세")
    lotto.alert("k", "사유", "상세")
    assert len(pings) == 2 and "k" not in lotto.load_state().get("alerts", {})   # 못 보냈으면 다음에도 다시 보낸다
    lotto.channel.notify = lambda source, items: pings.append(items) or True
    lotto.alert("k", "사유", "상세")
    lotto.alert("k", "사유", "상세")
    assert len(pings) == 3 and lotto.load_state()["alerts"]["k"] == "2026-11-16"  # 보낸 뒤엔 하루 1번


# ---------- 수리 실행 · 로그인 ----------
def test_login_failure_is_not_ok_and_wrong_password_latches():
    tries = []

    class Down:
        def __init__(self, user, capture):
            tries.append(1)
            raise RuntimeError("동행복권 사이트가 현재 시스템 점검중입니다.")

    class WrongPassword:
        def __init__(self, user, capture):
            tries.append(1)
            raise RuntimeError("로그인에 실패했습니다. 아이디 또는 비밀번호를 확인해주세요.")

    reset()
    lotto.login = ORIGINAL["login"]
    lotto.read_user = lambda: object()
    lotto.RawLotteryClient = Down
    _, last = captured(lotto.cmd_buy)
    assert last["ok"] is False and "skipped" not in last and "login_blocked" not in lotto.load_state(), last   # 점검은 잠그지 않는다
    lotto.RawLotteryClient = WrongPassword
    _, last = captured(lotto.cmd_buy)
    assert last["ok"] is False and lotto.load_state()["login_blocked"], last
    assert any("비번" in p[0][2] for p in pings), pings
    tries.clear()
    _, last = captured(lotto.cmd_buy)                            # 다음 실행: 로그인 시도 자체를 안 한다
    assert not tries and "skipped" in last, (tries, last)
    sat = datetime(2026, 11, 21, 21, 30, tzinfo=KST)             # check 도 로그인 안 함
    lotto.now_kst = lambda: sat
    lotto.upsert_orders([lotto_order(LOTTO_ROUND, 3)])
    captured(lotto.cmd_check)
    assert not tries
    state = lotto.load_state()                                   # 손으로 지우면 다시 로그인한다
    del state["login_blocked"]
    lotto.save_state(state)
    lotto.now_kst = lambda: MON
    captured(lotto.cmd_buy)
    assert tries


# ---------- 계정 파일 (DPAPI) ----------
HEAD = "🚨 동행복권 이상 감지 — {} · 비밀번호를 바꾸고 예치금 충전 계좌 연결을 확인하세요"
TUE = datetime(2026, 11, 17, 10, 0, tzinfo=KST)


def test_anomaly_none_for_own_purchases_and_first_run():
    reset()
    _, last = captured(run_buy, [], ack=False)                   # 첫 실행: 기준값 없음 → 무알림, 평소대로 삼
    assert last["ok"] is True and len(sends) == 1 and "🚨" not in sends[0][0] and not pings, (sends, pings)
    state = lotto.load_state()
    assert state["last_balance"] == {"ts": "2026-11-16T10:00:00+09:00", "amount": 40000} and len(state["spend_log"]) == 2, state
    assert "anomaly_seen" not in state
    nxt = datetime(2026, 11, 23, 10, 0, tzinfo=KST)              # 다음 주: 구매내역엔 내가 산 것만, 예치금도 쓴 만큼(8,000원)만 줄었음 → 무알림
    lotto.now_kst = lambda: nxt
    calls.clear()
    sends.clear()
    _, last = captured(run_buy, list(site), balance=32000, ack=False)
    assert "login" in calls and ("execBuy", 3) in calls and last["ok"] is True, (calls, last)   # 평소대로 이번 주 로또를 산다
    assert not any("🚨" in t for t, _ in sends) and not pings, (sends, pings)
    state = lotto.load_state()
    assert state["last_balance"]["amount"] == 32000 and [e["won"] for e in state["spend_log"]] == [3000], state   # 기준 갱신 뒤 쓴 것만 남는다


def test_anomaly_order_not_mine_stops_buying_once():
    reset()
    _, last = captured(run_buy, [lotto_order(LOTTO_ROUND, 1, no="202611150001234")], ack=False)   # 내가 안 산 로또 1게임
    assert "ok" not in last and "이상징후" in last["skipped"] and not tuples(), (last, calls)      # 그 실행은 아무것도 안 삼 (실패가 아니라 건너뜀)
    text, silent = sends[0]
    assert text.splitlines()[0] == HEAD.format("내가 안 산 로또 주문") and silent is False, text
    assert "1게임(1,000원)" in text and "주문번호 끝 1234" in text and "2026-11-16" in text and "202611150001234" not in text, text
    assert not pings and len(sends) == 1                          # 🚨 없이 한 통
    calls.clear()
    sends.clear()
    _, last = captured(run_buy, list(site), ack=False)            # 한 번 알린 주문은 다음 실행부터 정상 — 모자란 2게임을 삼
    assert ("execBuy", 2) in calls and last["ok"] is True and not any("🚨" in t for t, _ in sends), (calls, sends)
    reset()
    lotto.mark_lotto_attempt(LOTTO_ROUND, 3)                      # 내가 3게임 결제했는데 사이트엔 5게임 = 상한 밖 수량
    _, last = captured(run_buy, [lotto_order(LOTTO_ROUND, 3), lotto_order(LOTTO_ROUND, 2, no="X9999")], ack=False)
    assert "skipped" in last and "5게임 중 2게임(2,000원)" in sends[0][0], sends
    reset(reserved=False)
    rsv_rows.append(rsv_row(343))                                 # 예약내역은 이 감지 대상이 아님 (구매내역·예치금만 본다)
    _, last = captured(run_buy, [pension_order(PENSION_ROUND, no="LP72-777")], ack=False)   # 내가 안 산 연금 5장
    assert "skipped" in last and sends[0][0].splitlines()[0] == HEAD.format("내가 안 산 연금720 주문"), sends


def test_anomaly_other_product():
    reset()
    other = lotto.other_order({"ltGdsNm": "스피또1000", "ntslOrdrNo": "S2026111500005678", "eltOrdrDt": "2026-11-15"})
    _, last = captured(run_buy, [other], ack=False)
    assert "skipped" in last and not tuples(), (last, calls)
    assert sends[0][0].splitlines()[:2] == [HEAD.format("다른 복권 구매 (스피또1000)"), "스피또1000 · 주문번호 끝 5678 · 2026-11-15"], sends
    assert "other:S2026111500005678:2026-11-15:스피또1000:" in lotto.load_state()["anomaly_seen"]


def set_baseline(amount: int, spent: int = 0) -> None:
    state = lotto.load_state()
    state["last_balance"] = {"ts": "2026-11-16T10:00:00+09:00", "amount": amount}
    state["spend_log"] = [{"ts": "2026-11-16T10:00:05+09:00", "won": spent, "what": "테스트"}] if spent else []
    lotto.save_state(state)


def test_anomaly_balance_drop_unexplained():
    reset(TUE)
    set_baseline(30000, spent=8000)
    _, last = captured(run_buy, [], balance=22000, ack=False)     # 내가 쓴 8,000원만큼만 줄었음 → 정상 (딱 같음)
    assert last["ok"] is True and not any("🚨" in t for t, _ in sends), sends
    reset(TUE)
    set_baseline(30000, spent=8000)
    _, last = captured(run_buy, [], balance=12000, ack=False)     # 1만원이 더 줄었음
    assert "skipped" in last and not tuples(), (last, calls)
    text = sends[0][0]
    assert text.splitlines()[0] == HEAD.format("예치금 1만원 이 설명 안 됨 — 털림 의심"), text
    assert "구매가능 3만원 → 지금 1만 2,000원" in text and "결제 요청한 돈 8,000원" in text and "2026-11-16T10:00:00" in text, text
    assert lotto.load_state()["last_balance"]["amount"] == 12000  # 알렸으니 새 기준 → 다음 실행은 평소대로
    sends.clear()
    _, last = captured(run_buy, [], balance=12000, ack=False)
    assert last["ok"] is True and ("execBuy", 3) in calls and not any("🚨" in t for t, _ in sends), (last, sends)
    reset(TUE)
    set_baseline(30000)
    text, last = captured(run_buy, [], balance=80000, ack=False)  # 늘어난 건 충전·입금 — 알림 없이 정보만
    assert last["ok"] is True and "충전·입금 감지" in text and "5만원" in text and not any("🚨" in t for t, _ in sends), text
    reset(TUE)
    set_baseline(30000)
    lotto.channel.send = lambda text, silent=None: sends.append((text, silent)) and False   # 알림 발송 실패
    _, last = captured(run_buy, [], balance=10000, ack=False)
    assert "skipped" in last and lotto.load_state()["last_balance"]["amount"] == 30000  # 못 알렸으면 기준을 안 바꾼다
    lotto.now_kst = lambda: datetime(2026, 11, 18, 10, 0, tzinfo=KST)
    lotto.channel.send = lambda text, silent=None: sends.append((text, silent)) or True
    _, last = captured(run_buy, [], balance=10000, ack=False)     # 다음 실행이 다시 잡는다
    assert "skipped" in last and "예치금 2만원 이 설명 안 됨" in sends[-1][0], sends


def test_anomaly_same_kind_once_a_day_but_still_blocks():
    reset()
    first = lotto.other_order({"ltGdsNm": "스피또1000", "ntslOrdrNo": "S1111", "eltOrdrDt": "2026-11-15"})
    second = lotto.other_order({"ltGdsNm": "파워볼", "ntslOrdrNo": "S2222", "eltOrdrDt": "2026-11-16"})
    captured(run_buy, [first], ack=False)
    _, last = captured(run_buy, [first, second], ack=False)       # 같은 날 같은 종류 두 번째: 알림은 안 가지만 구매는 멈춘다
    assert len(sends) == 1 and "skipped" in last and not tuples(), (sends, last)
    lotto.now_kst = lambda: TUE
    _, last = captured(run_buy, [first, second], ack=False)       # 다음 날 알린다
    assert len(sends) == 2 and "파워볼" in sends[1][0] and "스피또" not in sends[1][0] and "skipped" in last, sends


def test_anomaly_password_changed_after_working_login():
    class WrongPassword:
        def __init__(self, user, capture):
            raise RuntimeError("로그인에 실패했습니다. 아이디 또는 비밀번호를 확인해주세요.")

    reset()
    lotto.login = ORIGINAL["login"]
    lotto.read_user = lambda: object()
    lotto.RawLotteryClient = WrongPassword
    captured(lotto.cmd_buy)                                       # 한 번도 로그인된 적 없음 → 그냥 비번 오류 (이상징후 아님)
    assert not sends and lotto.load_state()["login_blocked"], sends
    reset()
    lotto.login = ORIGINAL["login"]
    lotto.read_user = lambda: object()
    lotto.RawLotteryClient = WrongPassword
    set_baseline(30000)                                           # 전엔 로그인이 됐다
    _, last = captured(lotto.cmd_buy)
    assert last["ok"] is False and len(sends) == 1 and sends[0][0].splitlines()[0] == HEAD.format("되던 로그인이 비번 오류로 바뀜"), sends
    assert "2026-11-16T10:00:00" in sends[0][0] and lotto.load_state()["login_blocked"]


def test_anomaly_in_check_alerts_but_results_go_on():
    sat = datetime(2026, 11, 21, 21, 30, tzinfo=KST)
    reset(sat)
    this_round = lotto.sales_round(sat.date())
    lotto.mark_lotto_attempt(this_round, 3)
    mine = dict(lotto_order(this_round, 3), result="낙첨")
    other = lotto.other_order({"ltGdsNm": "스피또2000", "ntslOrdrNo": "S7777", "eltOrdrDt": "2026-11-20"})
    lotto.fetch_orders = lambda client, capture, days: [mine, other]
    lotto.read_balance = lambda client, capture, buying=True: calls.append(("balance", buying)) or 5000
    _, last = captured(lotto.cmd_check)
    assert calls.count("login") == 1 and ("balance", False) in calls, calls       # 이미 한 로그인 안에서만
    assert [t.splitlines()[0] for t, _ in sends][0] == HEAD.format("다른 복권 구매 (스피또2000)"), sends
    assert last["ok"] is True and last["notified"] == [f"{L}:{this_round}"] and len(sends) == 2, (last, sends)   # 결과 알림은 그대로
    assert lotto.load_state()["last_balance"]["amount"] == 5000


def test_anomaly_dry_run_writes_nothing():
    reset()
    lotto.DRY = True
    lotto.channel.DRY_RUN = True
    other = lotto.other_order({"ltGdsNm": "스피또1000", "ntslOrdrNo": "S1111", "eltOrdrDt": "2026-11-15"})
    before = lotto.STATE.read_text(encoding="utf-8")
    _, last = captured(run_buy, [other], ack=False)
    assert "skipped" in last and not tuples()
    assert lotto.STATE.read_text(encoding="utf-8") == before      # 알린 기록·잔액 기준 어느 것도 안 쓴다


# ---------- 구매 뒤 확인 ----------
def test_bought_but_not_visible_alerts_and_halts():
    reset()
    FakeClient.visible = False
    run_buy([pension_order(PENSION_ROUND)], balance=1000)        # 로또 1게임만 삼 → 응답은 성공인데 구매내역에 안 보임
    assert ("execBuy", 1) in calls and any("확인 안 됨" in p[0][2] for p in pings), (calls, pings)
    assert lotto.halted(L, LOTTO_ROUND) and lotto.needs_login(L, LOTTO_ROUND) is False   # 2게임이 남았어도 이 회차는 더 로그인 안 함
    reset()
    run_buy([lotto_order(LOTTO_ROUND, 3)])                       # 연금 직접 구매가 보이면 조용
    assert ("pension", PENSION_ROUND) in calls and not pings and not lotto.halted(P, PENSION_ROUND), (calls, pings)


def test_pension_bought_but_not_visible_halts():
    reset()
    results = {lotto.GAME_LABEL[P]: {"number": "123456", "tickets": 5, "attempted": True}}
    lotto.fetch_orders = lambda client, capture, days: []        # 재조회엔 연금이 안 보임
    lotto.verify_purchases(None, None, {P: PENSION_ROUND}, results, [])
    assert lotto.halted(P, PENSION_ROUND) and any("연금 구매 확인 안 됨" in p[0][2] for p in pings), pings
    assert lotto.needs_login(P, PENSION_ROUND) is False


def test_lotto_failure_classified_by_ledger():
    reset()
    FakeClient.fail = "reject"                                   # 실패했고 구매내역에도 없음
    _, last = captured(run_buy, [pension_order(PENSION_ROUND)])
    assert pings[-1][0][2] == "로또 구매 실패" and "돈 안 나감" in pings[-1][0][3], pings
    assert last["ok"] is False and not sends and lotto.lotto_attempted_games(LOTTO_ROUND) == 3   # 시도 기록은 남는다 (다시 안 삼)
    reset()
    FakeClient.fail = "after"                                    # 응답은 실패인데 구매내역엔 있음
    run_buy([pension_order(PENSION_ROUND)])
    assert "결과 불명" in pings[-1][0][2] and "돈 나감" in pings[-1][0][3], pings
    reset()
    FakeClient.fail = "lost"                                     # dhapi '알 수 없는 오류'(응답을 못 읽음) + 구매내역엔 없음 → 실패로 단정하지 않는다
    run_buy([pension_order(PENSION_ROUND)])
    assert pings[-1][0][2] == "로또 구매 결과 불명 — 구매내역 확인" and "돈 안 나감" not in pings[-1][0][3], pings
    assert "응답을 못 읽었고" in pings[-1][0][3] and lotto.lotto_attempted_games(LOTTO_ROUND) == 3, pings
    reset()
    FakeClient.fail = "reject"                                   # 실패했는데 구매내역도 못 읽음
    run_buy([pension_order(PENSION_ROUND)], refetch_fails=True)
    assert "결과 불명" in pings[-1][0][2] and "구매내역도 못 읽" in pings[-1][0][3], pings
    assert paid("execBuy") == [("execBuy", 3)]                   # 어떤 경우에도 결제 재시도 없음


# ---------- 연금 직접 결제 1번 ----------
class FakeGame:
    """가짜 연금 게임 서버. mode — 직접 구매: ok · unknown(결제 뒤 응답 없음) · order-fail(결제 전 실패) · code(100 아닌 코드)
    예약: ok · reject(서버가 코드로 거절) · timeout(결제 뒤 응답 없음) · double(겹침) · hidden(성공인데 예약내역에 안 보임)."""
    mode = "ok"

    def __init__(self, session, dry):
        self.dry = dry

    def open(self):
        calls.append("game-open")

    def open_reserve(self):
        calls.append("reserve-open")

    def round_info(self):
        return {"ROUND": str(PENSION_ROUND), "IS_SALE_FLAG": "true", "CLOSE_DATE_TIME": "2026-11-19 16:59:59",
                "DRAW_DATE": "2026-11-19"}

    def assign_set_number(self, r):
        return "123456"

    def buy_set(self, r, number, before_payment):
        if FakeGame.mode == "order-fail":
            raise pension720.PensionError("예약 실패")
        before_payment()
        if FakeGame.mode == "unknown":
            raise TimeoutError("응답 없음")
        if FakeGame.mode == "code":
            return {"code": "110", "tickets": [], "msg": pension720.short_text({"data": {"secret": "원문 덩어리"}})}
        return {"code": "100", "tickets": [(g, number) for g in range(1, 6)], "msg": ""}

    def reserve(self, cur, rounds, win_dates, deposit, before_payment):
        assert not self.dry, "dry-run 인데 예약 요청"
        calls.append(("addMyReserve", tuple(rounds), tuple(win_dates), deposit))
        if FakeGame.mode == "double":
            raise pension720.PensionError("이미 예약된 회차가 겹침 (1개 회차)")
        before_payment()
        if FakeGame.mode == "timeout":
            raise TimeoutError("응답 없음")
        if FakeGame.mode == "reject":
            return {"code": "5350", "order_no": "", "msg": "한도 초과"}
        if FakeGame.mode == "reject-paid":                       # 서버는 거절 코드인데 예치금은 줄었음
            wallet["won"] -= 5000 * len(rounds)
            return {"code": "5350", "order_no": "", "msg": "한도 초과"}
        wallet["won"] -= 5000 * len(rounds) + (1000 if FakeGame.mode == "overcharge" else 0)
        if FakeGame.mode == "four":                              # 한 회차가 4장만 예약됨
            rsv_rows.extend(rsv_row(r, qty=4 if r == rounds[0] else 5) for r in rounds)
        elif FakeGame.mode == "stray":                           # 예약 안 한 회차까지 예약됨
            rsv_rows.extend(rsv_row(r) for r in [*rounds, rounds[-1] + 1])
        elif FakeGame.mode != "hidden":
            rsv_rows.extend(rsv_row(r) for r in rounds)
        return {"code": "100", "order_no": "R20261116", "msg": ""}


def test_pension_unknown_result_never_retries():
    reset()
    buy = lotto.buy_pension
    pension720.PensionGame = FakeGame
    FakeGame.mode = "unknown"
    out = buy(FakeClient(None), PENSION_ROUND)
    assert "결과 불명" in out["error"] and out["attempted"], out
    assert lotto.pension_attempted(PENSION_ROUND)
    assert lotto.needs_login(P, PENSION_ROUND) is False   # 다음 실행은 연금 때문에 로그인 안 함
    assert lotto.round_have([], [], None, [PENSION_ROUND])[(P, PENSION_ROUND)] == 5
    out = buy(FakeClient(None), PENSION_ROUND)            # 이미 적힌 회차: 결제 요청 전에 멈춘다 ('결과 불명' 아님)
    assert out == {"error": "주문 실패"} and "돈 안 나감" in pings[-1][0][3], (out, pings[-1])
    reset()
    pension720.PensionGame = FakeGame
    FakeGame.mode = "order-fail"
    out = buy(FakeClient(None), PENSION_ROUND)
    assert "주문 실패" in out["error"] and not lotto.pension_attempted(PENSION_ROUND), out
    reset()
    pension720.PensionGame = FakeGame
    assert buy(FakeClient(None), PENSION_ROUND) == {"number": "123456", "tickets": 5, "attempted": True}
    assert lotto.pension_attempted(PENSION_ROUND)


def test_alerts_carry_no_raw_server_dump():
    reset()
    pension720.PensionGame = FakeGame
    FakeGame.mode = "code"
    out = lotto.buy_pension(FakeClient(None), PENSION_ROUND)
    detail = pings[-1][0][3]
    assert out["error"] == "결제 코드 110" and "110" in detail and "{" not in detail and "원문" not in detail, detail
    s = pension720.short_text
    assert s({"a": 1}) == "(dict)" and s(["x"]) == "(list)" and s(None) == "" and s(" 한도\n초과 ") == "한도 초과" and len(s("가" * 500)) == 60


def test_dry_run_never_pays():
    game = ORIGINAL_GAME(session=None, dry=True)
    assert {"/connPro.do", "/makeOrderNo.do", "/addMyReserve.do", "/cancelMyReserve.do", "/checkMyReserve.do"} <= pension720.PAYMENT_PATHS
    for path in pension720.PAYMENT_PATHS:
        try:
            game._step(path, [])
        except AssertionError:
            continue
        raise AssertionError(f"dry-run 이 {path} 를 막지 않음")
    marks = []                                                   # 예약도: 겹침 확인조차 안 보내고 멈춘다
    assert raises(AssertionError, game.reserve, 342, NEXT5, ["x"] * 5, 30000, lambda: marks.append(1))
    assert not marks
    source = (LIB / "lotto.py").read_text(encoding="utf-8") + (LIB / "pension720.py").read_text(encoding="utf-8")
    assert source.count('"/cancelMyReserve.do"') == 1            # 취소는 막는 목록에만 있고 부르는 곳이 없다


# ---------- 연금 예약 ----------
def test_reserves_five_rounds_once_when_none_reserved():
    reset(reserved=False)
    pension720.PensionGame = FakeGame
    _, last = captured(run_buy, [], real_reserve=True)
    dates = ("2026.11.26", "2026.12.03", "2026.12.10", "2026.12.17", "2026.12.24")
    assert paid("addMyReserve") == [("addMyReserve", tuple(NEXT5), dates, 40000 - 8000)], calls
    assert ("pension", PENSION_ROUND) in calls and ("execBuy", 3) in calls    # 이번 회차는 직접 + 로또
    assert [t for t, _ in sends] == [
        "이번 주 복권을 샀어요 🍀\n연금복권 342회: 1-5조 123456\n"
        f"로또 {LOTTO_ROUND}회: 3, 11, 17, 25, 38, 42 / 3, 11, 17, 25, 38, 42 / 3, 11, 17, 25, 38, 42\n(이번 주 8,000원 / 한도 8,000원)",
        "연금복권 5주치를 예약했어요 343-347회 · 2만 5,000원"], sends
    assert "🚨" not in sends[1][0] and not pings and last["ok"] is True, (pings, last)
    attempt = lotto.load_state()["pension_reserve_attempts"]
    assert len(attempt) == 1 and attempt[0]["rounds"] == NEXT5 and attempt[0]["result"] == "ok", attempt
    calls.clear()
    _, last = captured(run_buy, site, real_reserve=True)         # 다음 실행: 할 일 없음 → 로그인도 안 함
    assert "login" not in calls and "skipped" in last, (calls, last)
    nxt = datetime(2026, 11, 23, 10, 0, tzinfo=KST)              # 다음 주 월요일(343회 판매 중): 343 은 예약분이라 직접 안 삼, 예약도 안 함
    lotto.now_kst = lambda: nxt
    calls.clear()
    run_buy([], real_reserve=True)
    assert not paid("pension") and not paid("addMyReserve") and ("execBuy", 3) in calls, calls


def test_reserved_round_not_in_ledger_is_not_bought_again():
    reset(reserved=False)
    rsv_rows.extend(rsv_row(r) for r in [342, 343])              # 이번 회차가 예약돼 있는데 구매내역엔 아직 없음 (state 도 모름)
    run_buy([])
    assert not paid("pension") and not paid("reserve") and ("execBuy", 3) in calls, calls
    assert lotto.reserved_seen() == {342, 343}                   # 본 예약을 적어 둔다 → 다음 실행은 이것 때문에 로그인하지 않는다
    assert lotto.needs_login(P, PENSION_ROUND) is False
    assert lotto.reserve_due(PENSION_ROUND) is False


def test_partial_reservation_coverage_skips_reserving():
    reset(reserved=False)
    rsv_rows.append(rsv_row(345))                                # 다음 5회차 중 345 만 손으로 예약돼 있음
    run_buy([])
    assert not paid("reserve") and ("pension", PENSION_ROUND) in calls, calls   # 예약은 안 넣고 이번 회차만 직접
    assert not pings and lotto.reserved_seen() == {345}
    lotto.upsert_orders(site)
    calls.clear()
    run_buy(site)                                                # 다음 실행은 예약 때문에 다시 로그인하지 않는다
    assert "login" not in calls, calls
    reset(reserved=False)
    rsv_rows.append(rsv_row(345, status="3"))                    # 취소된 예약뿐이면 5회차 전부 빈 것 → 예약
    run_buy([])
    assert paid("reserve") == [("reserve", PENSION_ROUND, tuple(NEXT5))], calls
    reset(reserved=False)
    rsv_rows.append(rsv_row(346, status="RB99"))                 # 모르는 상태 코드는 '있음'으로 (안 사는 쪽)
    run_buy([])
    assert not paid("reserve"), calls


def test_reservation_list_unreadable_no_pension_at_all():
    reset(reserved=False)
    _, last = captured(run_buy, [], rsv_fails=True)
    assert not paid("pension") and not paid("reserve") and ("execBuy", 3) in calls, calls    # 로또는 예약과 무관
    assert any("예약내역 조회 실패" in p[0][2] for p in pings) and last["ok"] is False, (pings, last)


def test_reservation_needs_full_deposit():
    reset(reserved=False)
    run_buy([], balance=8000 + 24999)                            # 이번 주 것 사고 남은 돈이 예약 값에 1원 모자람
    assert not paid("reserve") and ("pension", PENSION_ROUND) in calls and ("execBuy", 3) in calls, calls
    assert "예치금 부족" in pings[-1][0][2] and "2만 5,000원" in pings[-1][0][3], pings
    reset(reserved=False)
    run_buy([], balance=8000 + 25000)                            # 딱 맞으면 예약
    assert paid("reserve") and not pings, (calls, pings)


def test_reserve_dry_run_plans_but_never_posts():
    reset(reserved=False)
    pension720.PensionGame = FakeGame
    lotto.DRY = True
    text, last = captured(run_buy, [], real_reserve=True)
    assert "[DRY] 연금 예약 계획: 343-347회" in text and "2만 5,000원" in text, text
    assert not paid("addMyReserve") and not sends and last["dry"] is True, (calls, sends)
    assert "pension_reserve_attempts" not in lotto.load_state() and "lotto_attempts" not in lotto.load_state()


def test_reserve_rejected_falls_back_to_weekly_buys():
    reset(reserved=False)
    pension720.PensionGame = FakeGame
    FakeGame.mode = "reject"                                     # 서버가 코드로 거절 + 예약내역에도 없음 → 실패 확정
    _, last = captured(run_buy, [], real_reserve=True)
    assert lotto.load_state()["pension_reserve_attempts"][0]["result"] == "failed" and last["ok"] is False
    assert pings[-1][0][2] == "연금 예약 실패" and "5350" in pings[-1][0][3] and len(sends) == 1, (pings, sends)
    assert lotto.reserve_due(PENSION_ROUND) is False             # 같은 회차들에 예약을 또 시도하지 않는다
    assert lotto.round_have([], [], None, NEXT5) == {(P, r): 0 for r in NEXT5}    # 대신 그 주가 되면 직접 산다
    assert lotto.needs_login(P, 343) is True
    calls.clear()
    lotto.upsert_orders(site)
    run_buy(site, real_reserve=True)
    assert "login" not in calls, calls                           # 이번 주 할 일은 끝 — 예약 재시도로 로그인하지 않는다


def test_reserve_unknown_result_counts_as_reserved():
    for mode, where in (("timeout", "0/5"), ("hidden", "0/5")):
        reset(reserved=False)
        pension720.PensionGame = FakeGame
        FakeGame.mode = mode
        _, last = captured(run_buy, [], real_reserve=True)
        assert lotto.load_state()["pension_reserve_attempts"][0]["result"] == "unknown", mode
        assert "결과 불명" in pings[-1][0][2] and where in pings[-1][0][3] and last["ok"] is False, (mode, pings)
        assert len(sends) == 1                                    # 예약 핑은 안 나감 (구매 핑만)
        assert lotto.round_have([], [], None, NEXT5) == {(P, r): 5 for r in NEXT5}, mode   # 예약된 걸로 치고 안 산다
        assert lotto.needs_login(P, 343) is False and lotto.reserve_due(PENSION_ROUND) is False
        assert len(paid("addMyReserve")) == 1                     # 재시도 없음


def test_reserve_stops_before_payment():
    reset(reserved=False)
    pension720.PensionGame = FakeGame
    FakeGame.mode = "double"                                     # 사이트가 '이미 예약된 회차'라고 답함 → 결제 요청 안 보냄
    out = lotto.reserve_pension(FakeClient(None), PENSION_ROUND, NEXT5, 30000)
    assert out == {"error": "예약 실패"} and "돈 안 나감" in pings[-1][0][3], (out, pings)
    assert "pension_reserve_attempts" not in lotto.load_state()
    reset(reserved=False)                                        # 사이트 회차가 계산과 다르면 준비 단계에서 멈춤

    class OtherRound(FakeGame):
        def round_info(self):
            return {**super().round_info(), "ROUND": "343"}

    pension720.PensionGame = OtherRound
    out = lotto.reserve_pension(FakeClient(None), PENSION_ROUND, NEXT5, 30000)
    assert out == {"error": "준비 실패"} and not paid("addMyReserve"), out


def test_pension_game_reserve_request_shape():
    game = ORIGINAL_GAME(session=None, dry=False)
    seen, marks = [], []
    answers = {"/checkMyReserve.do": {"resultCode": "100", "doubleRound": []},
               "/addMyReserve.do": {"resultCode": "100", "reserveOrderNo": "R1", "resultMsg": {"big": "dump"}}}

    def step(path, fields):
        seen.append((path, dict(fields), len(marks)))
        return answers[path]

    game._step = step
    dates = pension720.reserve_draw_dates({"DRAW_DATE": "2026-11-19"}, 342, NEXT5)
    assert dates == ["2026.11.26", "2026.12.03", "2026.12.10", "2026.12.17", "2026.12.24"], dates
    out = game.reserve(342, NEXT5, dates, 30000, before_payment=lambda: marks.append(1))
    assert out == {"code": "100", "order_no": "R1", "msg": "(dict)"}, out
    (p1, f1, m1), (p2, f2, m2) = seen
    assert (p1, m1, p2, m2) == ("/checkMyReserve.do", 0, "/addMyReserve.do", 1)        # 확인 → 기록 → 결제 순서
    assert f1["winDate"] == "" and f2["winDate"] == ",".join(dates)
    assert {k: f2[k] for k in ("ROUND", "nextRound", "reserveJo", "repeatRoundCnt", "repeatClass", "roundBuyCnt", "totalBuyAmt",
                               "totalBuyCnt", "curpay", "curdeposit", "repeatRound", "repeatRoundHidden")} == {
        "ROUND": "342", "nextRound": "343", "reserveJo": "0", "repeatRoundCnt": "5", "repeatClass": "5", "roundBuyCnt": "1",
        "totalBuyAmt": "25000", "totalBuyCnt": "25", "curpay": "25000", "curdeposit": "30000", "repeatRound": "5",
        "repeatRoundHidden": "2026.11.26"}, f2
    for bad in ({"resultCode": "100", "doubleRound": [{"doubleRound": 343, "doubleCnt": 5}]},    # 겹침
                {"resultCode": "100"}, {"resultCode": "E000", "doubleRound": []}):               # 모양이 다름 · 실패 코드
        seen.clear()
        marks.clear()
        answers["/checkMyReserve.do"] = bad
        assert raises(pension720.PensionError, game.reserve, 342, NEXT5, dates, 30000, lambda: marks.append(1)), bad
        assert not marks and [s[0] for s in seen] == ["/checkMyReserve.do"], bad                 # 결제 요청 전에 멈춤
    d = pension720.reserve_draw_dates
    assert raises(pension720.PensionError, d, {"DRAW_DATE": "2026-11-20"}, 342, NEXT5)           # 사이트 추첨일이 공식과 다름
    assert raises(pension720.PensionError, d, {}, 342, NEXT5)
    assert raises(pension720.PensionError, d, {"DRAW_DATE": "2026-11-19"}, 342, [344, 345])      # 다음 회차부터가 아님
    assert raises(pension720.PensionError, d, {"DRAW_DATE": "2026-11-19"}, 342, [*NEXT5, 348])   # 6회차
    assert raises(pension720.PensionError, d, {"DRAW_DATE": "2026-11-19"}, 342, [])


def site_rsv(round_no, qty=5, status="1", **extra) -> dict:
    return {"ltGdsCd": "LP72", "ltGdsNm": "연금복권720+", "ltEpsd": round_no, "rsvtOrdrQty": qty, "rsvtPrchsSttsCd": status,
            "rsvtOrdrAmt": 5000, "rsvtPrchsNo": f"R{round_no}", **extra}


class FakeHttp:
    def __init__(self, pages, status=200):
        self.pages, self.status, self.gets = pages, status, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((url, params))
        page = (params or {}).get("pageNum")
        body = self.pages.get(page, {"data": {"list": [], "total": 0}}) if page else None
        return type("Resp", (), {"status_code": self.status, "json": lambda self_: body})()


def test_reservation_list_parsing():
    parse = pension720.parse_reservations
    rows, total, raw = parse({"data": {"total": 4, "list": [site_rsv(343), site_rsv(344, status="3"), site_rsv(345, status=2),
                                                            {"ltGdsCd": "LD14", "ltGdsNm": "파워볼", "ltEpsd": 1}]}})
    assert (total, raw) == (4, 4) and rows == [rsv_row(343), rsv_row(344, status="3"), rsv_row(345, status="2")], rows
    assert lotto.reserved_in(rows, 343) == 5 and lotto.reserved_in(rows, 344) == 0 and lotto.reserved_in(rows, 345) == 5
    assert parse({"data": {"list": [], "total": 0}}) == ([], 0, 0)
    for bad in ({"data": {"list": [site_rsv(None)], "total": 1}},              # 회차 없음
                {"data": {"list": [site_rsv(343, qty=0)], "total": 1}},        # 매수 0
                {"data": {"list": [site_rsv(343, qty=None)], "total": 1}},
                {"data": {"list": [{"ltEpsd": 343, "rsvtOrdrQty": 5}], "total": 1}},   # 무슨 복권인지 모름
                {"data": {"list": ["x"], "total": 1}}, {"data": {"total": 0}}, {"data": None}, {}, None, [],
                {"resultCode": "E001", "data": {"list": [], "total": 0}}):
        assert raises(pension720.PensionError, parse, bad), bad
    page = lambda rounds, total: {"data": {"total": total, "list": [site_rsv(r) for r in rounds]}}  # noqa: E731
    http = FakeHttp({1: page(range(343, 353), 12), 2: page([353, 354], 12)})
    got = pension720.fetch_reservations(http, date(2026, 9, 17), date(2026, 11, 16))
    assert [r["round"] for r in got] == list(range(343, 355)) and len(http.gets) == 3, http.gets    # 화면 1번 + 목록 2쪽
    params = http.gets[1][1]
    assert params["srchStrDt"] == "20260917" and params["srchEndDt"] == "20261116" and params["srchLtGdsCdArr"] == "LP72", params
    assert all(u.startswith("https://www.dhlottery.co.kr/mypage/") for u, _ in http.gets)
    f = pension720.fetch_reservations
    assert raises(pension720.PensionError, f, FakeHttp({i: page(range(10), 999) for i in range(1, 9)}), date(2026, 9, 17), date(2026, 11, 16))
    assert raises(pension720.PensionError, f, FakeHttp({}, status=500), date(2026, 9, 17), date(2026, 11, 16))
    # 전체 줄 수(total)를 안 주거나 0 으로 줘도 꽉 찬 쪽 뒤는 계속 읽는다 — 25줄: 10 + 10 + 5
    span = (date(2026, 9, 17), date(2026, 11, 16))
    for total in (0, None, "missing"):
        def page25(rounds, total=total):
            data = {"list": [site_rsv(r) for r in rounds]}
            if total != "missing":
                data["total"] = total
            return {"data": data}
        http = FakeHttp({1: page25(range(343, 353)), 2: page25(range(353, 363)), 3: page25(range(363, 368))})
        got = f(http, *span)
        assert [r["round"] for r in got] == list(range(343, 368)) and len(http.gets) == 4, (total, http.gets)   # 화면 1 + 목록 3쪽
        http = FakeHttp({1: page25(range(343, 368))})                 # 쪽 크기를 무시하고 25줄을 한 쪽에 — 다음 쪽이 빌 때까지 본다
        assert len(f(http, *span)) == 25 and len(http.gets) == 3, (total, http.gets)
        http = FakeHttp({1: page25(range(343, 353)), 2: page25(range(353, 363))})   # 딱 20줄: 3쪽째가 비어서 끝
        assert len(f(http, *span)) == 20 and len(http.gets) == 4, (total, http.gets)
        http = FakeHttp({1: page25(range(343, 346))})                 # 덜 찬 첫 쪽: 거기가 끝 (요청을 늘리지 않는다)
        assert len(f(http, *span)) == 3 and len(http.gets) == 2, (total, http.gets)
        full = FakeHttp({i: page25(range(343, 353)) for i in range(1, pension720.RESERVE_LIST_MAX_PAGES + 1)})
        assert raises(pension720.PensionError, f, full, *span), total   # 끝을 못 보면 다 못 읽은 것


# ---------- 판정 · check ----------
WIN = {"group": 3, "number": "194929", "bonus": "783820", "date": "2026-11-19"}
PNL_TAIL = "(손익은 세후 · 연금식 당첨금은 실제 받은 돈만 셈)"


def test_judge():
    same = [(g, "194929") for g in range(1, 6)]
    assert pension720.judge(same, WIN) == ("1등×1 · 2등×4", pension720.PRIZE["1등"] + 4 * pension720.PRIZE["2등"])
    assert pension720.judge([(g, "000009") for g in range(1, 6)], WIN) == ("7등×5", 5000)
    assert pension720.judge([(g, "094929") for g in range(1, 6)], WIN) == ("3등×5", 5_000_000)
    assert pension720.judge([(1, "783820")], WIN) == ("보너스×1", pension720.PRIZE["보너스"])
    assert pension720.judge([(g, "111111") for g in range(1, 6)], WIN) == ("낙첨", 0)
    assert pension720.tickets_of("1:123456,2:123456,9:123456,3:12345") == [(1, "123456"), (2, "123456")]


def test_check_thursday_no_login():
    thu = datetime(2026, 11, 19, 21, 30, tzinfo=KST)
    reset(thu)
    lotto.upsert_orders([pension_order(PENSION_ROUND, number="000009")])
    state = lotto.load_state()
    state["check_missing"] = {L: lotto.latest_drawn_round(thu)}   # 로또 쪽 이유 없앰
    lotto.save_state(state)
    pension720.fetch_winning = lambda session=None: {PENSION_ROUND: WIN}
    lotto.cmd_check()
    assert "login" not in calls, calls
    assert not pings and len(sends) == 1, (pings, sends)
    text, silent = sends[0]
    assert text == ("연금복권 7등 당첨! +5,000원 (비과세)\n"
                    "연금복권 342회: 7등 5장 +5,000원 (비과세) (당첨번호 3조 194929 · 내 번호 1-5조 000009)\n"
                    f"올해 0원 · 누적 0원 {PNL_TAIL}"), text
    assert silent is None and "🚨" not in text
    rows = lotto.drawn_rows()
    assert rows and rows[0]["prize"] == 5000 and rows[0]["prize_net"] == 5000 and rows[0]["notified_at"]
    sends.clear()
    lotto.cmd_check()                                     # 두 번 돌아도 한 통
    assert not sends


def test_check_before_draw_waits():
    thu = datetime(2026, 11, 19, 19, 30, tzinfo=KST)      # 추첨(19:05) 뒤지만 20:00 전
    reset(thu)
    lotto.upsert_orders([pension_order(PENSION_ROUND)])
    assert lotto.pension_due(thu) == []


def test_result_marked_notified_only_when_sent():
    thu = datetime(2026, 11, 19, 21, 30, tzinfo=KST)
    reset(thu)
    lotto.upsert_orders([pension_order(PENSION_ROUND, number="000009")])
    pension720.fetch_winning = lambda session=None: {PENSION_ROUND: WIN}
    lotto.channel.send = lambda text, silent=None: sends.append((text, silent)) and False      # 텔레그램 실패
    assert lotto.notify_results(thu) == ([], [])
    assert len(sends) == 1 and not briefs and not lotto.drawn_rows()                         # 못 보냈으면 '보냄' 아님
    lotto.channel.send = lambda text, silent=None: sends.append((text, silent)) or True
    done, _ = lotto.notify_results(thu)                                                      # 다음 실행이 다시 보낸다
    assert done == [f"{P}:{PENSION_ROUND}"] and len(sends) == 2 and len(lotto.drawn_rows()) == 1 and briefs
    assert lotto.notify_results(thu) == ([], [])


def test_lotto_result_only_after_draw_and_known_wording():
    thu = datetime(2026, 11, 19, 21, 30, tzinfo=KST)      # 로또 이번 주 회차는 토요일 추첨 — 아직 추첨 전
    reset(thu)
    this_round = lotto.sales_round(thu.date())
    for word in ("추첨 전", "-", "낙첨", "당첨", "미확인"):   # 추첨 전 회차는 문구가 뭐든 결과로 안 본다
        lotto.upsert_orders([dict(lotto_order(this_round, 3), result=word)])
        assert lotto.notify_results(thu) == ([], []) and not sends and not pings, word
    sat = datetime(2026, 11, 21, 21, 30, tzinfo=KST)      # 추첨 뒤: 아는 문구만 결과로
    lotto.now_kst = lambda: sat
    for word in ("미확인", "-", "5등", "당첨완료"):
        pings.clear()
        lotto.STATE.unlink(missing_ok=True)                 # 경보 하루 1번 기록을 지워 문구마다 본다
        lotto.upsert_orders([dict(lotto_order(this_round, 3), result=word)])
        assert lotto.notify_results(sat) == ([], []) and not sends, word
        assert pings and pings[-1][0][2] == "로또 결과 문구가 낯섦" and word in pings[-1][0][3], (word, pings)
    pings.clear()
    lotto.upsert_orders([dict(lotto_order(this_round, 3), result="미추첨")])
    assert lotto.notify_results(sat) == ([], []) and not pings          # 아직 미추첨 — 조용히 기다림
    mon = datetime(2026, 11, 23, 12, 0, tzinfo=KST)
    assert lotto.notify_results(mon) == ([], [f"{L}:{this_round}"])     # 월요일까지 미추첨이면 '늦음'
    lotto.upsert_orders([dict(lotto_order(this_round, 3), result="낙첨")])
    done, late = lotto.notify_results(sat)
    assert done == [f"{L}:{this_round}"] and not late and "낙첨" in sends[0][0], (done, sends)
    r = lotto.result_state
    assert [r(x) for x in ("", None, "미추첨", " 미추첨 ", "추첨 전", "낙첨", "당첨", "미확인", "-", "당첨금 지급")] == \
        ["pending", "pending", "pending", "pending", "pending", "drawn", "drawn", "unknown", "unknown", "unknown"]


# ---------- 2026-11-16 첫 실구매에서 나온 것 ----------
# selectMyLotteryledger.do 실응답 모양 그대로 (342회 모든 조 세트 = 같은 주문번호로 한 장 한 줄)
REAL_PENSION_ROWS = [
    {"ltGdsCd": "LP72", "ltGdsNm": "연금복권720+", "ltEpsdView": "342", "ltEpsd": 342, "prchsQty": 1,
     "ltWnResult": "미추첨", "ltWnAmt": None, "gmInfo": f"{g}:314159", "ntslOrdrNo": "202611160000001",
     "epsdRflDt": "2026-11-19", "eltOrdrDt": "2026-11-16"}
    for g in range(1, 6)
]


def raw_client(detail: str = "[A] 자동: 1 2 3 4 5 6") -> lotto.RawLotteryClient:
    client = object.__new__(lotto.RawLotteryClient)   # 로그인 없이 파서만
    client._get_lotto645_ticket_detail = lambda order_no, barcode, bought: detail
    client._DETAIL_REQUEST_DELAY = 0
    return client


def test_real_pension_ledger_rows():
    reset()
    payload = {"data": {"list": REAL_PENSION_ROWS + [dict(REAL_PENSION_ROWS[0])]}}   # 같은 줄이 한 번 더 와도 5장
    orders = raw_client()._parse_buy_list_json(payload)
    assert len(orders) == 1, orders
    o = orders[0]
    assert (o["order_no"], o["game"], o["round"], o["games"]) == ("LP72-202611160000001", P, 342, 5), o
    assert o["numbers"] == "\n".join(f"{g}조 314159" for g in range(1, 6)), o["numbers"]
    assert o["draw_date"] == "2026-11-19" and o["bought_on"] == "2026-11-16" and o["result"] == ""
    assert lotto.tickets_in(orders, P, 342) == 5
    assert lotto.round_have(orders, [], None, [342])[(P, 342)] == 5                  # 구매내역만으로도 '있음'
    lotto.upsert_orders(orders)
    lotto.upsert_orders(orders)                                                      # 두 번 넣어도 5장
    assert lotto.db_tickets(P, 342) == 5
    assert pension720.parse_tickets(lotto.unnotified_by_round()[(P, 342)][0]["numbers"]) == \
        [(g, "314159") for g in range(1, 6)]
    # 이름만 맞는 줄(코드 없음)도 연금으로
    assert pension720.is_pension_item({"ltGdsNm": "연금복권720+"}) and not pension720.is_pension_item({"ltGdsNm": "로또6/45"})


def test_pension_row_unreadable_is_unknown_not_guessed():
    for bad in (dict(REAL_PENSION_ROWS[0], gmInfo="", prchsQty=5),            # 번호 없음 — 구입매수로 세지 않는다
                dict(REAL_PENSION_ROWS[0], gmInfo="1조314159"),               # 번호 형식이 바뀜
                dict(REAL_PENSION_ROWS[0], ltEpsd=None, ltEpsdView=""),       # 회차 없음
                dict(REAL_PENSION_ROWS[0], ltEpsd=0, ltEpsdView="0")):        # 회차 0
        old = [dict(r, ltEpsd=341, ltEpsdView="341", ntslOrdrNo="OLD") for r in REAL_PENSION_ROWS]   # 지난 회차 줄은 멀쩡
        orders = lotto.parse_pension_orders(old + [bad])
        assert [o.get("unknown", False) for o in orders] == [False, True], (bad, orders)
        reset()
        run_buy(orders)                                                       # 못 읽은 줄이 하나라도 있으면 아무것도 안 산다
        assert not tuples() and "balance" not in calls and "못 셈" in pings[-1][0][2], (bad, calls)
    mixed = lotto.parse_pension_orders(REAL_PENSION_ROWS[:2] + [dict(REAL_PENSION_ROWS[2], ltEpsd=343, ltEpsdView="343")])
    assert any(o.get("unknown") for o in mixed), mixed                        # 같은 주문번호인데 회차가 다름


def test_lotto_row_without_detail_is_unknown():
    item = {"ltGdsNm": "로또6/45", "ltGdsCd": "LO40", "ntslOrdrNo": "X", "ltEpsdView": str(LOTTO_ROUND), "prchsQty": 1,
            "gmInfo": "12345", "eltOrdrDt": "2026-11-16"}
    for detail in ("조회 실패", "번호 정보 없음", "번호 확인 불가", ""):            # 3게임짜리 주문인데 번호 조회 실패 + prchsQty 1
        orders = raw_client(detail)._parse_buy_list_json({"data": {"list": [item]}})
        assert len(orders) == 1 and orders[0].get("unknown"), (detail, orders)
    reset()
    run_buy(raw_client("조회 실패")._parse_buy_list_json({"data": {"list": [item]}}))
    assert not tuples() and "balance" not in calls, calls                      # 1게임으로 세고 2게임 더 사지 않는다
    good = raw_client("[A] 자동: 1 2 3 4 5 6\n[B] 자동: 7 8 9 10 11 12\n[C] 자동: 13 14 15 16 17 18")
    assert good._parse_buy_list_json({"data": {"list": [item]}})[0]["games"] == 3
    renamed = dict(item, ltGdsNm="로또 6/45", ltGdsCd="")                      # 상품명 띄어쓰기가 바뀌어도 로또 줄로 본다
    assert good._parse_buy_list_json({"data": {"list": [renamed]}})[0]["games"] == 3
    assert good._parse_buy_list_json({"data": {"list": [dict(item, ltGdsNm="새이름")]}})[0]["games"] == 3   # 코드로도


def test_payment_response_dict_or_str():
    lst = ",".join(f"314159|202611160000001|{i}|342|{g}" for i, g in enumerate(range(1, 6)))
    want = [(g, "314159") for g in range(1, 6)]
    t = pension720._tickets_from_payment
    assert t({"resultCode": "100", "resultMsg": {"data": {"prchsLtNoInfoLstCn": lst}}}, "314159") == want   # 실구매 때 터진 모양
    assert t({"resultCode": "100", "resultMsg": '{"data": {"prchsLtNoInfoLstCn": "%s"}}' % lst}, "314159") == want
    assert t({"resultCode": "100", "data": {"prchsLtNoInfoLstCn": lst}}, "314159") == want
    assert t({"resultCode": "100", "data": '{"prchsLtNoInfoLstCn": "%s"}' % lst}, "314159") == want
    for odd in ({"resultMsg": {"data": None}}, {"resultMsg": "구매 완료"}, {"resultMsg": None}, {"resultMsg": 3},
                {"resultMsg": "[1, 2]"}, {"data": {"prchsLtNoInfoLstCn": ["x"]}}, {}):
        assert t(odd, "314159") == [], odd
    assert pension720.as_dict('{"a": 1}') == {"a": 1} and pension720.as_dict({"a": 1}) == {"a": 1}
    assert pension720.as_dict("") is None and pension720.as_dict("[1]") is None and pension720.as_dict(b'{"a":1}') == {"a": 1}


def test_buy_set_with_dict_result_msg():
    game = ORIGINAL_GAME(session=None, dry=False)
    lst = ",".join(f"314159|O|{g}|342|{g}" for g in range(1, 6))
    answers = {"/makeOrderNo.do": {"resultCode": "100", "orderNo": "202611160000001", "orderDate": "20261116002203"},
               "/connPro.do": {"resultCode": "100", "resultMsg": {"data": {"prchsLtNoInfoLstCn": lst}}}}
    game._step = lambda path, fields: answers[path]
    marks = []
    out = game.buy_set(342, "314159", before_payment=lambda: marks.append(1))
    assert out["code"] == pension720.SUCCESS and len(out["tickets"]) == 5 and marks == [1], out
    assert out["msg"] == "(dict)"                                                     # 서버 덩어리는 알림에 안 싣는다
    answers["/connPro.do"] = {"resultCode": 110, "resultMsg": {"x": 1}}               # 숫자 코드도 글로 → '불명' 처리 경로
    assert game.buy_set(342, "314159", before_payment=lambda: None)["code"] == "110"


def test_daily_sales_start_0600():
    at = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=KST)  # noqa: E731  16=월
    for d in (16, 19, 21):                                        # 월·목·토
        assert lotto.buy_window_closed(at(d, 5, 59)) is not None
        assert lotto.buy_window_closed(at(d, 6, 0)) is None
        assert lotto.buy_window_closed(at(d, 0, 22)) is not None
    assert lotto.pension_window_closed(at(16, 5, 59)) is not None
    assert lotto.pension_window_closed(at(16, 6, 0)) is None
    assert lotto.open_rounds(at(16, 5, 59))[0] == {}
    assert set(lotto.open_rounds(at(16, 6, 0))[0]) == {L, P}
    reset(at(16, 0, 22))                                          # 첫 실구매 시각 — 로그인도 안 함
    run_buy([])
    assert "login" not in calls, calls


def test_dry_run_reports_pension_already_bought():
    reset()
    lotto.DRY = True
    lotto.mark_pension_attempt(PENSION_ROUND)
    orders = raw_client()._parse_buy_list_json({"data": {"list": REAL_PENSION_ROWS}})
    text, _ = captured(run_buy, orders)
    assert not sends                                       # dry-run 은 구매 알림 안 보냄
    assert f"연금720 {PENSION_ROUND}회 5장" in text and f"로또 {LOTTO_ROUND}회 0게임" in text, text
    assert ("lotto", LOTTO_ROUND, 3) in calls and not paid("pension") and not paid("execBuy"), calls
    assert "lotto_attempts" not in lotto.load_state()      # dry-run 은 시도 기록도 안 쓴다


# ---------- 주 환산 8,000원 (회차 상한) ----------
def test_week_cap_pension_already_bought_lotto_3_only():
    reset()
    run_buy([pension_order(PENSION_ROUND, 5)])
    assert ("execBuy", 3) in calls and not paid("pension"), calls
    assert len(sends) == 1 and sends[0][0].endswith("(이번 주 8,000원 / 한도 8,000원)") and "연금복권" not in sends[0][0], sends


def test_week_cap_all_bought_buys_nothing():
    reset()
    run_buy([pension_order(PENSION_ROUND, 5), lotto_order(LOTTO_ROUND, 3)])
    assert not tuples() and not sends, (calls, sends)


def test_week_cap_uncountable_row_buys_nothing():
    reset()
    bad = lotto.unknown_order({"ltGdsNm": "로또6/45", "ntslOrdrNo": "X1"})
    run_buy([bad])
    assert not tuples() and "balance" not in calls, calls
    assert pings and "못 셈" in pings[-1][0][2] and "8,000원" in pings[-1][0][3], pings
    # 파서: 회차 없는 로또 줄·회차 없는 연금 줄 → 셀 수 없음 표시, DB 엔 안 들어감
    orders = raw_client()._parse_buy_list_json({"data": {"list": [
        {"ltGdsNm": "로또6/45", "ntslOrdrNo": "X2", "ltEpsdView": ""},
        dict(REAL_PENSION_ROWS[0], ltEpsd=None, ltEpsdView="")]}})
    assert len(orders) == 2 and all(o.get("unknown") for o in orders), orders
    lotto.upsert_orders(orders)
    assert lotto.db_tickets(P, 0) == 0
    others = raw_client()._parse_buy_list_json({"data": {"list": [{"ltGdsNm": "스피또1000", "ntslOrdrNo": "S1"}]}})
    assert len(others) == 1 and others[0]["other"] and others[0]["games"] == 0 and others[0]["name"] == "스피또1000", others
    lotto.upsert_orders(others)                                 # 다른 복권은 세지도 DB 에 넣지도 않는다 (이상징후로만 본다)
    assert lotto.tickets_in(others, L, LOTTO_ROUND) == 0 and lotto.unnotified_by_round() == {}


def test_week_cap_counts_other_week_rounds_out():
    reset()
    run_buy([pension_order(PENSION_ROUND - 1, 5), lotto_order(LOTTO_ROUND - 1, 3)])   # 지난주 회차는 이번 회차 상한과 무관
    assert ("pension", PENSION_ROUND) in calls and ("execBuy", 3) in calls, calls


# ---------- 구매 알림 ----------
def test_purchase_message():
    reset()
    run_buy([])
    assert len(sends) == 1, sends
    text, silent = sends[0]
    assert text == ("이번 주 복권을 샀어요 🍀\n"
                    "연금복권 342회: 1-5조 123456\n"
                    f"로또 {LOTTO_ROUND}회: 3, 11, 17, 25, 38, 42 / 3, 11, 17, 25, 38, 42 / 3, 11, 17, 25, 38, 42\n"
                    "(이번 주 8,000원 / 한도 8,000원)"), text
    assert "🚨" not in text and briefs
    r = {L: 1251, P: 342}
    assert lotto.purchase_message(r, {"연금720": {"error": "x"}, "로또": {"error": "y"}}, 0) is None
    assert lotto.purchase_message(r, {"로또": {"games": 2, "numbers": []}}, 2000) == \
        "이번 주 복권을 샀어요 🍀\n로또 1251회: 자동 2게임 (번호는 구매내역에서)\n(이번 주 2,000원 / 한도 8,000원)"
    assert lotto.reserve_message(NEXT5, 25000) == "연금복권 5주치를 예약했어요 343-347회 · 2만 5,000원"
    reset()
    lotto.DRY = True
    run_buy([])
    assert not sends                                           # dry-run 은 안 보냄


# ---------- 결과 알림 톤 (두괄식 · 세후) ----------
WIN_L = {"numbers": [1, 2, 3, 4, 5, 6], "bonus": 7}
EMOJI_FIRST = "🎉🎊💰💸🔥🍾🏆👑🌈✨" * 2


def lotto_rows(*games: str, prize: int = 0, result: str | None = None) -> list[dict]:
    numbers = "\n".join(f"[{'ABCDE'[i]}] 자동: {g}" for i, g in enumerate(games))
    return [dict(lotto_order(1251, len(games)), numbers=numbers, result=result or ("당첨" if prize else "낙첨"), prize=prize)]


def pension_rows(number: str) -> list[dict]:
    rows = [pension_order(342, number=number)]
    lotto.judge_pension(rows, WIN)
    return rows


def render(*entries) -> tuple[str, bool]:
    return lotto.render_result(list(entries), -12000, -12000)


def first_info_line(text: str) -> str:
    return next(l for l in text.splitlines() if not l.startswith("🎉"))


def test_lotto_rank():
    r = lotto.lotto_rank
    assert [r(n, WIN_L) for n in ([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 7], [1, 2, 3, 4, 5, 9], [1, 2, 3, 4, 8, 9],
                                  [1, 2, 3, 10, 8, 9], [1, 2, 11, 10, 8, 9])] == ["1등", "2등", "3등", "4등", "5등", None]
    assert lotto.lotto_numbers_of("[A] 자동: 6 5 4 3 2 1\n[B] 수동: 1 2\n엉뚱") == [[1, 2, 3, 4, 5, 6]]


def test_tax_rules():
    t = lotto.lotto_tax
    assert t(5_000) == 0 and t(2_000_000) == 0                       # 200만원 정각까지 비과세
    assert t(2_000_001) == 439_780, t(2_000_001)
    assert t(300_001_000) == 66_000_000                              # 공제 뒤 딱 3억 → 전부 22%
    assert t(300_002_000) == 66_000_000 + 330                        # 3억 넘는 1,000원만 33%
    assert lotto.after_tax(2_000_000_000) == 1_373_000_330           # 20억 → 세후 13억 7,300만원 근처
    assert lotto.money_text(2_000_000_000, lotto.after_tax(2_000_000_000)) == "세후 약 13억 7,300만원 (세전 20억원)"
    assert lotto.money_text(5_000, 5_000) == "+5,000원 (비과세)"
    assert lotto.annuity_net(7_000_000) == 5_460_000 and lotto.annuity_net(1_000_000) == 780_000


def test_lotto_game_prizes_split():
    g = lotto.lotto_game_prizes
    assert g(["5등", "5등"], 10_000) == [5_000, 5_000]
    assert g(["3등", "5등"], 1_435_000) == [5_000, 1_430_000]
    assert g(["1등"], 2_000_000_000) == [2_000_000_000]
    assert g([], 5_000) == [5_000] and g([], 0) == [] and g(["4등"], 0) == []     # 사이트 금액과 안 맞으면 한 장으로
    rows = lotto_rows("1 2 3 4 5 6", "1 2 3 10 11 12", prize=2_000_005_000)   # 1등 + 5등 한 주문
    e = lotto.lotto_entry(1251, rows, WIN_L)
    assert rows[0]["prize_net"] == 5_000 + 1_373_000_330 and e["net"] == rows[0]["prize_net"], e


def test_lotto_site_prize_zero():
    rows = lotto_rows("1 2 3 10 11 12", "1 2 3 4 20 21", "30 31 32 33 34 35", result="당첨")   # 5등 + 4등인데 사이트 금액 0
    e = lotto.lotto_entry(1251, rows, WIN_L)
    assert e["prize"] == 55_000 and e["net"] == 55_000 and rows[0]["prize"] == 55_000 and rows[0]["prize_net"] == 55_000, e
    assert e["ranks"] == "4등 1게임 · 5등 1게임" and e["tier"] == "win"
    for games in (("1 2 3 4 5 10",), ("1 2 3 4 5 6", "1 2 3 10 11 12")):                       # 3등 · 1등+5등인데 금액 0 → 보류
        assert lotto.lotto_entry(1251, lotto_rows(*games, result="당첨"), WIN_L) is None, games
    assert lotto.lotto_entry(1251, lotto_rows("1 2 3 10 11 12", result="당첨"), None) is None    # 당첨이라는데 등수도 금액도 모름
    assert lotto.lotto_entry(1251, lotto_rows("30 31 32 33 34 35"), WIN_L)["tier"] == "miss"     # 낙첨은 그대로
    sat = datetime(2026, 11, 21, 21, 30, tzinfo=KST)                                            # notify: 보류는 안 보내고, 늦으면 경보 대상
    reset(sat)
    this_round = lotto.sales_round(sat.date())
    lotto.lotto_winning = lambda r: WIN_L
    lotto.upsert_orders([dict(lotto_order(this_round, 1), numbers="[A] 자동: 1 2 3 4 5 10", result="당첨", prize=0)])
    assert lotto.notify_results(sat) == ([], []) and not sends
    assert lotto.notify_results(datetime(2026, 11, 23, 12, 0, tzinfo=KST)) == ([], [f"{L}:{this_round}"]) and not sends


def test_pension_money():
    m = lambda ranks: lotto.pension_money(ranks)[1]  # noqa: E731
    assert m(["1등", "2등", "2등", "2등", "2등"]) == "월 546만원 × 20년 + 월 312만원 × 10년 (세전 월 700만원 + 월 400만원)"
    assert m(["1등"]) == "월 546만원 × 20년 (세전 월 700만원)"
    assert m(["보너스"] * 5) == "월 390만원 × 10년 (세전 월 500만원)"
    assert m(["7등"] * 5) == "+5,000원 (비과세)" and m(["3등"] * 5) == "+500만원 (비과세)"    # 장마다 100만 → 비과세
    # 손익엔 실제 받은 돈만: 연금식(1·2등·보너스)은 아직 0, 일시금만 센다
    assert lotto.pension_money(["1등"])[0] == 0 and lotto.pension_money(["보너스"] * 5)[0] == 0
    assert lotto.pension_money(["7등"] * 5)[0] == 5_000 and lotto.pension_money(["1등", "7등"])[0] == 1_000


def test_annuity_not_counted_in_pnl():
    reset()
    rows = pension_rows("194929")                                                   # 세트 1등 (+2등×4) = 전부 연금식
    entry = lotto.pension_entry(342, rows, WIN)
    assert rows[0]["prize_net"] == 0 and entry["net"] == 0, (rows[0]["prize_net"], entry)
    assert lotto.pnl(rows) == -5_000                                                # 20년치 16.8억을 벌었다고 적지 않는다
    text, _ = lotto.render_result([entry], *lotto.pnl_lines(rows, date(2026, 11, 19)))
    assert "예상 연금(세후): 월 546만원 × 20년 + 월 312만원 × 10년 — 손익엔 받은 돈만 넣어서 아직 0원" in text, text
    assert text.splitlines()[-1] == f"올해 −5,000원 · 누적 −5,000원 {PNL_TAIL}", text
    assert "예상 연금" not in render(lotto.pension_entry(342, pension_rows("000009"), WIN))[0]


def test_tone_miss():
    text, loud = render(lotto.pension_entry(342, pension_rows("111111"), WIN),
                        lotto.lotto_entry(1251, lotto_rows("10 11 12 13 14 15"), WIN_L))
    assert text == ("결과가 나왔어요. 이번 주는 낙첨이에요 😢 다음 주에 또!\n"
                    "연금복권 342회: 낙첨 (당첨번호 3조 194929 · 내 번호 1-5조 111111)\n"
                    "로또 1251회: 낙첨 (당첨번호 1, 2, 3, 4, 5, 6 + 보너스 7)\n"
                    f"올해 −1만 2,000원 · 누적 −1만 2,000원 {PNL_TAIL}"), text
    assert loud is False and lotto.CLAIM_NOTE not in text


def test_tone_small():
    text, loud = render(lotto.lotto_entry(1251, lotto_rows("1 2 3 10 11 12", "20 21 22 23 24 25", prize=5000), WIN_L))
    assert text.splitlines()[:2] == ["로또 5등 당첨! +5,000원 (비과세)",
                                     "로또 1251회: 5등 1게임 +5,000원 (비과세) (당첨번호 1, 2, 3, 4, 5, 6 + 보너스 7)"], text
    assert not loud and lotto.CLAIM_NOTE not in text and "아쉽" not in text


def test_tone_mixed_top_rank_first():
    text, _ = render(lotto.pension_entry(342, pension_rows("000009"), WIN),          # 연금 7등×5 (소액)
                     lotto.lotto_entry(1251, lotto_rows("1 2 3 4 10 11", prize=50000), WIN_L))   # 로또 4등
    assert first_info_line(text) == "로또 4등 당첨!! +5만원 (비과세)", text
    assert "연금복권 342회: 7등 5장 +5,000원 (비과세) (" in text and "아쉽" not in text


def test_tone_win_4th():
    text, loud = render(lotto.lotto_entry(1251, lotto_rows("1 2 3 4 10 11", prize=50000), WIN_L))
    lines = text.splitlines()
    assert lines[0] == "🎉🎊" * 4 and lines[1] == "로또 4등 당첨!! +5만원 (비과세)", text
    assert lotto.CLAIM_NOTE in text and not loud


def test_tone_third():
    text, loud = render(lotto.lotto_entry(1251, lotto_rows("1 2 3 4 5 10", prize=1_430_000), WIN_L))
    lines = text.splitlines()
    assert lines[:2] == ["🎉🎊🍀💰" * 4] * 2 and lines[3] == "🎉🎊🍀💰" * 4, text
    assert lines[2] == "!!!! 로또 3등 당첨!!!! +143만원 (비과세)" and loud and lotto.CLAIM_NOTE in text
    text, _ = render(lotto.pension_entry(342, pension_rows("094929"), WIN))          # 연금 3등×5
    assert first_info_line(text) == "!!!! 연금복권 3등 당첨!!!! +500만원 (비과세)", text


def test_tone_second():
    text, loud = render(lotto.lotto_entry(1251, lotto_rows("1 2 3 4 5 7", prize=54_000_000), WIN_L))
    lines = text.splitlines()
    assert lines[:4] == ["🎉🎊💰🔥🍾" * 3] * 4 and lines[5:9] == ["🎉🎊💰🔥🍾" * 3] * 4, text
    assert lines[4] == "!!!!!!!! 로또 2등 당첨!!!!!!!! 대박!!!!!!!! 세후 약 4,212만원 (세전 5,400만원)", lines[4]
    assert loud
    text, _ = render(lotto.pension_entry(342, pension_rows("783820"), WIN))           # 연금 보너스 = 2등 등급
    assert first_info_line(text) == "!!!!!!!! 연금복권 보너스 당첨!!!!!!!! 대박!!!!!!!! 월 390만원 × 10년 (세전 월 500만원)", text


def test_tone_first():
    text, loud = render(lotto.lotto_entry(1251, lotto_rows("1 2 3 4 5 6", prize=2_000_000_000), WIN_L),
                        lotto.pension_entry(342, pension_rows("111111"), WIN))
    lines = text.splitlines()
    assert lines[:5] == [EMOJI_FIRST] * 5 and lines[6:11] == [EMOJI_FIRST] * 5, lines
    assert lines[5] == "!!!!!!!!!!!!! 로또 1등 당첨!!!!!!!! 인생 역전!!!!!!!! 세후 약 13억 7,300만원 (세전 20억원)", lines[5]
    assert lines[11].startswith("로또 1251회: 1등 1게임 세후 약 13억 7,300만원") and lines[12].startswith("연금복권 342회: 낙첨")
    assert len(EMOJI_FIRST) >= 15 and loud and len(text) <= 4096 and "결과가 나왔어요" not in text
    text, _ = render(lotto.pension_entry(342, pension_rows("194929"), WIN))           # 연금 세트 1등 (+2등×4)
    assert first_info_line(text) == ("!!!!!!!!!!!!! 연금복권 1등 당첨!!!!!!!! 인생 역전!!!!!!!! "
                                     "월 546만원 × 20년 + 월 312만원 × 10년 (세전 월 700만원 + 월 400만원)"), text


def test_tone_lotto_fallback_without_numbers():
    entry = lotto.lotto_entry(1251, lotto_rows("1 2 3 10 11 12", prize=5000), None)
    assert entry["tier"] == "small" and "못 받음" in entry["paren"], entry
    assert first_info_line(render(entry)[0]) == "로또 당첨! +5,000원 (비과세)"
    assert lotto.lotto_entry(1251, lotto_rows("1 2 3 10 11 12"), None)["tier"] == "miss"


def test_tone_length_cap():
    many = [dict(lotto.lotto_entry(1251, lotto_rows("1 2 3 4 5 6", prize=2_000_000_000), WIN_L),
                 paren="x" * 600) for _ in range(6)]
    text, _ = lotto.render_result(many, 0, 0)
    assert len(text) <= lotto.TG_LIMIT, len(text)


def test_reserve_weeks_left_line():
    thu = datetime(2026, 11, 19, 21, 30, tzinfo=KST)                  # 342회 추첨 직후
    reset(thu, reserved=False)
    assert lotto.reserve_left_line(thu) is None                       # 예약을 한 번도 안 했으면 줄 없음
    lotto.remember_reserved_seen({343, 344})
    assert lotto.reserve_left_line(thu) is None                       # 2주 남음 — 조용
    lotto.remember_reserved_seen({342, 343})
    line = "연금 예약 1주 남음 — 다음 구매 때 5주치 2만 5,000원 다시 예약 (예치금 확인)"
    assert lotto.reserve_left_line(thu) == line                       # 1주 남음
    lotto.remember_reserved_seen({341, 342})
    assert lotto.reserve_left_line(thu).startswith("연금 예약 0주 남음")
    lotto.remember_reserved_seen({342, 343})
    lotto.upsert_orders([pension_order(PENSION_ROUND, number="111111")])
    state = lotto.load_state()
    state["check_missing"] = {L: lotto.latest_drawn_round(thu)}
    lotto.save_state(state)
    pension720.fetch_winning = lambda session=None: {PENSION_ROUND: WIN}
    lotto.cmd_check()                                                 # 예약분 회차 결과도 구매내역에 뜬 뒤엔 똑같이 판정·알림
    lines = sends[0][0].splitlines()
    assert lines[-2] == line and lines[-1].startswith("올해 "), lines


def test_pnl_after_tax():
    reset()
    rows = [dict(lotto_order(1251, 3), result="당첨", prize=2_000_000_000, draw_date="2026-11-21"),
            dict(lotto_order(1250, 3, no="L0"), result="낙첨", prize=0, draw_date="2026-11-14")]
    assert lotto.pnl(rows) == 1_373_000_330 - 6_000                   # prize_net 없는 줄은 세전 한 장으로 세금
    rows[0]["prize_net"] = 1_000_000_000
    assert lotto.pnl(rows) == 1_000_000_000 - 6_000


def test_won_korean_units():
    w = lotto.won
    cases = {0: "0원", 999: "999원", 1000: "1,000원", 9999: "9,999원", 10000: "1만원", 12000: "1만 2,000원",
             50000: "5만원", 1_430_000: "143만원", 54_000_000: "5,400만원", 99_999_999: "9,999만 9,999원",
             100_000_000: "1억원", 1_950_000_000: "19억 5,000만원", 2_000_000_000: "20억원",
             1_680_000_000: "16억 8,000만원", 123_400_000_005: "1,234억 5원", -12000: "−1만 2,000원", -999: "−999원",
             -100_000_000: "−1억원"}
    for x, want in cases.items():
        assert w(x) == want, (x, w(x), want)
    assert w(5000, signed=True) == "+5,000원" and w(-5000, signed=True) == "−5,000원" and w(0, signed=True) == "0원"


def test_crypto_roundtrip():
    jsid = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdef"
    plain = "ROUND=342&SEL_NO=&BUY_CNT=5&orderDate=2026-11-16+10%3A00%3A00"
    q = pension720.encrypt(plain, jsid)
    assert pension720.decrypt(q, jsid) == plain
    assert pension720.wire_q("ab+c/d=") == "ab%252Bc%2Fd%3D"


# ---------- 예약 흐름 ----------


def put_attempt(result: str, before: int | None = None, after: int | None = None, rounds: list[int] | None = None,
                rejected: bool | None = None) -> None:
    """예약 시도 한 줄을 state 에 직접 넣는다 (끊긴 실행·결과 불명 뒤 상태 흉내)."""
    state = lotto.load_state()
    row = {"rounds": list(rounds or NEXT5), "ts": "2026-11-16T10:00:00+09:00", "result": result, "deposit_before": before}
    if after is not None:
        row["deposit_after"] = after
    if rejected is not None:
        row["rejected"] = rejected                                # 그때 서버가 코드로 거절했나 (settle_reservation 이 적는다)
    state["pension_reserve_attempts"] = [row]
    lotto.save_state(state)


def attempt_result() -> str:
    return lotto.load_state()["pension_reserve_attempts"][0]["result"]


def test_reserve_skipped_when_earlier_step_had_trouble():
    for trouble in ("reject", "lost", "after", "not-visible"):
        reset(reserved=False)
        pension720.PensionGame = FakeGame
        if trouble == "not-visible":
            FakeClient.visible = False                            # 샀다는데 구매내역에 안 보임 → 확인 안 됨(halt)
        else:
            FakeClient.fail = trouble                             # 로또 실패 · 결과 불명
        text, last = captured(run_buy, [], real_reserve=True)
        assert not paid("addMyReserve") and "reserve-open" not in calls, (trouble, calls)       # 예약 준비조차 안 한다
        assert "pension_reserve_attempts" not in lotto.load_state(), trouble
        assert "skipped" in last["bought"]["연금 예약"] and "연금 예약 안 함" in text, (trouble, last)
    reset(reserved=False)                                         # 로또 회차 확인이 실패해도 (결제 요청 전 멈춤) 예약은 안 얹는다
    pension720.PensionGame = FakeGame
    lotto.site_lotto_round = lambda: LOTTO_ROUND + 1
    captured(run_buy, [], real_reserve=True)
    assert not paid("addMyReserve"), calls
    FakeClient.fail = None                                        # 다음 실행: 앞 단계가 깨끗하면 그때 예약한다
    lotto.site_lotto_round = lambda: LOTTO_ROUND
    calls.clear()
    captured(run_buy, list(site), real_reserve=True)
    assert len(paid("addMyReserve")) == 1 and attempt_result() == "ok", calls
    out = lotto.reserve_step(None, None, PENSION_ROUND, NEXT5, [], {}, 30000, trouble=True)    # 직접: trouble 이면 아무것도 안 부른다
    assert "skipped" in out


def test_reserve_settle_needs_exact_rounds_qty_and_money():
    cases = (("four", "343회 4장"),                               # 한 회차가 5장이 아님
             ("stray", "348회 5장"),                              # 예약 안 한 회차가 바뀜
             ("overcharge", "줄어든 돈 2만 6,000원"),             # 예치금이 예약 값보다 더 줄었음
             ("hidden", "0/5"))                                   # 응답은 성공인데 예약내역에 없음 (돈은 나감)
    for mode, needle in cases:
        reset(reserved=False)
        pension720.PensionGame = FakeGame
        FakeGame.mode = mode
        _, last = captured(run_buy, [], real_reserve=True)
        attempt = lotto.load_state()["pension_reserve_attempts"][0]
        assert attempt["result"] == "unknown" and attempt["deposit_before"] == 32000, (mode, attempt)
        assert pings[-1][0][2] == "연금 예약 결과 불명 — 예약내역 확인" and needle in pings[-1][0][3], (mode, pings[-1])
        assert len(sends) == 1 and "예약했어" not in sends[0][0] and last["ok"] is False, (mode, sends)   # 예약 핑은 안 나감
        assert len(paid("addMyReserve")) == 1, mode               # 재시도 없음
    reset(reserved=False)                                         # 예약 뒤 예치금을 못 읽으면 성공으로 안 친다
    pension720.PensionGame = FakeGame
    captured(run_buy, [], real_reserve=True, balance_reads_ok=2)
    assert attempt_result() == "unknown" and "예치금을 못 읽었어" in pings[-1][0][3], pings[-1]
    reset(reserved=False)                                         # 다 맞으면 성공 — 금액은 실제로 줄어든 예치금
    pension720.PensionGame = FakeGame
    captured(run_buy, [], real_reserve=True)
    attempt = lotto.load_state()["pension_reserve_attempts"][0]
    assert attempt["result"] == "ok" and attempt["deposit_after"] == 32000 - 25000 and not pings, (attempt, pings)
    assert sends[-1][0] == "연금복권 5주치를 예약했어요 343-347회 · 2만 5,000원", sends


def test_reserve_rejected_but_deposit_dropped_is_unknown():
    reset(reserved=False)
    pension720.PensionGame = FakeGame
    FakeGame.mode = "reject-paid"                                 # 서버는 거절이라는데 예치금이 줄었다 → '실패(돈 안 나감)'로 못 적는다
    captured(run_buy, [], real_reserve=True)
    assert attempt_result() == "unknown" and "결과 불명" in pings[-1][0][2] and "줄어든 돈 2만 5,000원" in pings[-1][0][3], pings[-1]
    assert lotto.round_have([], [], None, NEXT5) == {(P, r): 5 for r in NEXT5}                 # 예약된 걸로 치고 안 산다


def test_reserve_deposit_recheck_right_before_request():
    reset(reserved=False)                                         # 예약 직전 예치금이 계산(시작 − 방금 산 것)과 다르면 안 넣는다
    pension720.PensionGame = FakeGame
    _, last = captured(run_buy, [], real_reserve=True, drift=1)
    assert not paid("addMyReserve") and "pension_reserve_attempts" not in lotto.load_state(), calls
    assert pings[-1][0][2].startswith("연금 예약 미룸") and "돈 안 나감" in pings[-1][0][3] and last["ok"] is False, pings[-1]
    reset(reserved=False)                                         # 못 읽어도 안 넣는다
    pension720.PensionGame = FakeGame
    captured(run_buy, [], real_reserve=True, balance_reads_ok=1)
    assert not paid("addMyReserve") and "못 읽었어" in pings[-1][0][3], (calls, pings[-1])


def test_reserved_seen_saved_only_when_list_was_read():
    reset()                                                       # 다음 5회차 예약이 걸려 있는 평소 주
    lotto.upsert_orders([pension_order(PENSION_ROUND)])           # 이번 회차 연금은 이미 DB 에 → 이번 실행은 로또만
    run_buy([pension_order(PENSION_ROUND)])
    assert ("execBuy", 3) in calls and "rsv-list" not in calls, calls                          # 예약내역을 안 읽었다
    assert lotto.reserved_seen() == set(NEXT5)                    # 안 읽은 실행이 빈 목록으로 덮어쓰지 않는다
    assert lotto.reserve_due(PENSION_ROUND) is False


def test_other_rows_without_order_number_get_their_own_keys():
    reset()
    first = lotto.other_order({"ltGdsNm": "스피또1000", "eltOrdrDt": "2026-11-15", "gmInfo": "A1"})
    second = lotto.other_order({"ltGdsNm": "파워볼", "eltOrdrDt": "2026-11-16", "gmInfo": "B2"})
    assert first["key"] != second["key"] and "other:?" not in (first["key"], second["key"]), (first, second)
    assert first["key"] == "other::2026-11-15:스피또1000:A1", first
    assert [f["key"] for f in lotto.order_anomalies([first, second, first])] == [first["key"], second["key"]]   # 같은 줄이 두 번 와도 한 번
    captured(run_buy, [first], ack=False)
    assert first["key"] in lotto.load_state()["anomaly_seen"]
    assert [f["key"] for f in lotto.order_anomalies([first, second])] == [second["key"]]       # 알린 줄만 빠지고 새 줄은 잡힌다


def test_pending_reserve_alerts_at_start_and_resolves_on_next_login():
    mine = [pension_order(PENSION_ROUND)]
    reset(reserved=False)                                         # 끊긴 예약 → 시작할 때 알림, 예약내역에 5회차가 다 있으면 성공으로
    put_attempt("pending", before=32000)
    rsv_rows.extend(rsv_row(r) for r in NEXT5)
    run_buy(mine)
    assert any("요청 중 끊김" in p[0][2] for p in pings) and "rsv-list" in calls, (pings, calls)
    assert attempt_result() == "ok" and any("예약내역에서 확인됨" in t for t, _ in sends), sends
    reset(reserved=False)                                         # 예약내역에 없고 예치금도 요청 전 그대로여도 → 끊긴 요청은 서버 답을 못 봤으니 불명 (실패로 안 내린다)
    put_attempt("pending", before=32000)
    run_buy(mine, balance=32000)
    assert attempt_result() == "unknown" and not any("안 된 걸로 확인" in p[0][2] for p in pings), pings
    assert any("결과 불명" in p[0][2] and 'result 를 "ok"' in p[0][3] for p in pings), pings     # 손으로 적는 법을 알려 준다
    assert lotto.round_have([], [], None, NEXT5) == {(P, r): 5 for r in NEXT5}                  # 있는 걸로 친다 — 직접 안 산다
    reset(reserved=False)                                         # 예약내역에 없는데 예치금이 요청 전보다 1원이라도 적으면 → 불명 그대로
    put_attempt("pending", before=32000)
    run_buy(mine, balance=31999)
    assert attempt_result() == "unknown" and any("결과 불명" in p[0][2] for p in pings), pings
    assert lotto.round_have([], [], None, NEXT5) == {(P, r): 5 for r in NEXT5}
    assert not paid("reserve") and not paid("pension"), calls     # 가리는 동안 연금 결제는 하나도 안 보낸다


def test_unknown_reserve_downgraded_only_when_no_reservation_and_no_money_out():
    mine = [pension_order(PENSION_ROUND)]
    reset(reserved=False)                                         # 서버가 거절했었고 + 그때 예치금이 안 줄었고 + 지금 예약내역에도 없다 → 실패로 + 한 줄
    put_attempt("unknown", before=32000, after=32000, rejected=True)
    run_buy(mine, balance=5000)                                   # 지금 예치금은 상관없다 (그때 읽은 값이 있으면 그걸 쓴다)
    assert attempt_result() == "failed" and pings[-1][0][2] == "연금 예약 안 된 걸로 확인" and "돈 안 나감" in pings[-1][0][3], pings
    assert not any("요청 중 끊김" in p[0][2] for p in pings)        # unknown 은 시작 알림 대상이 아니다 (그때 이미 알렸다)
    for rejected in (False, None):                                # 서버 응답이 성공이었거나(False) 거절 기록이 없는 옛 줄(None) → 안 보여도 불명 그대로
        reset(reserved=False)
        put_attempt("unknown", before=32000, after=32000, rejected=rejected)
        run_buy(mine, balance=32000)
        assert attempt_result() == "unknown" and not pings, (rejected, pings)
        assert lotto.round_have([], [], None, NEXT5) == {(P, r): 5 for r in NEXT5}, rejected    # 사람이 적을 때까지 '있음'
        assert not paid("reserve") and not paid("pension"), calls
    reset(reserved=False)                                         # 사람이 손으로 failed 로 적으면 그 회차들은 직접 구매로 돌아간다
    put_attempt("failed", before=32000, after=32000, rejected=False)
    assert lotto.round_have([], [], None, NEXT5) == {(P, r): 0 for r in NEXT5} and not lotto.unresolved_reserve_attempts()
    put_attempt("ok", before=32000, after=32000, rejected=False)  # ok 로 적으면 예약 있음
    assert lotto.round_have([], [], None, NEXT5) == {(P, r): 5 for r in NEXT5} and not lotto.unresolved_reserve_attempts()
    reset(reserved=False)                                         # 그때 예치금이 줄었다 → 거절이었어도, 예약내역에 없어도 불명 그대로 (사람 확인)
    put_attempt("unknown", before=32000, after=7000, rejected=True)
    run_buy(mine, balance=40000)
    assert attempt_result() == "unknown" and not pings, pings
    reset(reserved=False)                                         # 그때 예치금을 못 읽었으면 지금 예치금으로: 요청 전 이상이면 실패로
    put_attempt("unknown", before=32000, rejected=True)
    run_buy(mine, balance=32000)
    assert attempt_result() == "failed"
    reset(reserved=False)                                         # 일부 회차만 보이면 불명 그대로
    put_attempt("unknown", before=32000, after=32000, rejected=True)
    rsv_rows.extend(rsv_row(r) for r in NEXT5[:2])
    run_buy(mine)
    assert attempt_result() == "unknown"
    reset(reserved=False)                                         # 예약내역을 못 읽으면 아무것도 안 바꾼다
    put_attempt("unknown", before=32000, after=32000, rejected=True)
    run_buy(mine, rsv_fails=True)
    assert attempt_result() == "unknown"
    reset(reserved=False)                                         # 미해결 예약만으로는 로그인하지 않는다 (로그인을 늘리지 않는다)
    put_attempt("unknown", before=32000, after=32000)
    lotto.upsert_orders([lotto_order(LOTTO_ROUND, 3), pension_order(PENSION_ROUND)])
    run_buy([])
    assert "login" not in calls and attempt_result() == "unknown", calls
    reset(reserved=False)                                         # dry-run 은 판정을 안 적는다
    put_attempt("unknown", before=32000, after=32000)
    lotto.DRY = True
    captured(run_buy, mine)
    assert attempt_result() == "unknown"


def test_two_login_failures_in_a_row_stop_logins():
    tries = []

    class Down:
        def __init__(self, user, capture):
            tries.append(1)
            raise RuntimeError("동행복권 사이트가 현재 시스템 점검중입니다.")

    class Up:
        _session = None

        def __init__(self, user, capture):
            tries.append(1)

    reset()
    lotto.login = ORIGINAL["login"]
    lotto.read_user = lambda: object()
    lotto.RawLotteryClient = Down
    captured(lotto.cmd_buy)
    state = lotto.load_state()
    assert state["login_fail_streak"] == 1 and "login_blocked" not in state, state            # 1번은 안 멈춘다
    lotto.RawLotteryClient = Up                                   # 그 사이 로그인이 되면 횟수를 지운다
    assert lotto.login() is not None and lotto.load_state()["login_fail_streak"] == 0
    lotto.RawLotteryClient = Down
    captured(lotto.cmd_buy)
    assert "login_blocked" not in lotto.load_state()
    captured(lotto.cmd_buy)                                       # 연속 2번째 → 멈춤 (같은 날이어도 '멈췄다' 알림은 간다)
    state = lotto.load_state()
    assert state["login_fail_streak"] == 2 and "연속 2번 실패" in state["login_blocked"], state
    assert pings[-1][0][2] == "로그인 실패 — 연속 실패라 로그인 멈춤", pings[-1]
    tries.clear()
    _, last = captured(lotto.cmd_buy)                             # 그 뒤로는 로그인 시도 자체를 안 한다
    assert not tries and "skipped" in last, (tries, last)


def test_reserve_only_shortfall_alerts_once_a_week():
    reset(reserved=False)
    run_buy([], balance=8000 + 24999)                             # 이번 주 것은 다 사고 예약 값만 1원 모자람
    assert "예치금 부족" in pings[-1][0][2] and "이번 주엔 다시 안 봐" in pings[-1][0][3], pings[-1]
    assert lotto.load_state()["reserve_short_week"] == "2026-11-16"
    lotto.upsert_orders(site)
    for day in (17, 18, 19):                                      # 화-목: 예약 때문에 다시 로그인하지 않는다
        lotto.now_kst = lambda day=day: datetime(2026, 11, day, 10, 0, tzinfo=KST)
        assert lotto.RESERVE not in lotto.wanted({P: PENSION_ROUND}), day
        calls.clear()
        run_buy(list(site))
        assert "login" not in calls, (day, calls)
    lotto.now_kst = lambda: datetime(2026, 11, 23, 10, 0, tzinfo=KST)                           # 다음 주 월요일엔 다시 본다
    assert lotto.RESERVE in lotto.wanted({P: 343})
    reset(reserved=False)                                         # 로또·연금까지 모자란 날은 '예약만 모자람'이 아니다 → 주 기록 안 함
    run_buy([], balance=4000)
    assert "reserve_short_week" not in lotto.load_state() and "예치금 부족" in pings[-1][0][2], pings[-1]


def test_lotto_round_outside_ledger_window_is_abandoned():
    sat = datetime(2026, 11, 21, 21, 30, tzinfo=KST)              # 조회 창 첫날 = 10-31 (오늘 − 21일)
    old_round = lotto.sales_round(sat.date()) - 3
    for bought_on, stale in (("2026-10-30", True), ("2026-10-31", False)):
        reset(sat)
        state = lotto.load_state()
        state["check_missing"] = {L: lotto.latest_drawn_round(sat), P: lotto.latest_drawn_pension(sat)}   # 다른 로그인 이유 없앰
        lotto.save_state(state)
        lotto.upsert_orders([dict(lotto_order(old_round, 3), bought_on=bought_on, draw_date="2026-10-31")])
        if not stale:                                             # 창 첫날에 산 것은 아직 조회된다 → 평소대로 로그인 이유
            assert lotto.login_reasons(sat) == [f"로또 결과 핑 안 보낸 회차 {old_round}"], bought_on
            continue
        assert lotto.login_reasons(sat) == [] and (L, old_round) not in lotto.unnotified_by_round()
        _, last = captured(lotto.cmd_check)
        assert "login" not in calls and "skipped" in last, (calls, last)                       # 이 회차 때문에 더 로그인하지 않는다
        assert len(pings) == 1 and pings[0][0][2].startswith("로또 결과 확인 포기") and f"{old_round}회" in pings[0][0][3], pings
        assert lotto.notify_results(sat) == ([], [])              # '결과 못 가져옴' 경보도 더 안 만든다
        lotto.now_kst = lambda: datetime(2026, 11, 22, 12, 0, tzinfo=KST)
        captured(lotto.cmd_check)
        assert len(pings) == 1, pings                             # 회차당 한 번만 알린다
        assert not lotto.drawn_rows()                             # 손익엔 안 들어간다


def test_check_alerts_when_ledger_has_unreadable_rows():
    sat = datetime(2026, 11, 21, 21, 30, tzinfo=KST)
    reset(sat)
    this_round = lotto.sales_round(sat.date())
    mine = dict(lotto_order(this_round, 3), result="낙첨")
    bad = lotto.unknown_order({"ltGdsNm": "로또6/45", "ntslOrdrNo": "X1"})
    lotto.fetch_orders = lambda client, capture, days: [mine, bad]
    lotto.read_balance = lambda client, capture, buying=True: calls.append("balance") or 5000
    _, last = captured(lotto.cmd_check)
    assert any("못 읽은 줄" in p[0][2] and "이상징후 감지 건너뜀" in p[0][2] for p in pings), pings
    assert "balance" not in calls and last["ok"] is True and last["notified"] == [f"{L}:{this_round}"], (calls, last)   # 결과 알림은 그대로


def test_anomaly_ack_is_by_order_numbers_and_survives_my_later_purchase():
    reset()
    hand = lotto_order(LOTTO_ROUND, 1, no="HAND0001")
    _, last = captured(run_buy, [hand], ack=False)                # 손으로 산 1게임 → 한 번 알림
    assert "skipped" in last and len(sends) == 1
    seen = lotto.load_state()["anomaly_seen"]
    assert list(seen) == [f"{L}:{LOTTO_ROUND}:HAND0001"] and seen[f"{L}:{LOTTO_ROUND}:HAND0001"]["extra"] == 1, seen
    sends.clear()
    captured(run_buy, list(site), ack=False)                      # 다음 실행: 모자란 2게임을 내가 삼 (새 주문번호가 생긴다)
    assert ("execBuy", 2) in calls and len(site) >= 2, calls
    assert lotto.order_anomalies(site) == []                      # 수량이 3으로 늘었어도 다시 안 알린다 (내 기록에 없는 양은 그대로 1)
    stranger = lotto_order(LOTTO_ROUND, 1, no="ZZZZ9999")         # 그 뒤 모르는 주문이 하나 더 → 다시 알린다
    found = lotto.order_anomalies([*site, stranger])
    assert len(found) == 1 and found[0]["extra"] == 2 and "ZZZZ9999" in found[0]["key"] and "HAND0001" in found[0]["key"], found
    reset()                                                       # 옛 모양 기록(글 값)도 '알린 주문'으로 읽는다
    state = lotto.load_state()
    state["anomaly_seen"] = {f"{L}:{LOTTO_ROUND}:HAND0001": "2026-11-16T10:00:00+09:00"}
    lotto.save_state(state)
    assert lotto.order_anomalies([hand]) == []


def test_ledger_longer_than_one_page_is_unreadable():
    parse = raw_client()._parse_buy_list_json
    assert not lotto.unknown_rows(parse({"data": {"list": REAL_PENSION_ROWS, "total": 5}}))
    assert not lotto.unknown_rows(parse({"data": {"list": REAL_PENSION_ROWS}}))                # 전체 줄 수를 안 줘도 한 쪽이 안 찼으면 다 읽은 것
    assert not lotto.unknown_rows(parse({"data": {"list": REAL_PENSION_ROWS, "total": 100}}))  # 딱 100줄은 한 쪽 안
    over = lotto.unknown_rows(parse({"data": {"list": REAL_PENSION_ROWS, "total": 101}}))
    assert len(over) == 1 and "다 못 읽음" in over[0]["name"], over
    assert lotto.unknown_rows(parse({"data": {"list": REAL_PENSION_ROWS * 20}}))               # 전체 줄 수 없이 100줄이 꽉 참 → 더 있는지 모름
    assert not lotto.unknown_rows(parse({"data": {"list": (REAL_PENSION_ROWS * 20)[:99]}}))
    reset()
    assert raises(lotto.CapError, lotto.round_have, over, [], LOTTO_ROUND, [])                 # buy 는 안 산다


def test_deposit_increase_goes_on_next_purchase_ping():
    reset(TUE)
    set_baseline(30000)
    captured(run_buy, [], balance=80000, ack=False)               # 예상보다 5만원 많음 (충전) → 그 실행의 구매 알림 끝에 한 줄
    assert sends[0][0].splitlines()[-1] == "참고: 예치금이 예상보다 5만원 늘어 있었어요 (충전·당첨금 입금 — 직접 넣은 게 아니면 확인하세요)", sends
    assert "deposit_up" not in lotto.load_state()                 # 보냈으면 지운다
    reset(TUE)
    set_baseline(30000)
    lotto.channel.send = lambda text, silent=None: sends.append((text, silent)) and False         # 알림이 안 나가면 다음 구매 알림으로 넘긴다
    captured(run_buy, [], balance=80000, ack=False)
    assert lotto.load_state()["deposit_up"] == 50000
    reset(TUE)                                                    # 늘어난 게 없으면 줄도 없다
    set_baseline(30000)
    captured(run_buy, [], balance=30000, ack=False)
    assert "참고" not in sends[0][0], sends


def test_not_bought_this_week_alert():
    sat = datetime(2026, 11, 21, 21, 30, tzinfo=KST)
    this_round = lotto.latest_drawn_round(sat)

    def check(enabled: bool, lotto_games: int, seen_before: bool = True) -> None:
        reset(sat)
        lotto.buy_job_enabled = lambda: enabled
        if seen_before:                                           # 지난주에도 이 점검이 돌았다 (기준 회차가 적혀 있다)
            state = lotto.load_state()
            state["not_bought_told"] = {L: this_round - 1, P: PENSION_ROUND - 1}
            lotto.save_state(state)
        rows = [pension_order(PENSION_ROUND, number="111111")] + ([lotto_order(this_round, lotto_games)] if lotto_games else [])
        lotto.upsert_orders(rows)
        pension720.fetch_winning = lambda session=None: {PENSION_ROUND: WIN}
        lotto.fetch_orders = lambda client, capture, days: [dict(r, result="낙첨") if r["game"] == L else r for r in rows]
        lotto.read_balance = lambda client, capture, buying=True: 5000
        captured(lotto.cmd_check)

    check(enabled=True, lotto_games=0)                            # PC 는 켜져 있는데 이번 주 로또를 한 게임도 안 샀다
    assert len(pings) == 1 and pings[0][0][2] == "이번 주 안 샀어" and f"로또 {this_round}회" in pings[0][0][3], pings
    assert "연금" not in pings[0][0][3]                           # 연금은 샀다
    pings.clear()
    captured(lotto.cmd_check)                                     # 회차당 한 번
    assert not pings, pings
    check(enabled=False, lotto_games=0)                           # lotto-buy 를 꺼 둔 동안엔 안 사는 게 정상
    assert not pings, pings
    check(enabled=True, lotto_games=1)                            # 한 게임이라도 샀으면 조용
    assert not pings, pings
    check(enabled=True, lotto_games=0, seen_before=False)         # 처음 보는 주(켜기 전 회차)는 기준만 적고 알리지 않는다
    assert not pings and lotto.load_state()["not_bought_told"] == {L: this_round, P: PENSION_ROUND}, pings
    reset()
    assert ORIGINAL["buy_job_enabled"]() is True                  # 테스트 설정: 자동 구매 켜짐
    settings.config_path().unlink()
    assert ORIGINAL["buy_job_enabled"]() is False                 # 설정이 없으면 꺼진 걸로
    settings.config_path().write_text("{broken", encoding="utf-8")
    assert ORIGINAL["buy_job_enabled"]() is False                 # 못 읽어도 꺼진 걸로
    write_config()


SOFT_HEAD = "🚨 동행복권 예치금 확인 — {} · 내 구매일 수도 있어요 (예약 실행 등) — 구매내역에 없으면 비밀번호를 바꾸세요"


def test_reserved_round_opening_is_explained_and_balance_wording_split():
    nxt = datetime(2026, 11, 23, 10, 0, tzinfo=KST)               # 다음 주 월요일 — 예약한 343회 판매가 열려(11-19) 구매내역에 5장이 떴다

    def week_after_reservation(balance: int) -> dict:
        reset(nxt, reserved=False)
        put_attempt("ok", before=35000, after=10000)              # 지난주에 343-347회를 예약했고 그때 2만 5,000원이 나갔다
        state = lotto.load_state()
        state["last_balance"] = {"ts": "2026-11-16T10:05:00+09:00", "amount": 10000}           # 예약 뒤 예치금이 기준
        state["spend_log"] = []
        lotto.save_state(state)
        return captured(run_buy, [pension_order(343, no="LP72-RSV343")], balance=balance, ack=False)[1]

    last = week_after_reservation(10000)                          # 예치금 그대로 → 주문도 잔액도 이상 아님, 평소대로 로또를 산다
    assert last["ok"] is True and ("execBuy", 3) in calls and not any("🚨" in t for t, _ in sends) and not pings, (last, sends, pings)
    assert not paid("pension") and not paid("reserve"), calls     # 예약분 회차를 또 사지 않는다
    last = week_after_reservation(5000)                           # 열린 예약 1회차 값(5,000원)만큼 줄었음 → '내 구매일 수도'
    assert "skipped" in last and sends[0][0].splitlines()[0] == SOFT_HEAD.format("예치금 5,000원 줄었어"), sends
    assert "판매가 열린 내 예약 1회차" in sends[0][0] and "털림" not in sends[0][0], sends
    last = week_after_reservation(4000)                           # 그보다 1,000원 더 줄었음 → '털림 의심'
    assert "skipped" in last and sends[0][0].splitlines()[0] == HEAD.format("예치금 6,000원 이 설명 안 됨 — 털림 의심"), sends


def test_lotto_round_checked_against_site_before_buying():
    reset()
    lotto.site_lotto_round = lambda: LOTTO_ROUND + 1              # 사이트 회차가 계산과 다름 (휴무로 밀린 주 등)
    _, last = captured(run_buy, [pension_order(PENSION_ROUND)])
    assert not paid("execBuy") and lotto.lotto_attempted_games(LOTTO_ROUND) == 0 and last["ok"] is False, (calls, last)
    assert pings[-1][0][2] == "로또 회차 불일치 — 안 삼" and f"사이트 {LOTTO_ROUND + 1}회" in pings[-1][0][3], pings[-1]
    reset()

    def down() -> int:
        raise RuntimeError("timeout")

    lotto.site_lotto_round = down                                 # 회차를 못 읽어도 안 산다
    captured(run_buy, [pension_order(PENSION_ROUND)])
    assert not paid("execBuy") and pings[-1][0][2] == "로또 회차 확인 실패 — 안 삼" and "돈 안 나감" in pings[-1][0][3], pings[-1]
    for site_round in (lambda: LOTTO_ROUND + 1, down):            # 할 일이 로또뿐이면 로그인 전에 대조 — 어긋난 주에 칸마다 로그인하지 않는다
        reset()
        lotto.upsert_orders([pension_order(PENSION_ROUND)])       # 연금은 끝남 → 남은 건 로또
        lotto.site_lotto_round = site_round
        for _ in range(2):                                        # 같은 날 두 칸 다
            _, last = captured(run_buy, [pension_order(PENSION_ROUND)])
            assert "login" not in calls and "fetch" not in calls and not paid("execBuy"), calls
            assert last["ok"] is False and "로그인 안 함" in last["error"] and "로또 회차" in last["error"], last
        assert len(pings) == 1 and pings[0][0][2].startswith("로또 회차"), pings                # 경보는 하루 1번
    reset()                                                       # 회차가 맞으면 평소대로 로그인해서 산다
    lotto.upsert_orders([pension_order(PENSION_ROUND)])
    captured(run_buy, [pension_order(PENSION_ROUND)])
    assert "login" in calls and paid("execBuy") == [("execBuy", 3)], calls
    reset()
    lotto.DRY = True                                              # dry-run 도 회차를 대조한다 (읽기)
    lotto.site_lotto_round = lambda: LOTTO_ROUND + 1
    text, _ = captured(run_buy, [pension_order(PENSION_ROUND)])
    assert pings[-1][0][2] == "로또 회차 불일치 — 안 삼" and "(사이트 회차와 같음)" not in text, (pings, text)
    import requests

    class Reply:
        def __init__(self, rows):
            self.rows = rows

        def json(self):
            return {"resultCode": None, "data": {"list": self.rows}}

    real_get = requests.get
    try:                                                          # 진짜 함수: 마지막 추첨 회차 + 1 (2026-11-16 실측 응답 모양)
        requests.get = lambda url, **kw: Reply([{"ltEpsd": 1250, "ltRflYmd": "20261114"}])
        assert ORIGINAL["site_lotto_round"]() == 1251
        requests.get = lambda url, **kw: Reply([])
        assert raises(RuntimeError, ORIGINAL["site_lotto_round"])
    finally:
        requests.get = real_get


def test_order_numbers_are_shown_by_last_4_only():
    reset()
    bad = lotto.unknown_order({"ntslOrdrNo": "202611160009876"})   # 이름도 없는 못 읽은 줄
    assert lotto.row_label(bad) == "주문번호 끝 9876"
    assert lotto.row_label(lotto.unknown_order({"ltGdsNm": "로또6/45", "ntslOrdrNo": "202611160009876"})) == "로또6/45"
    _, last = captured(run_buy, [bad])
    assert "202611160009876" not in json.dumps(last, ensure_ascii=False) + str(pings) and "끝 9876" in pings[-1][0][3], (last, pings)
    game = ORIGINAL_GAME(session=None, dry=False)                 # 예약 응답의 주문번호도 끝 4자리만 돌려준다
    game._step = lambda path, fields: {"resultCode": "100", "doubleRound": [], "reserveOrderNo": "R2026111600001234"}
    assert game.reserve(342, NEXT5, ["x"] * 5, 30000, lambda: None)["order_no"] == "1234"


# ---------- 어댑터 (LOTTO_BACKEND — local 기본 · actions = gist·env·웹훅) ----------
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    lotto.db.init()
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} 통과")
    sys.exit(1 if failed else 0)

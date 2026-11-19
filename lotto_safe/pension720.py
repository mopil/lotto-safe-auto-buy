"""연금복권720+ — 동행복권 el 게임 서버 연결 (lotto.py 가 부른다).

dhapi 는 연금복권을 못 산다. 구매 흐름은 두 곳을 맞춰 봐서 정했다 (2026-11-16):
- 실제 페이지 el.dhlottery.co.kr/game/pension720/game.jsp + /lotto/js/crypt/encrypt.js — 2026-01 개편 뒤 지금 것
- umi-corp/autolotto720 PurchaseService720.kt — 325회 '모든 조 세트' 실구매로 확인된 요청 모양 (그대로 따른다)

흐름 (전부 POST, 본문은 암호문 q= 하나):
  게임 페이지 GET → el JSESSIONID 발급 (암호 키)
  roundRemainTime.do (평문)  판매 중인 회차·판매 여부 — 읽기
  makeAutoNo.do  SA          모든 조 공통 6자리 번호 하나 배정 — 돈 안 나감
  makeOrderNo.do SA·5장      주문번호 예약 — 돈 안 나감 (그래도 dry-run 금지)
  connPro.do     SA·5장      실결제 5,000원. 같은 요청을 두 번 보내면 두 번 산다 → 재시도 절대 금지
예약 (공식 예약구매. reserveGame.jsp 를 읽어서 맞춤 — ⚠️ 실제 예약은 아직 검증 전, '① 설정' 에서 실험 기능으로 표시):
  mypage selectRstvPrchsDsctnList.do (GET)  예약 내역 — 읽기
  checkMyReserve.do  예약 폼              이미 예약된 회차가 겹치나 — 돈 안 나감 (dry-run 에선 안 보냄)
  addMyReserve.do    예약 폼 + winDate    다음 회차부터 연속 N회차 모든 조 1세트씩 예약 = 그 자리에서 예치금 전액 차감. 재시도 절대 금지
  사이트는 이미 예약한 회차를 또 예약해도 안 막는다 (확인 창만) → 겹침은 이쪽에서 막는다.
암호: 키 = PBKDF2-SHA256(JSESSIONID 앞 32자, 랜덤 salt 32B, 1000회, 16B) · AES-CBC-PKCS7 · q = hex(salt)+hex(iv)+base64(암호문).
응답 {"q": 암호문} 을 같은 키로 풀면 JSON.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import date, timedelta
from urllib.parse import quote, urlencode

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

EL = "https://el.dhlottery.co.kr"
GAME_ENTRY = EL + "/game/TotalGame.jsp?LottoId=LP72"
GAME_PAGE = EL + "/game/pension720/game.jsp"
WINNING_LIST = "https://www.dhlottery.co.kr/pt720/selectPstPt720WnList.do"   # 로그인 없이 전 회차 당첨번호
RESERVE_PAGE = EL + "/game/pension720/reserveGame.jsp"
RESERVE_LIST_PAGE = "https://www.dhlottery.co.kr/mypage/rstvPrchsDsctnView"
RESERVE_LIST = "https://www.dhlottery.co.kr/mypage/selectRstvPrchsDsctnList.do"   # GET — 예약구매 내역 (읽기)
# dry-run 에선 절대 안 부른다 (_step 이 막는다). 돈 나가는 경로 + 예약 취소(cancelMyReserve — 이 코드는 어디서도 안 부른다)
# + checkMyReserve(조회지만 예약 폼 전체를 보내는 요청이라 dry-run 에선 같이 막는다)
PAYMENT_PATHS = {"/makeOrderNo.do", "/connPro.do", "/addMyReserve.do", "/cancelMyReserve.do", "/checkMyReserve.do"}
# el 게임 서버는 데스크톱 UA 전제 — 모바일 UA 면 모바일 페이지로 보낸다
DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
TIMEOUT = 15

PENSION_CODE = "LP72"                  # 구매내역 ltGdsCd
PENSION_NAME = "연금복권720+"
FIRST_DRAW = date(2020, 5, 7)          # 1회 추첨 (목) — 341회 = 2026-11-12 로 맞춰 봄
GROUPS = (1, 2, 3, 4, 5)               # 조
SET_PRICE = 5000                       # 모든 조 세트 5장
SUCCESS = "100"                        # connPro 완료 코드. 110(부분)·120(살 표 없음)·그 밖은 전부 '결과 불명'으로 본다
RESERVE_MAX_ROUNDS = 5                 # 예약은 다음 회차부터 연속 최대 5회차 (reserveGame.jsp initSelect)
RESERVE_LIST_PAGE_SIZE = 10            # 사이트 화면과 같은 쪽 크기
RESERVE_LIST_MAX_PAGES = 5             # 이보다 많으면 못 센 걸로 본다 (예약·구매 안 함)
RESERVE_CANCELLED_CODES = {"3", "5"}   # 예약취소 상태 코드 (reserveGame.jsp) — 그 밖(예약중 1·4, 구매완료 2, 모르는 코드)은 전부 '그 회차 있음'

# 등수별 당첨금 (세전, 연금식은 총액으로 환산 — DB 기록용. 손익엔 연금식을 안 넣는다: lotto.pension_money). 1등 월 700만×20년 · 2등·보너스 월 100만×10년
ANNUITY = {"1등": (7_000_000, 20), "2등": (1_000_000, 10), "보너스": (1_000_000, 10)}   # 등수: (월 지급액, 년) — 동행복권 공식 안내
PRIZE = {**{r: monthly * 12 * years for r, (monthly, years) in ANNUITY.items()},
         "3등": 1_000_000, "4등": 100_000, "5등": 50_000, "6등": 5_000, "7등": 1_000}
SUFFIX_RANKS = ((5, "3등"), (4, "4등"), (3, "5등"), (2, "6등"), (1, "7등"))   # 1등 번호 끝 k자리 일치


class PensionError(RuntimeError):
    """연금복권 서버가 거절했거나 응답을 못 읽음."""


# ---------- 회차 ----------
def round_of_draw(draw_day: date) -> int:
    return 1 + (draw_day - FIRST_DRAW).days // 7


def draw_date_of(round_no: int) -> date:
    return FIRST_DRAW + timedelta(days=7 * (round_no - 1))


# ---------- 암호 ----------
def _key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase[:32].encode(), salt, 1000, 16)


def encrypt(plain: str, passphrase: str) -> str:
    salt, iv = os.urandom(32), os.urandom(16)
    cipher = AES.new(_key(passphrase, salt), AES.MODE_CBC, iv).encrypt(pad(plain.encode("utf-8"), 16))
    return salt.hex() + iv.hex() + base64.b64encode(cipher).decode()


def decrypt(text: str, passphrase: str) -> str:
    salt, iv, body = bytes.fromhex(text[:64]), bytes.fromhex(text[64:96]), base64.b64decode(text[96:])
    return unpad(AES.new(_key(passphrase, salt), AES.MODE_CBC, iv).decrypt(body), 16).decode("utf-8")


def _site_url_encode(text: str) -> str:
    """encrypt.js urlEncode — '.'(0x2E)-'~'(0x7E) 와 영숫자·_ 는 그대로, 공백은 +, 나머지는 %XX."""
    out = []
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch == "_" or "." <= ch <= "~"):
            out.append(ch)
        elif ch == " ":
            out.append("+")
        else:
            out.append(f"%{ord(ch):02X}")
    return "".join(out)


def wire_q(q: str) -> str:
    """브라우저가 보내는 모양 그대로: encrypt.js urlEncode 뒤 jQuery serialize(encodeURIComponent) 한 번 더."""
    return quote(_site_url_encode(q), safe="-_.!~*'()")


def as_dict(value) -> dict | None:
    """응답 조각을 dict 로. 이미 dict 면 그대로, JSON 글이면 풀어서, 그 밖(빈 값·숫자·깨진 글·JSON 배열)은 None."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------- 게임 서버 ----------
class PensionGame:
    """로그인된 dhapi requests 세션을 빌려 쓴다. dry=True 면 돈 나가는 경로(PAYMENT_PATHS)는 부르면 예외."""

    def __init__(self, session: requests.Session, dry: bool):
        self.session = session
        self.dry = dry
        self.user_id = ""
        self._jsid = ""
        self._referer = GAME_PAGE

    def _el_jsessionid(self) -> str:
        for c in self.session.cookies:
            if c.name == "JSESSIONID" and c.domain.endswith("el.dhlottery.co.kr"):
                return c.value
        return ""

    def _pin_jsessionid(self) -> str:
        """암호 키 = el JSESSIONID. 구매 도중 서버가 바꿔도 주문·결제가 같은 세션에 묶이게 처음 값으로 고정한다."""
        if self._el_jsessionid() != self._jsid:
            self.session.cookies.set("JSESSIONID", self._jsid, domain="el.dhlottery.co.kr", path="/")
        return self._jsid

    def open(self) -> None:
        """게임 페이지를 열어 el 세션과 USER_ID(결제 폼 값)를 받는다."""
        self.session.get(GAME_ENTRY, headers={"User-Agent": DESKTOP_UA}, timeout=TIMEOUT)
        html = self.session.get(GAME_PAGE, headers={"User-Agent": DESKTOP_UA, "Referer": GAME_ENTRY}, timeout=TIMEOUT).text
        found = re.search(r'name="USER_ID"\s+value="([^"]*)"', html)
        self.user_id = found.group(1) if found else ""
        self._jsid = self._el_jsessionid()
        if not self._jsid or not self.user_id:
            raise PensionError("연금복권 게임 세션을 못 열었어요 (로그인 풀림·점검 중일 수 있음)")

    def _headers(self) -> dict:
        return {"User-Agent": DESKTOP_UA, "X-Requested-With": "XMLHttpRequest", "Referer": self._referer, "Origin": EL,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}

    def _step(self, path: str, fields: list[tuple[str, str]]) -> dict:
        """암호화 POST 한 번 → 풀어낸 JSON. 응답이 JSON 이 아니거나 못 풀면 PensionError (connPro 에선 '결과 불명')."""
        # dry-run 은 돈 나가는 경로를 절대 안 부른다 — 코드가 잘못 와도 여기서 멈춘다
        if self.dry and path in PAYMENT_PATHS:
            raise AssertionError(f"dry-run 인데 {path} 를 부르려 함")
        passphrase = self._pin_jsessionid()
        body = "q=" + wire_q(encrypt(urlencode(fields), passphrase))
        resp = self.session.post(EL + path, data=body, headers=self._headers(), timeout=TIMEOUT)
        try:
            outer = resp.json()
        except ValueError as e:
            raise PensionError(f"{path} 응답이 JSON 아님 (HTTP {resp.status_code})") from e
        if not isinstance(outer, dict):
            raise PensionError(f"{path} 응답이 JSON 객체 아님 (HTTP {resp.status_code})")
        if not outer.get("q"):
            raise PensionError(f"{path} 거절: {outer.get('resultCode')} {outer.get('resultMsg')}")
        try:
            data = as_dict(decrypt(outer["q"], passphrase))
        except (ValueError, KeyError, TypeError) as e:
            raise PensionError(f"{path} 응답을 못 풀었어요") from e
        if data is None:
            raise PensionError(f"{path} 풀어낸 응답이 JSON 객체가 아님")
        return data

    def round_info(self) -> dict:
        """지금 판매 중인 회차 {'ROUND': '342', 'IS_SALE_FLAG': 'true', 'CLOSE_DATE_TIME': ...} — 평문 POST, 읽기만."""
        form = urlencode([("ROUND", ""), ("SEL_NO", ""), ("BUY_CNT", ""), ("AUTO_SEL_SET", ""),
                          ("SEL_CLASS", ""), ("BUY_TYPE", "A"), ("ACCS_TYPE", "01")])
        resp = self.session.post(EL + "/roundRemainTime.do", data=form, headers=self._headers(), timeout=TIMEOUT)
        data = resp.json()
        if data.get("resultCode") != SUCCESS:
            raise PensionError(f"회차 조회 실패: {data.get('resultCode')}")
        return data

    def assign_set_number(self, round_no: int) -> str:
        """모든 조 공통 6자리 번호 하나를 서버가 배정 (돈 안 나감)."""
        data = self._step("/makeAutoNo.do", _auto_form(str(round_no), "", ""))
        if data.get("resultCode") != SUCCESS or str(data.get("round")) != str(round_no):
            raise PensionError(f"번호 배정 실패: {data.get('resultCode')} {data.get('resultMsg')}")
        number = str(data.get("selLotNo") or "").split(",")[0]
        if not re.fullmatch(r"\d{6}", number):
            raise PensionError(f"배정 번호 형식 이상: {number!r}")
        return number

    def buy_set(self, round_no: int, number: str, before_payment) -> dict:
        """주문 예약 → before_payment() → 결제 한 번. 결제는 재시도하지 않는다.
        before_payment 는 '이 회차 결제 시도함' 기록 — 실패하면(예외) 결제 안 하고 끝난다.
        돌려주는 것: {'code': connPro 결과 코드, 'tickets': [(조, 번호)…] (응답에서 읽은 것, 못 읽으면 빈 목록)}."""
        order = self._step("/makeOrderNo.do", _auto_form(str(round_no), number, "5"))
        if order.get("resultCode") != SUCCESS or not order.get("orderNo") or not order.get("orderDate"):
            raise PensionError(f"주문 예약 실패 (돈 안 나감): {order.get('resultCode')} {order.get('resultMsg')}")
        before_payment()
        paid = self._step("/connPro.do", _payment_form(round_no, number, order["orderNo"], order["orderDate"], self.user_id))
        return {"code": str(paid.get("resultCode")), "tickets": _tickets_from_payment(paid, number), "msg": short_text(paid.get("resultMsg"))}

    def open_reserve(self) -> None:
        """예약 페이지를 한 번 연다 (GET — 브라우저와 같은 순서). 그 뒤 요청의 Referer 는 예약 페이지."""
        self.session.get(RESERVE_PAGE, headers={"User-Agent": DESKTOP_UA, "Referer": GAME_PAGE}, timeout=TIMEOUT)
        self._referer = RESERVE_PAGE

    def reserve(self, cur_round: int, rounds: list[int], win_dates: list[str], deposit: int, before_payment) -> dict:
        """겹침 확인 → before_payment() → 예약 결제 한 번. 결제는 재시도하지 않는다.
        before_payment 는 '이 회차들 예약 시도함' 기록 — 실패하면(예외) 결제 안 하고 끝난다.
        겹침 확인이 실패하거나 겹치는 회차가 있으면 PensionError (결제 요청 전이라 돈 안 나감).
        돌려주는 것: {'code': addMyReserve 결과 코드, 'order_no': 예약 주문번호 끝 4자리, 'msg': 짧은 글}."""
        check = self._step("/checkMyReserve.do", _reserve_form(cur_round, rounds, [], deposit))
        doubles = check.get("doubleRound")
        if str(check.get("resultCode")) != SUCCESS or not isinstance(doubles, list):
            raise PensionError(f"예약 겹침 확인 실패: {short_text(check.get('resultCode'))}")
        if doubles:
            raise PensionError(f"이미 예약된 회차가 겹침 ({len(doubles)}개 회차)")
        before_payment()
        paid = self._step("/addMyReserve.do", _reserve_form(cur_round, rounds, win_dates, deposit))
        return {"code": str(paid.get("resultCode")), "order_no": short_text(paid.get("reserveOrderNo"))[-4:],   # 끝 4자리만 — 주문번호 전체는 출력·알림에 안 싣는다
                "msg": short_text(paid.get("resultMsg"))}


def _auto_form(round_no: str, number: str, count: str) -> list[tuple[str, str]]:
    """frmauto — 모든 조(SA) · 자동(A) · 인터넷(01)."""
    return [("ROUND", round_no), ("SEL_NO", number), ("BUY_CNT", count), ("AUTO_SEL_SET", "SA"),
            ("SEL_CLASS", ""), ("BUY_TYPE", "A"), ("ACCS_TYPE", "01")]


def _payment_form(round_no: int, number: str, order_no: str, order_date: str, user_id: str) -> list[tuple[str, str]]:
    """frm — 브라우저 폼 순서 그대로. 값은 autolotto720 이 실구매로 확인한 것 (BUY_TYPE 끝 쉼표 포함)."""
    fields = [
        ("ROUND", str(round_no)), ("FLAG", ""), ("BUY_KIND", "01"),
        ("BUY_NO", ",".join(f"{g}{number}" for g in GROUPS)), ("BUY_CNT", str(len(GROUPS))),
        ("BUY_SET_TYPE", ",".join("SA" for _ in GROUPS)), ("BUY_TYPE", "A,A,A,A,A,"), ("ACCS_TYPE", "01"),
        ("orderNo", order_no), ("orderDate", order_date), ("TRANSACTION_ID", ""), ("WIN_DATE", ""),
        ("USER_ID", user_id), ("PAY_TYPE", ""),
        ("resultErrorCode", ""), ("resultErrorMsg", ""), ("resultOrderNo", ""),
        ("WORKING_FLAG", "false"), ("NUM_CHANGE_TYPE", ""),
        ("auto_process", "Y"), ("set_type", "SA"), ("classnum", ""), ("selnum", number), ("buytype", "A"),
    ]
    fields += [(f"num{i + 1}", digit) for i, digit in enumerate(number)]
    fields += [("DSEC", "0"), ("CLOSE_DATE", ""), ("verifyYN", "N"), ("curdeposit", "0"), ("curpay", str(SET_PRICE))]
    return fields


def short_text(value, limit: int = 60) -> str:
    """서버가 준 값을 알림·브리프에 넣을 짧은 글로. 글·숫자만 그대로(잘라서), dict·list 같은 덩어리는 종류 이름만."""
    if value is None:
        return ""
    if isinstance(value, (str, int)):
        return " ".join(str(value).split())[:limit]
    return f"({type(value).__name__})"


def reserve_draw_dates(info: dict, cur_round: int, rounds: list[int]) -> list[str]:
    """예약할 회차들의 추첨일 'YYYY.MM.DD' (addMyReserve winDate). 사이트 회차 정보의 이번 회차 추첨일(DRAW_DATE)에서 7일씩 —
    브라우저(initSelect)와 같은 계산. 날짜 공식(draw_date_of)과 다르거나 연속 회차가 아니면 PensionError (휴무로 밀린 주 등 — 예약 안 함)."""
    # 1 <= … <= 5 : 사이트가 받는 범위 그대로 (딱 5회차까지 허용)
    if not 1 <= len(rounds) <= RESERVE_MAX_ROUNDS or rounds != list(range(cur_round + 1, cur_round + 1 + len(rounds))):
        raise PensionError(f"예약 회차가 다음 회차부터 연속 {RESERVE_MAX_ROUNDS}개 이내가 아님: {rounds}")
    try:
        cur_draw = date.fromisoformat(str(info.get("DRAW_DATE") or "")[:10])
    except ValueError as e:
        raise PensionError("사이트 회차 정보에 추첨일(DRAW_DATE)이 없음") from e
    if cur_draw != draw_date_of(cur_round):
        raise PensionError(f"추첨일 불일치: 사이트 {cur_draw} · 계산 {draw_date_of(cur_round)}")
    return [(cur_draw + timedelta(days=7 * (r - cur_round))).strftime("%Y.%m.%d") for r in rounds]


def _reserve_form(cur_round: int, rounds: list[int], win_dates: list[str], deposit: int) -> list[tuple[str, str]]:
    """reserveGame.jsp 의 frm — 브라우저 폼 순서 그대로. 모든 조(reserveJo 0 · repeatClass 5) 1매씩 = 회차당 세트 1개.
    겹침 확인(checkMyReserve) 땐 winDate 가 빈 값, 예약(addMyReserve) 땐 회차별 추첨일을 쉼표로 (브라우저와 같게)."""
    count = len(rounds)
    amount = count * SET_PRICE
    first_draw = draw_date_of(rounds[0]).strftime("%Y.%m.%d")
    return [
        ("ROUND", str(cur_round)), ("reserveJo", "0"), ("repeatRoundCnt", str(count)),
        ("totalBuyAmt", str(amount)), ("totalBuyCnt", str(count * len(GROUPS))),
        ("moneyBalance", "0"), ("couponBalance", "0"), ("nextRound", str(cur_round + 1)),
        ("repeatClass", str(len(GROUPS))), ("roundBuyCnt", "1"),
        ("curdeposit", str(max(int(deposit), 0))), ("curpay", str(amount)),
        ("winDate", ",".join(win_dates)), ("WORKING_FLAG", "false"),
        ("repeatRound", str(count)), ("repeatRoundHidden", first_draw),
    ]


def _tickets_from_payment(paid: dict, number: str) -> list[tuple[int, str]]:
    """결제 응답의 prchsLtNoInfoLstCn ('번호|주문|일련|회차|조' 를 쉼표로 이음) — top-level data 또는 resultMsg 안 JSON."""
    # 2026-11-16 첫 실구매: resultMsg 가 글이 아니라 이미 풀린 dict 로 와서 json.loads 가 TypeError → '결과 불명'으로 오판.
    # data·resultMsg 둘 다 dict·JSON 글·그 밖 무엇이 와도 여기서 예외가 안 나게 as_dict 로만 읽는다
    raw = (as_dict(paid.get("data")) or {}).get("prchsLtNoInfoLstCn")
    if not raw:
        inner = as_dict(paid.get("resultMsg")) or {}
        raw = (as_dict(inner.get("data")) or {}).get("prchsLtNoInfoLstCn")
    if not isinstance(raw, str):
        raw = ""
    tickets = []
    for row in re.split(r"[,;\n]", raw or ""):
        parts = row.strip().split("|")
        if len(parts) >= 5 and parts[0] == number and parts[4].isdigit():
            tickets.append((int(parts[4]), parts[0]))
    return tickets


# ---------- 구매내역 · 당첨 ----------
def is_pension_item(item: dict) -> bool:
    return item.get("ltGdsCd") == PENSION_CODE or "연금복권" in str(item.get("ltGdsNm") or "")


def tickets_of(gm_info) -> list[tuple[int, str]]:
    """구매내역 gmInfo '1:123456,2:123456' → [(1, '123456'), …]. 이상한 조각은 버린다."""
    tickets = []
    for piece in str(gm_info or "").split(","):
        group, _, number = piece.partition(":")
        group, number = group.strip(), number.strip()
        if group.isdigit() and int(group) in GROUPS and re.fullmatch(r"\d{6}", number):
            tickets.append((int(group), number))
    return tickets


def format_tickets(tickets: list[tuple[int, str]]) -> str:
    return "\n".join(f"{g}조 {n}" for g, n in sorted(tickets))


def compact_tickets(tickets: list[tuple[int, str]]) -> str:
    """알림용 짧은 글. 모든 조 세트(1-5조 같은 번호)면 '1-5조 845545', 아니면 '1조 111111, 3조 222222'."""
    tickets = sorted(set(tickets))
    numbers = {n for _, n in tickets}
    groups = [g for g, _ in tickets]
    if len(numbers) == 1 and len(groups) > 1 and groups == list(range(groups[0], groups[-1] + 1)):
        return f"{groups[0]}-{groups[-1]}조 {numbers.pop()}"
    return ", ".join(f"{g}조 {n}" for g, n in tickets)


def parse_tickets(text: str | None) -> list[tuple[int, str]]:
    """format_tickets 로 저장한 글 → [(조, 번호)]."""
    found = re.findall(r"(\d)조 (\d{6})", text or "")
    return [(int(g), n) for g, n in found]


def parse_reservations(payload) -> tuple[list[dict], int, int]:
    """예약구매 내역 응답 한 쪽 → ([{'round', 'qty', 'status', 'cancelled'}…], 전체 줄 수, 이 쪽 줄 수). 연금복권 줄만.
    회차·매수를 못 읽는 줄이 하나라도 있으면 PensionError — 부르는 쪽은 연금을 사지도 예약하지도 않는다 (fail-closed)."""
    if not isinstance(payload, dict) or payload.get("resultCode"):
        raise PensionError("예약 내역 응답 오류" + (f": {short_text(payload.get('resultCode'))}" if isinstance(payload, dict) else ""))
    data = payload.get("data")
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise PensionError("예약 내역 응답에 목록이 없음")
    rows = []
    for item in items:
        if not isinstance(item, dict):
            raise PensionError("예약 내역에 못 읽는 줄")
        if not is_pension_item(item):
            # 다른 복권(파워볼 등)임이 확실한 줄만 건너뛴다. 무슨 복권인지 모르는 줄은 못 읽은 걸로
            if item.get("ltGdsCd") or item.get("ltGdsNm"):
                continue
            raise PensionError("예약 내역에 복권 종류를 모르는 줄")
        round_digits = re.sub(r"\D", "", str(item.get("ltEpsd") or ""))
        try:
            qty = int(item.get("rsvtOrdrQty"))
        except (TypeError, ValueError):
            qty = 0
        # <= 0 : 매수 0 도 못 읽은 걸로 본다 (있는 예약을 없는 걸로 세지 않게)
        if not round_digits or qty <= 0:
            raise PensionError("예약 내역 줄의 회차·매수를 못 읽음")
        status = str(item.get("rsvtPrchsSttsCd") or "").strip()
        rows.append({"round": int(round_digits), "qty": qty, "status": status, "cancelled": status in RESERVE_CANCELLED_CODES})
    try:
        total = int(data.get("total") or 0)
    except (TypeError, ValueError) as e:
        raise PensionError("예약 내역 전체 줄 수를 못 읽음") from e
    return rows, total, len(items)


def fetch_reservations(session: requests.Session, start: date, end: date) -> list[dict]:
    """예약일 start-end 사이의 연금복권 예약구매 내역 전부 (GET 만 — 읽기). 쪽을 넘겨 가며 다 읽고, 다 못 읽으면 PensionError."""
    headers = {"User-Agent": DESKTOP_UA, "X-Requested-With": "XMLHttpRequest", "AJAX": "true",
               "requestMenuUri": "/mypage/rstvPrchsDsctnView", "Accept": "application/json, text/javascript, */*; q=0.01",
               "Referer": RESERVE_LIST_PAGE}
    session.get(RESERVE_LIST_PAGE, headers={"User-Agent": DESKTOP_UA}, timeout=TIMEOUT)
    rows: list[dict] = []
    for page in range(1, RESERVE_LIST_MAX_PAGES + 1):
        params = {"srchStrDt": start.strftime("%Y%m%d"), "srchEndDt": end.strftime("%Y%m%d"), "srchLtGdsCdArr": PENSION_CODE,
                  "ordrCd": "LATEST", "srchRsvtPrchsSttsCd": "", "pageNum": page, "recordCountPerPage": RESERVE_LIST_PAGE_SIZE}
        resp = session.get(RESERVE_LIST, params=params, headers=headers, timeout=TIMEOUT)
        if resp.status_code != 200:
            raise PensionError(f"예약 내역 조회 실패 (HTTP {resp.status_code})")
        try:
            payload = resp.json()
        except ValueError as e:
            raise PensionError("예약 내역 응답이 JSON 아님 (로그인 풀림일 수 있음)") from e
        page_rows, total, raw_count = parse_reservations(payload)
        rows += page_rows
        if raw_count == 0:   # 빈 쪽 — 더 없음
            return rows
        if total > 0:
            # >= : 이 쪽까지 읽은 줄 수가 전체 이상이면 끝
            if page * RESERVE_LIST_PAGE_SIZE >= total:
                return rows
        # 전체 줄 수를 안 주면(없음·0) 그 값을 안 믿는다 — 쪽이 꽉 찼으면(>= 쪽 크기) 더 있을 수 있으니 다음 쪽으로,
        # 덜 찬 쪽이면 거기가 끝 (lotto.py 구매내역과 같은 원칙 — 다 읽었다고 추측하지 않는다)
        elif raw_count < RESERVE_LIST_PAGE_SIZE:
            return rows
    raise PensionError(f"예약 내역이 {RESERVE_LIST_MAX_PAGES}쪽을 넘음 — 다 못 읽음")


def fetch_winning(session: requests.Session | None = None) -> dict[int, dict]:
    """전 회차 당첨번호 {회차: {'group': 3, 'number': '194929', 'bonus': '783820', 'date': '2026-11-12'}} — 로그인 필요 없음."""
    http = session or requests.Session()
    resp = http.get(WINNING_LIST, headers={"User-Agent": DESKTOP_UA, "X-Requested-With": "XMLHttpRequest",
                                           "Accept": "application/json"}, timeout=TIMEOUT)
    rows = ((resp.json() or {}).get("data") or {}).get("result") or []
    out = {}
    for r in rows:
        try:
            ymd = str(r["psltRflYmd"])
            out[int(r["psltEpsd"])] = {"group": int(r["wnBndNo"]), "number": str(r["wnRnkVl"]),
                                       "bonus": str(r["bnsRnkVl"]), "date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"}
        except (KeyError, ValueError, TypeError):
            continue
    if not out:
        raise PensionError("연금복권 당첨번호 목록이 비었어요")
    return out


def rank_of(group: int, number: str, win: dict) -> str | None:
    """표 한 장 등수. 1등 = 조+6자리 · 2등 = 6자리(조 다름) · 보너스 = 보너스 6자리 · 3-7등 = 끝 5-1자리. 없으면 None."""
    if number == win["number"]:
        return "1등" if group == win["group"] else "2등"
    if number == win["bonus"]:
        return "보너스"
    for k, rank in SUFFIX_RANKS:
        if number[-k:] == win["number"][-k:]:
            return rank
    return None


def judge(tickets: list[tuple[int, str]], win: dict) -> tuple[str, int]:
    """(결과 글, 당첨금 합). 예 ('7등×5', 5000) · ('낙첨', 0)."""
    ranks = [rank_of(g, n, win) for g, n in tickets]
    won = [r for r in ranks if r]
    if not won:
        return "낙첨", 0
    order = list(PRIZE)
    text = " · ".join(f"{r}×{won.count(r)}" for r in sorted(set(won), key=order.index))
    return text, sum(PRIZE[r] for r in won)

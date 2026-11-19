# lotto-safe-auto-buy

> **Keep the odds above zero. Automate the consistency.** Safe weekly auto-buy for Korean Lotto 6/45 & Pension 720+ on GitHub Actions.

동행복권 **로또 6/45 · 연금복권720+** 를 매주 자동으로 삽니다. GitHub Actions 에서 돌아서 PC 를 켜 둘 필요가 없어요.

- **가능성은 0이 아니게**: 안 사면 확률은 0이에요
- **꾸준함은 자동으로**: 까먹어도, 귀찮아도 매주 삽니다
- **한도는 단단하게**: 정한 한도보다 더 사지 않고, 같은 회차를 두 번 사지 않아요

---

## 준비물

1. **[동행복권](https://dhlottery.co.kr) 회원가입 · 본인인증** (온라인 구매는 만 19세 이상)
2. **예치금 충전**: 온라인 구매는 예치금에서만 빠져요. 동행복권 마이페이지에서 미리 충전해 두세요
3. **GitHub 계정**
4. (알림을 받으려면) **텔레그램**

## AI 로 시작하기 (Claude Code)

아래를 Claude Code 에 붙여넣으세요. **비밀번호는 Claude 에게 말하지 말고, 열어주는 브라우저 화면에 직접 넣으세요.**

```
mopil/lotto-safe-auto-buy 로 로또 자동 구매를 시작하게 도와줘.
0. gh(GitHub CLI)가 없으면 설치해줘 (Windows: winget install GitHub.cli · Mac: brew install gh). 로그인이 안 돼 있으면 gh auth login 은 내가 직접 할게
1. 이 템플릿으로 내 Private 레포를 만들어줘 (gh repo create <이름> --private --template mopil/lotto-safe-auto-buy)
2. 그 레포의 Secrets 등록 페이지를 브라우저로 열어줘. LOTTO_SECRETS 는 내가 직접 넣을게. 비밀번호는 묻지 마
3. 다 넣었다고 하면 무엇을 살지 · 로또 게임 수 · 연금 방식 · 텔레그램 알림을 물어보고 "① 설정" 을 그 값으로 실행해줘
4. 실행 결과를 읽고 설명해줘. 구매 워크플로는 dry_run=true 로만 돌려
```

> AI 는 돈 버튼을 누르지 않아요. 구매는 매번 같은 결과를 내는 스크립트가 하고, AI 는 시작과 설명만 도와요.

## 브라우저로 시작하기

1. **[Use this template]** → **Private** 으로 내 레포 만들기
2. 내 레포 **Settings → Secrets and variables → Actions → New repository secret**
   이름 `LOTTO_SECRETS`, 값은 아래처럼 (알림을 안 받으면 마지막 줄은 빼요)
   ```
   DH_ID=동행복권아이디
   DH_PW=동행복권비밀번호
   TELEGRAM_TOKEN=텔레그램봇토큰
   ```
3. (알림 받을 때만) [@BotFather](https://t.me/BotFather) `/newbot` 으로 만든 봇에게 아무 말이나 한 번
4. **Actions → ① 설정 → Run workflow** 에서 고르고 실행

| 항목 | 선택지 |
|---|---|
| 무엇을 살까요? | 둘 다 / 로또만 / 연금만 |
| 로또 회차당 게임 수 | 1-5 |
| 연금 구매 방식 | 매주 직접 구매 / 5회차 예약 (실험적) |
| 텔레그램 알림 | 받음 / 안 받음 |

설정은 **사지 않고** 로그인 · 예치금 · 알림만 확인한 뒤 자동 구매를 켜요. 바꾸려면 ① 설정을 다시, 끄려면 **② 멈춤**.

구매는 월요일 오전, 결과 알림은 추첨 뒤에 와요 (KST).

## 안전장치

| | |
|---|---|
| **결제 재시도 없음** | 응답이 늦어도 결제 요청을 다시 보내지 않아요. 두 번 사는 것보다 한 번 놓치는 게 나아요 |
| **결제 직전 기록** | 결제 전에 시도를 먼저 기록해요. 기록을 못 남기면 사지 않아요 |
| **한도 재확인** | 결제 직전마다 이번 회차에 산 장 수를 다시 세요. 못 세면 사지 않아요 |
| **로그인 보호** | 할 일이 없으면 로그인하지 않고, 비밀번호 오류 1번이면 멈춰요 |
| **탈취 탐지** | 내가 안 산 주문이나 설명 안 되는 예치금 감소가 보이면 멈추고 알려요 |

## 주의

- **복권은 기대값이 마이너스인 오락이에요. 잃어도 괜찮은 금액만.**
- 비공식 도구예요. 사이트 약관 확인과 계정 관리는 사용자 책임이에요
- `LOTTO_SECRETS` 는 GitHub 이 암호화해서 저장하고, 주인도 다시 볼 수 없어요. 레포는 **Private** 으로 두세요
- Actions 서버는 해외에 있어요. 동행복권이 해외 접속을 막으면 ① 설정 단계에서 바로 알 수 있어요

## 라이선스

MIT · 의존성 [dhapi](https://github.com/roeniss/dhlottery-api) (Apache-2.0), [NOTICE](NOTICE) 참고

"""클라우드(GitHub Actions) 부품 테스트 — state 브랜치 저장소 · 비밀값 · 설정 · 텔레그램 · 워크플로 모양.

    python tests/test_cloud.py
진짜 git 을 임시 폴더에서 쓴다 (원격 = 임시 bare 저장소). 동행복권·텔레그램에는 붙지 않는다.
"""
from __future__ import annotations

import contextlib
import io


class Out(io.StringIO):
    def reconfigure(self, **kw):   # main() 이 sys.stdout.reconfigure 를 부른다
        pass
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lotto-cloud-test-"))
for _name in ("LOTTO_JITTER", "LOTTO_BACKEND", "LOTTO_SECRETS", "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY", "DH_ID", "DH_PW",
              "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.pop(_name, None)
os.environ["LOTTO_STATE_DIR"] = str(TMP / "unused")
os.environ["LOTTO_DB"] = str(TMP / "unused.sqlite")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "lotto_safe"))

import channel  # noqa: E402
import lotto  # noqa: E402
import settings  # noqa: E402
import store  # noqa: E402


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", check=True)


def raises(kind, fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except kind:
        return True
    return False


def state_repo(name: str) -> tuple[Path, Path]:
    """(원격 bare, state 브랜치를 꺼낸 상태 폴더) — 워크플로가 하는 것과 같은 모양."""
    remote = TMP / f"{name}-remote.git"
    work = TMP / f"{name}-state"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "state", str(work)], check=True)
    git(work, "config", "user.name", "test")
    git(work, "config", "user.email", "test@example.invalid")
    git(work, "remote", "add", "origin", str(remote))
    (work / "README.md").write_text("state\n", encoding="utf-8")
    git(work, "add", "README.md")
    git(work, "commit", "-q", "-m", "start")
    git(work, "push", "-q", "origin", "HEAD:state")
    return remote, work


def use_actions(work: Path) -> None:
    lotto.apply_config({"games": [lotto.LOTTO, lotto.PENSION], "lotto_games": 3, "pension_mode": "weekly"})
    lotto._BACKEND = lotto.make_backend("actions")
    lotto.STATE = work / "state.json"
    os.environ["LOTTO_STATE_DIR"] = str(work)


def remote_state(remote: Path) -> dict:
    out = subprocess.run(["git", "--git-dir", str(remote), "show", "state:state.json"], capture_output=True, text=True,
                         encoding="utf-8")
    return json.loads(out.stdout) if out.returncode == 0 else {}


# ---------- state 브랜치 저장소 ----------
def test_git_store_records_and_pushes():
    remote, work = state_repo("ok")
    use_actions(work)
    assert lotto.load_state() == {}
    lotto.save_state({"a": 1})
    assert remote_state(remote) == {"a": 1}                       # 푸시까지 끝나야 기록된 것
    before = git(work, "rev-parse", "HEAD").stdout
    lotto.save_state({"a": 1})                                    # 바뀐 게 없으면 커밋도 없다
    assert git(work, "rev-parse", "HEAD").stdout == before
    assert lotto.acquire_lock() is True                           # 잠금은 워크플로 concurrency 몫


def test_push_failure_never_pays():
    remote, work = state_repo("pushfail")
    use_actions(work)
    git(work, "remote", "set-url", "origin", str(TMP / "gone.git"))   # 원격이 사라짐 → 푸시 실패
    paid = []

    class Client:
        def buy_lotto645(self, tickets):
            paid.append(len(tickets))

    real_round, real_now = lotto.site_lotto_round, lotto.now_kst
    real_send, real_notify = channel.send, channel.notify
    try:
        lotto.site_lotto_round = lambda: 1251
        lotto.now_kst = lambda: lotto.datetime(2026, 11, 16, 10, 0, tzinfo=lotto.KST)
        channel.notify = lambda source, items: True
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = lotto.buy_lotto(Client(), lotto.Capture(), 1251, 1)
    finally:
        lotto.site_lotto_round, lotto.now_kst = real_round, real_now
        channel.send, channel.notify = real_send, real_notify
    assert result.get("error") == "시도 기록 실패" and not paid, (result, paid)   # 기록을 못 남기면 결제 요청 0번
    assert raises(lotto.StateError, lotto.mark_lotto_attempt, 1251, 1)


def test_wrong_branch_or_broken_state_never_reads():
    remote, work = state_repo("branch")
    use_actions(work)
    git(work, "switch", "-q", "-c", "other")
    assert raises(lotto.StateError, lotto.load_state)            # state 브랜치가 아니면 읽기부터 거부
    git(work, "switch", "-q", "state")
    (work / "state.json").write_text('{"lotto_attempts": {', encoding="utf-8")
    assert raises(lotto.StateError, lotto.load_state)            # 깨진 기록 → 안 산다
    (work / "state.json").write_text("[1, 2]", encoding="utf-8")
    assert raises(lotto.StateError, lotto.load_state)


def test_attempt_is_pushed_before_payment():
    remote, work = state_repo("order")
    use_actions(work)
    seen = []

    class Client:
        def buy_lotto645(self, tickets):
            seen.append(remote_state(remote))                     # 결제 요청 순간 원격에 이미 시도 기록이 있어야 한다

    real_round, real_now = lotto.site_lotto_round, lotto.now_kst
    try:
        lotto.site_lotto_round = lambda: 1251
        lotto.now_kst = lambda: lotto.datetime(2026, 11, 16, 10, 0, tzinfo=lotto.KST)
        with contextlib.redirect_stdout(io.StringIO()):
            lotto.buy_lotto(Client(), lotto.Capture(), 1251, 2)
    finally:
        lotto.site_lotto_round, lotto.now_kst = real_round, real_now
    assert seen and "1251" in json.dumps(seen[0]), seen


def test_publish_reports_git_errors_as_state_error():
    def boom(*a, **kw):
        raise TimeoutError("git hung")
    (TMP / "pub").mkdir(exist_ok=True)
    (TMP / "pub" / "state.json").write_text("{}", encoding="utf-8")
    assert raises(lotto.StateError, store.publish, TMP / "pub", "m", lotto.StateError, run=boom)
    assert raises(lotto.StateError, store.verify_branch, TMP / "pub", lotto.StateError, run=boom)


# ---------- 비밀값 ----------
def test_parse_secrets():
    text = "DH_ID=myid\r\nDH_PW= p a s s \r\n\n# 메모\nTELEGRAM_TOKEN=123:abc=def\n"
    s = settings.parse_secrets(text)
    assert s == {"DH_ID": "myid", "DH_PW": " p a s s ", "TELEGRAM_TOKEN": "123:abc=def"}, s   # 비번 공백·'=' 그대로
    assert raises(settings.SettingsError, settings.parse_secrets, "DH_ID=a\nDH_ID=b")
    assert raises(settings.SettingsError, settings.parse_secrets, "DH_ID")
    assert raises(settings.SettingsError, settings.parse_secrets, "=x")
    assert settings.parse_secrets(None) == {}


def test_env_provider_and_secret_fallback():
    try:
        os.environ["LOTTO_SECRETS"] = "DH_ID=abc\nDH_PW=pw "
        user = lotto.EnvProvider().read()
        assert user.username == "abc" and user.password == "pw ", user
        os.environ["LOTTO_SECRETS"] = "DH_ID=abc"
        assert lotto.EnvProvider().read() is None                 # 비번이 없으면 로그인 안 함
        os.environ.pop("LOTTO_SECRETS")
        os.environ["DH_ID"], os.environ["DH_PW"] = "x", "y"       # 로컬: 같은 이름 환경변수
        assert lotto.EnvProvider().read().username == "x"
        os.environ["LOTTO_SECRETS"] = "broken line"
        assert lotto.EnvProvider().read() is None                 # 모양이 틀리면 None (경보로 이어짐)
    finally:
        for n in ("LOTTO_SECRETS", "DH_ID", "DH_PW"):
            os.environ.pop(n, None)


def test_mask_secrets_only_in_actions():
    try:
        os.environ["LOTTO_SECRETS"] = "DH_ID=abc\nDH_PW=secret-pw"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            settings.mask_secrets()
        assert out.getvalue() == ""
        os.environ["GITHUB_ACTIONS"] = "true"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            settings.mask_secrets()
        assert "::add-mask::secret-pw" in out.getvalue() and "::add-mask::abc" in out.getvalue()
    finally:
        os.environ.pop("LOTTO_SECRETS", None)
        os.environ.pop("GITHUB_ACTIONS", None)


# ---------- 설정 ----------
def test_config_validation_and_form():
    assert settings.validate_config({})["enabled"] is False        # 없는 칸은 기본값(꺼짐)
    for bad in ({"lotto_games": 0}, {"lotto_games": 6}, {"lotto_games": True}, {"games": []}, {"games": ["spitto"]},
                {"pension_mode": "daily"}, {"enabled": "yes"}):
        assert raises(settings.SettingsError, settings.validate_config, bad), bad
    cfg = settings.config_from_form("lotto", 5, "weekly", False, None)
    assert cfg["games"] == [lotto.LOTTO] and cfg["enabled"] is True
    assert raises(settings.SettingsError, settings.config_from_form, "all", 1, "weekly", False, None)


def test_apply_config_sets_caps_and_games():
    lotto.apply_config({"games": [lotto.LOTTO], "lotto_games": 5, "pension_mode": "reserve"})
    assert lotto.ROUND_CAP[lotto.LOTTO] == 5 and lotto.WEEKLY_CAP_WON == 5000 and lotto.PENSION_RESERVE_ENABLED is False
    assert lotto.ENABLED_GAMES == {lotto.LOTTO}
    rounds, _ = lotto.open_rounds(lotto.datetime(2026, 11, 16, 10, 0, tzinfo=lotto.KST))
    assert set(rounds) == {lotto.LOTTO}                           # 고르지 않은 연금은 아예 안 본다
    lotto.apply_config({"games": [lotto.LOTTO, lotto.PENSION], "lotto_games": 2, "pension_mode": "reserve"})
    assert lotto.WEEKLY_CAP_WON == 7000 and lotto.PENSION_RESERVE_ENABLED is True
    lotto.apply_config({"games": [lotto.PENSION], "lotto_games": 1, "pension_mode": "weekly"})
    assert lotto.WEEKLY_CAP_WON == 5000 and lotto.PENSION_RESERVE_ENABLED is False


def test_buy_skips_when_disabled():
    os.environ["LOTTO_STATE_DIR"] = str(TMP / "disabled")
    lotto._BACKEND = None
    argv = sys.argv
    try:
        sys.argv = ["lotto.py", "buy", "--no-jitter"]
        out = Out()
        with contextlib.redirect_stdout(out):
            lotto.main()
    finally:
        sys.argv = argv
    last = json.loads(out.getvalue().strip().splitlines()[-1])
    assert "skipped" in last and "꺼져" in last["skipped"], last


# ---------- 텔레그램 ----------
def test_discover_chat_picks_latest():
    data = {"result": [{"update_id": 5, "message": {"chat": {"id": 111}}},
                       {"update_id": 9, "message": {"chat": {"id": 222}}},
                       {"update_id": 7, "edited_message": {"chat": {"id": 333}}}]}
    assert channel.discover_chat("t", get=lambda url: data) == "222"
    assert channel.discover_chat("t", get=lambda url: {"result": []}) is None


def test_send_respects_settings():
    os.environ["LOTTO_STATE_DIR"] = str(TMP / "tg")
    posted = []
    real_post = channel._post
    try:
        channel._post = lambda url, body=None: posted.append((url, body)) or {"ok": True}
        settings.save_config({"enabled": True, "telegram": False})
        with contextlib.redirect_stdout(io.StringIO()):
            assert channel.send("hi") is True and not posted          # 알림 끔 → 로그에만, '보냄'으로 친다
        settings.save_config({"enabled": True, "telegram": True, "chat_id": "42"})
        with contextlib.redirect_stdout(io.StringIO()):
            assert channel.send("hi") is False and not posted         # 토큰이 없으면 못 보냄
        os.environ["LOTTO_SECRETS"] = "TELEGRAM_TOKEN=tok"
        with contextlib.redirect_stdout(io.StringIO()):
            assert channel.send("hi", silent=False) is True
        assert posted and posted[-1][1]["chat_id"] == "42" and posted[-1][1]["disable_notification"] is False
        channel.DRY_RUN = True
        posted.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            assert channel.send("hi") is False and not posted         # dry-run 은 안 보낸다
    finally:
        channel._post = real_post
        channel.DRY_RUN = False
        os.environ.pop("LOTTO_SECRETS", None)


# ---------- 워크플로 모양 ----------
def load_workflows() -> dict:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        return {}
    out = {}
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        out[path.name] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return out


def test_workflows_one_at_a_time_never_cancel_and_can_write_state():
    flows = load_workflows()
    if not flows:
        print("  (PyYAML 없음 — 텍스트로만 확인)")
        for path in (ROOT / ".github" / "workflows").glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            assert "group: lotto" in text and "cancel-in-progress: false" in text and "contents: write" in text, path
        return
    assert set(flows) == {"setup.yml", "buy.yml", "check.yml", "stop.yml"}, set(flows)
    for name, flow in flows.items():
        assert flow["concurrency"] == {"group": "lotto", "cancel-in-progress": False}, name   # 결제 중간에 끊지 않는다
        assert flow["permissions"] == {"contents": "write"}, name
    triggers = flows["buy.yml"][True]                              # PyYAML 은 'on' 을 True 로 읽는다
    assert triggers["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True   # 손 실행 기본은 시험 실행
    form = flows["setup.yml"][True]["workflow_dispatch"]["inputs"]
    assert set(form) == {"buy", "lotto_games", "pension_mode", "telegram"}
    assert form["lotto_games"]["options"] == ["1", "2", "3", "4", "5"]


def test_no_payment_retry_in_source():
    src = (ROOT / "lotto_safe" / "lotto.py").read_text(encoding="utf-8") + \
        (ROOT / "lotto_safe" / "pension720.py").read_text(encoding="utf-8")
    assert "retry" not in src.lower(), "결제 경로에 재시도가 들어오면 안 된다"


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

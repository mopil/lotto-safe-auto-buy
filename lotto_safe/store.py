"""상태 폴더를 git `state` 브랜치에 기록한다 (GitHub Actions 용).

Actions 는 매번 빈 서버라, 결제 직전 시도 기록(state.json)이 다음 실행까지 남아 있어야 이중 결제를 막는다.
워크플로가 같은 레포의 `state` 브랜치를 상태 폴더(LOTTO_STATE_DIR)에 꺼내 두고, 이 파일이 쓸 때마다 커밋·푸시한다.
- 푸시까지 성공해야 '기록됨'. 실패하면 예외 → 결제 직전 기록이면 결제까지 못 간다 (fail-closed). 재시도하지 않는다.
- 상태 폴더가 `state` 브랜치가 아니면(꺼내기 실패·잘못된 실행) 읽기부터 거부한다.
- 동시에 두 실행이 돌지 않게 하는 건 워크플로 `concurrency` (cancel-in-progress: false) 몫이다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

BRANCH = "state"
TRACKED = ("state.json", "config.json", "lotto.sqlite")


def _git(path: Path, *args: str, run: Callable = subprocess.run) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def verify_branch(path: Path, error_cls: type[Exception], run: Callable = subprocess.run) -> None:
    try:
        r = _git(path, "rev-parse", "--abbrev-ref", "HEAD", run=run)
    except Exception as e:  # noqa: BLE001
        raise error_cls(f"상태 폴더를 git 으로 못 읽음 ({type(e).__name__}) — 안 사") from e
    if r.returncode != 0 or r.stdout.strip() != BRANCH:
        raise error_cls(f"상태 폴더({path})가 '{BRANCH}' 브랜치가 아님 — 시도 기록을 모르는 채로 안 사")


def publish(path: Path, message: str, error_cls: type[Exception], run: Callable = subprocess.run) -> bool:
    """상태 폴더의 추적 파일을 커밋·푸시. 바뀐 게 없으면 False. 실패하면 error_cls."""
    names = [n for n in TRACKED if (path / n).exists()]
    try:
        if names:
            add = _git(path, "add", "--", *names, run=run)
            if add.returncode != 0:
                raise error_cls(f"상태 기록 git add 실패 — 안 사 ({add.stderr.strip()[:120]})")
        if _git(path, "diff", "--cached", "--quiet", run=run).returncode == 0:
            return False
        commit = _git(path, "commit", "-q", "-m", message, run=run)
        if commit.returncode != 0:
            raise error_cls(f"상태 기록 커밋 실패 — 안 사 ({commit.stderr.strip()[:120]})")
        push = _git(path, "push", "-q", "origin", f"HEAD:{BRANCH}", run=run)
        if push.returncode != 0:
            raise error_cls(f"상태 기록 푸시 실패 — 안 사 ({push.stderr.strip()[:120]})")
    except error_cls:
        raise
    except Exception as e:  # noqa: BLE001  (타임아웃·git 없음)
        raise error_cls(f"상태 기록 실패 ({type(e).__name__}) — 안 사") from e
    return True

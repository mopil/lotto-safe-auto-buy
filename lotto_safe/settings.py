"""비밀값(LOTTO_SECRETS)과 설정(config.json).

비밀값 — GitHub 레포 Settings → Secrets 에 `LOTTO_SECRETS` 하나로 넣는다 (여러 줄 KEY=VALUE):
    DH_ID=동행복권아이디
    DH_PW=동행복권비밀번호
    TELEGRAM_TOKEN=봇토큰        (알림을 안 받으면 이 줄은 없어도 됨)
값은 '=' 뒤 그대로 쓴다 (비밀번호 앞뒤 공백도 지우지 않는다 — 줄 끝 \\r 만 뗀다). 빈 줄·# 줄은 건너뛴다.
같은 이름이 두 번 나오면 어느 쪽이 맞는지 모르니 오류. 로컬에선 같은 이름의 환경변수로도 줄 수 있다.

설정 — '① 설정' 워크플로가 상태 폴더(state 브랜치)의 config.json 에 쓴다. 파일이 없으면 꺼진 상태(enabled=False)다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SECRETS_ENV = "LOTTO_SECRETS"
SECRET_KEYS = ("DH_ID", "DH_PW", "TELEGRAM_TOKEN")
LOTTO, PENSION = "lotto645", "pension720"
GAME_CHOICES = {"lotto": [LOTTO], "pension": [PENSION], "both": [LOTTO, PENSION]}
LOTTO_GAMES_MAX = 5          # 로또 온라인 1회 구매 한도 5,000원 = 5게임
PENSION_MODES = ("reserve", "weekly")
DEFAULT_CONFIG = {"enabled": False, "games": [LOTTO, PENSION], "lotto_games": 1, "pension_mode": "weekly",
                  "telegram": False, "chat_id": None}


class SettingsError(ValueError):
    """비밀값·설정 모양이 틀림 — 사지 않는다."""


# ---------- 비밀값 ----------
def parse_secrets(text: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in (text or "").split("\n"):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise SettingsError(f"LOTTO_SECRETS 한 줄에 '=' 가 없음 (줄 앞부분: {line.strip()[:8]!r}…)")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise SettingsError("LOTTO_SECRETS 에 이름 없는 줄이 있음")
        if key in out:
            raise SettingsError(f"LOTTO_SECRETS 에 {key} 가 두 번 있음")
        out[key] = value
    return out


def secrets() -> dict[str, str]:
    return parse_secrets(os.environ.get(SECRETS_ENV))


def secret(name: str) -> str | None:
    """LOTTO_SECRETS 안의 값 → 없으면 같은 이름의 환경변수 → 없으면 None."""
    value = secrets().get(name)
    if value:
        return value
    return os.environ.get(name) or None


def mask_secrets() -> None:
    """GitHub Actions 로그에서 값이 보이지 않게 가린다 (여러 줄 비밀값은 줄마다 따로 가려야 안전하다)."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    for value in secrets().values():
        if value.strip():
            print(f"::add-mask::{value}")


# ---------- 설정 ----------
def state_dir() -> Path:
    return Path(os.environ.get("LOTTO_STATE_DIR") or Path.home() / ".lotto-safe")


def config_path() -> Path:
    return state_dir() / "config.json"


def validate_config(cfg: dict) -> dict:
    """모양을 확인하고 빠진 칸은 기본값으로. 틀리면 SettingsError."""
    if not isinstance(cfg, dict):
        raise SettingsError("config.json 모양이 다름")
    out = {**DEFAULT_CONFIG, **cfg}
    games = out["games"]
    if not isinstance(games, list) or not games or any(g not in (LOTTO, PENSION) for g in games):
        raise SettingsError(f"games 가 이상함: {games!r}")
    lg = out["lotto_games"]
    # 1 <= … <= 5 : 0게임이면 로또를 안 고르면 된다 · 5게임 = 온라인 1회 한도
    if not isinstance(lg, int) or isinstance(lg, bool) or not 1 <= lg <= LOTTO_GAMES_MAX:
        raise SettingsError(f"lotto_games 는 1-{LOTTO_GAMES_MAX}: {lg!r}")
    if out["pension_mode"] not in PENSION_MODES:
        raise SettingsError(f"pension_mode 는 reserve | weekly: {out['pension_mode']!r}")
    if not isinstance(out["enabled"], bool) or not isinstance(out["telegram"], bool):
        raise SettingsError("enabled·telegram 은 true/false")
    return out


def load_config() -> dict:
    """없으면 기본값(꺼짐). 있는데 못 읽거나 틀리면 SettingsError — 사지 않는다."""
    path = config_path()
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SettingsError(f"config.json 을 못 읽음 ({type(e).__name__})") from e
    return validate_config(cfg)


def save_config(cfg: dict) -> dict:
    cfg = validate_config(cfg)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return cfg


def config_from_form(buy: str, lotto_games: int, pension_mode: str, telegram: bool, chat_id: str | None) -> dict:
    """'① 설정' 폼 값 → config. buy = lotto | pension | both."""
    if buy not in GAME_CHOICES:
        raise SettingsError(f"살 것은 lotto | pension | both: {buy!r}")
    return validate_config({"enabled": True, "games": GAME_CHOICES[buy], "lotto_games": int(lotto_games),
                            "pension_mode": pension_mode, "telegram": bool(telegram), "chat_id": chat_id})

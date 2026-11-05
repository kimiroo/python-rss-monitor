#!/usr/bin/env python3
"""RSS 피드를 cron 스케줄로 확인해서 키워드가 포함된 새 글을 알려주는 봇.

- 스케줄: APScheduler (cron 표현식)
- 알림:   Apprise (Slack, Discord, Telegram, Gotify, ntfy 등 URL 하나로 지정)
설정은 모두 환경변수로 받는다 (README.md 참고).
"""

import json
import logging
import os
import signal
import sys
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import apprise
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("rssmonitor")


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

def parse_keywords(raw: str) -> list[str]:
    """KEYWORDS 환경변수 파싱.

    - JSON 배열:   '["장학", "AI 아카데미"]'  -> 하나라도 포함되면 매칭 (OR)
    - 쉼표 구분:   '장학, 공모전'               -> 위와 동일 (OR)
    - 단일 문자열: '장학'                        -> 그 문자열 하나
    """
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ValueError("KEYWORDS JSON must be an array")
        keywords = [str(v) for v in value]
    else:
        keywords = raw.split(",")
    return [k.strip() for k in keywords if k.strip()]


def env_secret(name: str) -> str:
    """민감한 값을 환경변수 또는 파일(Docker secrets)에서 읽는다.

    - NAME_FILE=/run/secrets/xxx  -> 파일 내용 사용 (postgres 등 공식 이미지와 같은 규칙)
    - NAME=/run/secrets/xxx       -> 값이 존재하는 파일 경로면 파일 내용 사용 (watchtower 방식)
    - NAME=값                     -> 그대로 사용
    """
    path = os.environ.get(f"{name}_FILE", "").strip()
    value = os.environ.get(name, "").strip()
    if not path and value.startswith("/") and os.path.isfile(value):
        path = value
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return value


def env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


class Config:
    def __init__(self) -> None:
        env = os.environ
        self.feed_url = env.get("FEED_URL", "").strip()
        self.keywords = parse_keywords(env.get("KEYWORDS", ""))
        # Apprise URL. 여러 개면 공백/쉼표/줄바꿈으로 구분. Slack 웹훅 URL은 그대로 넣어도 됨
        # Docker secrets 파일 경로로도 줄 수 있음 (env_secret 참고)
        self.notify_urls = env_secret("NOTIFY_URLS")
        # cron 표현식: 분 시 일 월 요일
        self.schedule = env.get("SCHEDULE", "*/5 * * * *").strip()
        self.timezone = ZoneInfo(env.get("TZ", "Asia/Seoul"))
        # 매칭 대상 필드: title, description (쉼표 구분)
        self.match_fields = [
            f.strip() for f in env.get("MATCH_FIELDS", "title").split(",") if f.strip()
        ]
        self.state_file = env.get("STATE_FILE", "/data/state.json")
        # 처음 실행할 때 이미 피드에 있는 글도 알릴지 여부
        self.notify_existing = env_bool("NOTIFY_EXISTING", False)
        # 컨테이너 시작 직후 한 번 바로 확인할지 (아니면 다음 cron 시각까지 대기)
        self.run_on_start = env_bool("RUN_ON_START", True)
        # 한 번만 확인하고 종료 (테스트용)
        self.run_once = env_bool("RUN_ONCE", False)
        self.timeout = int(env.get("HTTP_TIMEOUT", "20"))

        if not self.feed_url:
            sys.exit("FEED_URL is required")

        # 기본값이면 메시지에 'Apprise' 이름과 아이콘이 붙으므로 교체/제거
        self.notifier = apprise.Apprise(asset=apprise.AppriseAsset(
            app_id="rssmonitor",
            app_desc="RSS keyword monitor",
            image_url_mask=None,
        ))
        if self.notify_urls:
            if not self.notifier.add(self.notify_urls):
                sys.exit("NOTIFY_URLS contains an invalid Apprise URL")
        else:
            log.warning("NOTIFY_URLS is empty: notifications will only be logged")


# ---------------------------------------------------------------------------
# 상태 저장 (이미 본 글 ID 목록)
# ---------------------------------------------------------------------------

MAX_SEEN = 2000  # 피드별로 보관할 최대 ID 개수


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        log.error("failed to read state file %s: %s (starting fresh)", path, e)
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 원자적 교체: 중간에 죽어도 파일이 깨지지 않음


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "rssmonitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def clean_title(title: str) -> str:
    title = title.strip()
    # 연세대 게시판 RSS는 제목 끝에 '}'가 붙어 나오는 버그가 있음
    if title.endswith("}") and "{" not in title:
        title = title[:-1].rstrip()
    return title


def fetch_items(feed_url: str, timeout: int) -> list[dict]:
    data = http_get(feed_url, timeout).strip()  # 앞쪽 공백 줄 제거
    root = ET.fromstring(data)
    items = []
    for node in root.iter("item"):
        link = (node.findtext("link") or "").strip()
        guid = (node.findtext("guid") or "").strip()
        items.append({
            "id": guid or link,
            "title": clean_title(node.findtext("title") or ""),
            "link": urljoin(feed_url, link),  # 상대경로 -> 절대경로
            "description": (node.findtext("description") or "").strip(),
            "pubDate": (node.findtext("pubDate") or "").strip(),
            "author": (node.findtext("author") or "").strip(),
        })
    return items


def find_matches(item: dict, keywords: list[str], fields: list[str]) -> list[str]:
    text = " ".join(item.get(f, "") for f in fields).lower()
    return [k for k in keywords if k.lower() in text]


def should_notify(cfg: "Config", item: dict) -> tuple[bool, list[str]]:
    """(알릴지 여부, 매칭된 키워드). 키워드가 없으면 모든 새 글을 알린다."""
    if not cfg.keywords:
        return True, []
    matched = find_matches(item, cfg.keywords, cfg.match_fields)
    return bool(matched), matched


# ---------------------------------------------------------------------------
# 알림
# ---------------------------------------------------------------------------

def notify(cfg: Config, item: dict, matched: list[str]) -> None:
    """Apprise로 알림 전송. 실패하면 예외를 던진다."""
    # '<' '>' 는 Slack 링크 문법(<url|text>)을 깨뜨리므로 비슷한 문자로 치환
    title = item["title"].replace("<", "‹").replace(">", "›")
    subject = f"🔔 키워드 알림: {', '.join(matched)}" if matched else "🔔 새 글 알림"
    body = f"[{title}]({item['link']})\n{item['author']} · {item['pubDate']}"

    if not cfg.notify_urls:
        log.info("[dry-run] %s | %s", subject, body.replace("\n", " | "))
        return
    if not cfg.notifier.notify(title=subject, body=body,
                               body_format=apprise.NotifyFormat.MARKDOWN):
        raise RuntimeError("apprise notify failed (see apprise log above)")


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def check_once(cfg: Config) -> None:
    items = fetch_items(cfg.feed_url, cfg.timeout)
    state = load_state(cfg.state_file)
    feed_state = state.get(cfg.feed_url)
    first_run = feed_state is None
    seen: list[str] = feed_state["seen"] if feed_state else []
    seen_set = set(seen)

    new_items = [it for it in items if it["id"] and it["id"] not in seen_set]
    log.info("fetched %d items, %d new", len(items), len(new_items))

    # 오래된 글부터 알림 → 채널에서 시간순으로 보임
    for item in reversed(new_items):
        hit, matched = should_notify(cfg, item)
        if hit and (not first_run or cfg.notify_existing):
            try:
                notify(cfg, item, matched)
                log.info("notified: %s (%s)", item["title"], ", ".join(matched) or "all")
            except Exception as e:
                # 전송 실패한 글은 seen에 넣지 않아서 다음 주기에 재시도
                log.error("notify failed for %s: %s", item["link"], e)
                continue
        seen.append(item["id"])
        seen_set.add(item["id"])

    if first_run:
        log.info("first run: marked %d existing items as seen", len(seen))

    state[cfg.feed_url] = {"seen": seen[-MAX_SEEN:]}
    save_state(cfg.state_file, state)


def safe_check(cfg: Config) -> None:
    try:
        check_once(cfg)
    except Exception:
        log.exception("check failed")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    cfg = Config()

    if cfg.run_once:
        check_once(cfg)
        return

    trigger = CronTrigger.from_crontab(cfg.schedule, timezone=cfg.timezone)
    scheduler = BlockingScheduler(
        timezone=cfg.timezone,
        # 이전 실행이 안 끝났으면 겹쳐 돌지 않고, 밀린 실행은 한 번으로 합침
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 300},
    )
    scheduler.add_job(safe_check, trigger, args=[cfg], id="check")

    log.info("watching %s on '%s' (%s) for %s (fields: %s)",
             cfg.feed_url, cfg.schedule, cfg.timezone,
             cfg.keywords or "ALL new items", cfg.match_fields)

    # docker stop 시 SIGTERM을 받으면 바로 종료
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if cfg.run_on_start:
        safe_check(cfg)
    scheduler.start()


if __name__ == "__main__":
    main()

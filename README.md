# rssmonitor

RSS 피드를 cron 스케줄로 확인해서, 키워드가 포함된 **새 글**이 올라오면 알림을 보내는 봇.
코드는 `src/monitor.py` 파일 하나. Docker 빌드에는 `src/` 와 `requirements.txt` 만 들어간다 (`.dockerignore` 화이트리스트).

- 스케줄: [APScheduler](https://apscheduler.readthedocs.io/en/3.x/) 3.x (cron 표현식)
- 알림: [Apprise](https://github.com/caronc/apprise) — Slack, Discord, Telegram, Gotify, ntfy 등 100+ 서비스

## 실행

```sh
cp .env.example .env   # 값 수정
docker compose up -d --build
docker compose logs -f
```

상태(이미 본 글 목록)는 named volume `rssmonitor` 의 `/data/state.json` 에 저장된다.
컨테이너를 지우거나 다시 빌드해도 유지되고, 초기화하려면 `docker compose down -v`.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `FEED_URL` | (필수) | 감시할 RSS URL |
| `KEYWORDS` | (빈 값) | `장학` / `장학, 공모전` / `["장학", "AI 아카데미"]`. 여러 개면 OR, 대소문자 무시. 키워드에 쉼표가 들어가면 JSON 배열로. **비워두면 모든 새 글을 알림** |
| `NOTIFY_URLS` | (빈 값) | Apprise URL. 여러 개면 공백/쉼표로 구분. 비워두면 로그로만 출력 (테스트용) |
| `SCHEDULE` | `*/5 * * * *` | 확인 스케줄 (cron: 분 시 일 월 요일) |
| `TZ` | `Asia/Seoul` | `SCHEDULE` 과 로그 시각의 기준 시간대. `docker-compose.yml` 의 `environment` 에서 설정 |
| `RUN_ON_START` | `true` | 시작 직후 한 번 바로 확인할지 |
| `MATCH_FIELDS` | `title` | 키워드를 찾을 필드. `title,description` 으로 본문 미리보기까지 검색 |
| `NOTIFY_EXISTING` | `false` | 첫 실행 시 이미 피드에 있는 글도 알릴지 |
| `STATE_FILE` | `/data/state.json` | 상태 파일 경로 |
| `RUN_ONCE` | `false` | `true` 면 한 번만 확인하고 종료 (테스트용) |
| `LOG_LEVEL` | `INFO` | |

### SCHEDULE 예시

| 값 | 의미 |
|---|---|
| `*/5 * * * *` | 5분마다 |
| `0 * * * *` | 매시 정각 |
| `*/10 9-18 * * 1-5` | 평일 9~18시, 10분마다 |
| `0 9,13,18 * * *` | 매일 9시, 13시, 18시 |

### NOTIFY_URLS 예시

| 서비스 | URL |
|---|---|
| Slack (웹훅) | `https://hooks.slack.com/services/T.../B.../XXX` (발급받은 URL 그대로) |
| Discord | `https://discord.com/api/webhooks/ID/TOKEN` 또는 `discord://ID/TOKEN` |
| Telegram | `tgram://BOT_TOKEN/CHAT_ID` |
| Gotify | `gotifys://gotify.example.com/APP_TOKEN` |
| ntfy | `ntfys://ntfy.sh/my-topic` |

여러 곳에 동시에 보내려면 공백으로 이어 붙이면 된다.

메시지에 붙는 Apprise 이름/아이콘은 코드에서 제거해 두었다(`rssmonitor` 로 표시).
Slack은 URL 끝에 `?footer=no` 를 붙이면 하단 표시줄 자체가 사라진다:
`https://hooks.slack.com/services/T.../B.../XXX?footer=no` 전체 목록은 [Apprise wiki](https://github.com/caronc/apprise/wiki).

Slack 웹훅 발급: Slack App 생성 → Incoming Webhooks 활성화 → 채널 선택 → URL 복사.

## 동작 방식

- 글 ID(RSS의 `guid`, 없으면 `link`)를 `state.json` 에 피드 URL별로 저장해서 중복 알림을 막는다.
- 첫 실행 때는 기존 글을 "본 것"으로만 기록하고 알림은 보내지 않는다 (`NOTIFY_EXISTING=true` 로 변경 가능).
- 알림 전송이 실패한 글은 기록하지 않아서 다음 실행 때 다시 시도한다.
- 이전 확인이 끝나기 전에 다음 시각이 오면 겹쳐 실행하지 않는다.
- `FEED_URL` 을 바꾸면 그 피드는 새로 첫 실행 취급된다.

## 로컬 테스트 (Docker 없이)

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
FEED_URL='https://yonsei.ac.kr/bbs/sc/58/rssList.do?row=50' KEYWORDS=장학 \
STATE_FILE=./data/state.json RUN_ONCE=true .venv/bin/python src/monitor.py
```

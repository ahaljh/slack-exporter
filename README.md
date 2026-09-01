# slack-exporter

슬랙 워크스페이스 이전을 앞두고 특정 채널의 전체 대화(스레드 포함)와 첨부파일을 백업하는 도구.

- **원본 JSON을 손실 없이 보관**하고, 그 위에 사람이 읽기 좋은 **Markdown을 렌더링**하는 구조
- 원본이 남아 있으므로 나중에 정적 HTML 뷰어나 Notion 업로더를 추가로 만들 수 있음
- 레이트 리밋으로 수집이 오래 걸려도 **중단 후 재실행하면 이어받음** (체크포인트)
- 완료된 채널에 재실행하면 신규 메시지만 **증분 수집**

## 최초 설정 (1회)

### 1. Slack 앱 생성

[api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch → 이름(예: `backup-exporter`)과 워크스페이스 선택.

**OAuth & Permissions → Bot Token Scopes**에 아래 스코프 추가:

| 스코프 | 용도 |
|---|---|
| `channels:read`, `channels:history`, `channels:join` | 공개 채널 목록/메시지/자동 참여 |
| `groups:read`, `groups:history` | 비공개 채널 목록/메시지 |
| `users:read` | 유저 ID → 이름 변환 |
| `files:read` | 첨부파일 다운로드 |

**Install to Workspace** 클릭 후 발급된 `xoxb-` 토큰을 복사.

### 2. 토큰 저장

```bash
cp .env.example .env
# .env 파일에 SLACK_BOT_TOKEN=xoxb-... 입력
```

### 3. 의존성 설치

```bash
uv sync
```

## 사용법

아래에서 `dev-backend`, `general`은 백업할 채널명 예시. 채널명은 공백으로 구분해 여러 개 지정할 수 있고, `#` 접두사는 있어도 없어도 된다.

```bash
# 1. 접근 가능한 채널 목록 확인 (여기 나오는 이름을 아래 명령에 사용)
uv run main.py channels

# 2. (선택) 다운로드 없이 첨부파일 용량 미리 확인
uv run main.py estimate dev-backend general

# 3. 채널 수집 — 메시지 → 스레드 → 첨부파일 순으로 진행
uv run main.py export dev-backend general

# 4. Markdown 렌더링
uv run main.py render dev-backend general
```

- **비공개 채널**은 수집 전에 슬랙에서 봇을 초대해야 함: `/invite @backup-exporter`
- 공개 채널은 export 시 자동 참여
- 앱은 토큰 발급용일 뿐, 서버를 띄우는 것이 아니라 이 스크립트가 로컬에서 API를 호출하는 구조

## 출력 구조

```
export/
├── users.json           # 유저 ID → 이름 매핑
├── channels.json        # 채널 목록 메타
└── <채널명>/
    ├── raw/
    │   ├── history/     # 메시지 원본 JSON (페이지 단위)
    │   └── threads/     # 스레드 답글 원본 JSON
    ├── files/           # 첨부파일 (<파일ID>_<원본명>)
    ├── state.json       # 체크포인트 (수집 재개용)
    └── <채널명>-YYYY-MM.md  # 월별 Markdown
```

`export/`는 대화 내용을 담고 있으므로 `.gitignore`에 포함되어 있음. 백업 보관은 별도 저장소/드라이브에.

## 알아둘 점

- **레이트 리밋**: 2025년 5월 이후 생성된 비마켓플레이스 앱은 `conversations.history`가 분당 1회·요청당 15건으로 제한됨. 수천 메시지면 수 시간 걸릴 수 있으니 켜두면 됨. 중간에 꺼도 재실행하면 이어받음.
- **증분 수집과 스레드 답글**: 백업 완료 후 다시 export하면 신규 메시지에 더해 최근 30일 구간(`--thread-days`로 조정, 0이면 끄기)을 겹쳐서 다시 받아, 그 구간 스레드에 나중에 달린 답글까지 수집함. 그보다 오래된 스레드에 달린 답글은 여전히 놓칠 수 있으니, 그런 경우 `--thread-days`를 크게 주고 돌리면 됨 (구간이 길수록 API 호출이 늘어 오래 걸림).
- **렌더링은 언제든 재실행 가능**: `render`는 raw JSON에서 md를 다시 생성할 뿐이므로 반복 실행해도 안전함.
- 검색은 md 파일을 에디터/grep/Claude Code로 검색하면 됨.

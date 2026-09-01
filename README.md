# slack-exporter

슬랙 워크스페이스 이전을 앞두고 특정 채널의 전체 대화(스레드 포함)와 첨부파일을 백업하는 도구.

- **원본 JSON을 손실 없이 보관**하고, 그 위에 사람이 읽기 좋은 **Markdown을 렌더링**하는 구조
- 원본이 남아 있으므로 나중에 정적 HTML 뷰어나 Notion 업로더를 추가로 만들 수 있음
- 레이트 리밋으로 수집이 오래 걸려도 **중단 후 재실행하면 이어받음** (체크포인트)
- 완료된 채널에 재실행하면 신규 메시지만 **증분 수집**

## 인증 방식 (둘 중 하나 선택)

`.env`에 어떤 변수가 설정되어 있는지에 따라 자동 선택된다.

| 방식 | 환경변수 | 언제 쓰나 |
|---|---|---|
| **봇 토큰** (방식 1) | `SLACK_BOT_TOKEN` | 슬랙 앱을 설치할 수 있을 때 (Pro 플랜 등) |
| **브라우저 세션 토큰** (방식 2) | `SLACK_XOXC_TOKEN` + `SLACK_XOXD_COOKIE` | Free 플랜 등 앱을 쓸 수 없을 때. 앱 설치 불필요 |

두 방식이 모두 설정되어 있으면 **세션 토큰 방식이 우선**한다. 봇 토큰 방식으로 돌아가려면 `.env`에서 `SLACK_XOXC_TOKEN`/`SLACK_XOXD_COOKIE`를 주석 처리하면 된다.

## 최초 설정 (1회)

### 0. 공통

```bash
cp .env.example .env   # 아래에서 택한 방식의 토큰을 입력
uv sync                # 의존성 설치
```

### 방식 1: Slack 앱 생성 (봇 토큰)

[api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → From scratch → 이름(예: `backup-exporter`)과 워크스페이스 선택.

**OAuth & Permissions → Bot Token Scopes**에 아래 스코프 추가:

| 스코프 | 용도 |
|---|---|
| `channels:read`, `channels:history`, `channels:join` | 공개 채널 목록/메시지/자동 참여 |
| `groups:read`, `groups:history` | 비공개 채널 목록/메시지 |
| `users:read` | 유저 ID → 이름 변환 |
| `files:read` | 첨부파일 다운로드 |

**Install to Workspace** 클릭 후 발급된 `xoxb-` 토큰을 `.env`의 `SLACK_BOT_TOKEN`에 입력.

### 방식 2: 브라우저 세션 토큰 추출 (앱 없이)

브라우저에서 [app.slack.com](https://app.slack.com)에 로그인한 상태로 개발자 도구(F12)를 열고:

1. **xoxc 토큰** — Console 탭에서 아래 실행 후 나오는 값을 `SLACK_XOXC_TOKEN`에 입력:

   ```js
   JSON.parse(localStorage.localConfig_v2).teams[
     JSON.parse(localStorage.localConfig_v2).lastActiveTeamId].token
   ```

2. **xoxd 쿠키** — Application 탭 → Cookies → `https://app.slack.com` → `d` 쿠키 값(`xoxd-...`)을 `SLACK_XOXD_COOKIE`에 입력.

주의사항:

- 세션 토큰은 브라우저 로그인 세션에 묶여 있어 **해당 브라우저에서 로그아웃하면 무효화**됨. 수집이 끝날 때까지 로그아웃하지 말 것.
- 본인 계정 권한으로 동작하므로 본인이 볼 수 있는 채널(비공개 포함)을 봇 초대 없이 수집할 수 있음.
- 공식 지원 인증 경로는 아니고, 자기 계정으로 자기 데이터를 백업하는 용도로만 사용할 것.

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

- **비공개 채널**은 봇 토큰 방식일 때 수집 전에 슬랙에서 봇을 초대해야 함: `/invite @backup-exporter` (세션 토큰 방식은 본인이 속한 비공개 채널을 그대로 수집 가능)
- 공개 채널은 봇 토큰 방식이면 export 시 자동 참여, 세션 토큰 방식이면 참여 없이 수집 (입장 메시지가 남지 않도록)
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

- **레이트 리밋**: 2025년 5월 이후 생성된 비마켓플레이스 앱은 `conversations.history`가 분당 1회·요청당 15건으로 제한됨. 수천 메시지면 수 시간 걸릴 수 있으니 켜두면 됨. 중간에 꺼도 재실행하면 이어받음. (세션 토큰 방식은 이 제한과 무관해 훨씬 빠름)
- **Free 플랜의 90일 제한**: Free 플랜에서는 API로도 90일 이전 메시지가 조회되지 않음. 마지막 백업 후 90일이 지나기 전에 주기적으로 증분 수집을 돌릴 것.
- **증분 수집과 스레드 답글**: 백업 완료 후 다시 export하면 신규 메시지에 더해 최근 30일 구간(`--thread-days`로 조정, 0이면 끄기)을 겹쳐서 다시 받아, 그 구간 스레드에 나중에 달린 답글까지 수집함. 그보다 오래된 스레드에 달린 답글은 여전히 놓칠 수 있으니, 그런 경우 `--thread-days`를 크게 주고 돌리면 됨 (구간이 길수록 API 호출이 늘어 오래 걸림).
- **렌더링은 언제든 재실행 가능**: `render`는 raw JSON에서 md를 다시 생성할 뿐이므로 반복 실행해도 안전함.
- 검색은 md 파일을 에디터/grep/Claude Code로 검색하면 됨.

"""메시지/스레드 수집 및 채널별 체크포인트(state.json) 관리

수집은 3단계 상태 머신으로 진행되며, 각 단계는 페이지/스레드 단위로
진행 상황을 state.json에 기록하므로 언제 중단해도 재실행 시 이어받는다.

  history → threads → files → done

완료(done)된 채널에 재실행하면 마지막 타임스탬프 이후의 신규 메시지만
증분 수집한다.
"""

import json
import logging
from pathlib import Path

from .client import SlackClient

logger = logging.getLogger(__name__)

# 요청당 최대 메시지 수 (신규 비마켓플레이스 앱은 서버가 15로 강제 축소할 수 있음)
PAGE_LIMIT = 200

DEFAULT_STATE = {
    "phase": "history",   # history | threads | files | done
    "next_cursor": None,  # 히스토리 페이지네이션 재개 지점
    "page": 0,            # 저장된 히스토리 페이지 수
    "latest_ts": None,    # 수집한 가장 최신 메시지 ts (증분 수집 기준점)
    "oldest": None,       # 이번 수집의 시작점 (증분 수집 시 설정됨)
    "threads_done": [],   # 수집 완료된 스레드 부모 ts 목록
}


def load_state(ch_dir: Path) -> dict:
    path = ch_dir / "state.json"
    if path.exists():
        return {**DEFAULT_STATE, **json.loads(path.read_text())}
    return dict(DEFAULT_STATE)


def save_state(ch_dir: Path, state: dict) -> None:
    (ch_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def estimate_channel_files(client: SlackClient, channel_id: str) -> list[dict]:
    """files.list로 채널 첨부파일 메타(크기 포함) 수집 — 다운로드 없이 용량 예측용.

    files.list는 cursor가 아닌 page 기반 페이지네이션을 사용한다.
    """
    files: list[dict] = []
    page = 1
    while True:
        resp = client.call("files_list", channel=channel_id, count=200, page=page)
        files.extend(resp.get("files", []))
        paging = resp.get("paging") or {}
        if page >= (paging.get("pages") or 1):
            return files
        page += 1


def fetch_users(client: SlackClient, out_root: Path) -> list[dict]:
    """전체 유저 목록을 users.json으로 저장"""
    out_root.mkdir(parents=True, exist_ok=True)
    logger.info("유저 목록 수집 중...")
    users: list[dict] = []
    for items, _ in client.paginate("users_list", "members", limit=200):
        users.extend(items)
    _save_json(out_root / "users.json", users)
    logger.info("유저 %d명 저장 완료", len(users))
    return users


def fetch_channels(client: SlackClient, out_root: Path) -> list[dict]:
    """접근 가능한 공개/비공개 채널 목록을 channels.json으로 저장"""
    out_root.mkdir(parents=True, exist_ok=True)
    logger.info("채널 목록 수집 중...")
    channels: list[dict] = []
    for items, _ in client.paginate(
        "conversations_list",
        "channels",
        types="public_channel,private_channel",
        exclude_archived=False,
        limit=200,
    ):
        channels.extend(items)
    _save_json(out_root / "channels.json", channels)
    return channels


def iter_raw_messages(ch_dir: Path):
    """저장된 원본 JSON(히스토리 + 스레드)의 모든 메시지를 순회"""
    for sub in ("history", "threads"):
        d = ch_dir / "raw" / sub
        if not d.exists():
            continue
        for path in sorted(d.glob("*.json")):
            yield from json.loads(path.read_text())


def export_channel(client: SlackClient, channel: dict, out_root: Path) -> None:
    """채널 하나를 수집 (중단 시 재개 가능)"""
    name = channel["name"]
    ch_dir = out_root / name
    (ch_dir / "raw" / "history").mkdir(parents=True, exist_ok=True)
    (ch_dir / "raw" / "threads").mkdir(parents=True, exist_ok=True)

    state = load_state(ch_dir)

    # 완료된 채널 재실행 → 마지막 ts 이후 증분 수집으로 전환
    if state["phase"] == "done":
        logger.info("#%s: 이전 백업 이후 신규 메시지를 증분 수집합니다", name)
        state["phase"] = "history"
        state["next_cursor"] = None
        state["oldest"] = state["latest_ts"]
        save_state(ch_dir, state)

    # 공개 채널이고 아직 멤버가 아니면 자동 참여
    if not channel.get("is_member") and not channel.get("is_private"):
        logger.info("#%s: 봇이 채널에 참여합니다", name)
        client.call("conversations_join", channel=channel["id"])

    if state["phase"] == "history":
        _fetch_history(client, channel["id"], name, ch_dir, state)
    if state["phase"] == "threads":
        _fetch_threads(client, channel["id"], name, ch_dir, state)
    if state["phase"] == "files":
        from .downloader import download_channel_files

        download_channel_files(client.token, ch_dir)
        state["phase"] = "done"
        save_state(ch_dir, state)

    logger.info("#%s: 수집 완료", name)


def _fetch_history(
    client: SlackClient, channel_id: str, name: str, ch_dir: Path, state: dict
) -> None:
    logger.info("#%s: 메시지 히스토리 수집 시작 (저장된 페이지: %d)", name, state["page"])
    while True:
        params: dict = {"channel": channel_id, "limit": PAGE_LIMIT}
        if state.get("oldest"):
            params["oldest"] = state["oldest"]
        if state.get("next_cursor"):
            params["cursor"] = state["next_cursor"]

        resp = client.call("conversations_history", **params)
        messages = resp.get("messages", [])

        if messages:
            state["page"] += 1
            page_path = ch_dir / "raw" / "history" / f"page_{state['page']:05d}.json"
            _save_json(page_path, messages)
            # 가장 최신 ts 갱신 (히스토리는 최신순으로 내려옴)
            page_max = max(messages, key=lambda m: float(m["ts"]))["ts"]
            if state["latest_ts"] is None or float(page_max) > float(state["latest_ts"]):
                state["latest_ts"] = page_max

        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
        state["next_cursor"] = cursor

        if not cursor:
            state["phase"] = "threads"
            save_state(ch_dir, state)
            logger.info("#%s: 히스토리 수집 완료 (%d페이지)", name, state["page"])
            return

        save_state(ch_dir, state)
        logger.info("#%s: 히스토리 %d페이지 저장 (+%d건)", name, state["page"], len(messages))


def _fetch_threads(
    client: SlackClient, channel_id: str, name: str, ch_dir: Path, state: dict
) -> None:
    # 히스토리에서 스레드 부모 메시지 추출 (답글이 달린 메시지)
    parent_ts: list[str] = []
    seen = set()
    for msg in iter_raw_messages(ch_dir):
        ts = msg.get("ts")
        if (
            msg.get("reply_count")
            and msg.get("thread_ts", ts) == ts
            and ts not in seen
        ):
            seen.add(ts)
            parent_ts.append(ts)

    done = set(state["threads_done"])
    todo = [ts for ts in parent_ts if ts not in done]
    logger.info("#%s: 스레드 %d개 수집 시작 (완료: %d개)", name, len(todo), len(done))

    for i, ts in enumerate(todo, 1):
        replies: list[dict] = []
        for items, _ in client.paginate(
            "conversations_replies", "messages", channel=channel_id, ts=ts, limit=PAGE_LIMIT
        ):
            replies.extend(items)
        _save_json(ch_dir / "raw" / "threads" / f"{ts}.json", replies)
        state["threads_done"].append(ts)
        save_state(ch_dir, state)
        if i % 10 == 0 or i == len(todo):
            logger.info("#%s: 스레드 %d/%d 완료", name, i, len(todo))

    state["phase"] = "files"
    save_state(ch_dir, state)

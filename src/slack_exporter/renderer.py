"""원본 JSON → Markdown 렌더링

- 유저 ID를 실제 이름으로 변환 (users.json)
- Slack mrkdwn 문법을 표준 Markdown으로 변환
- 스레드는 부모 메시지 아래 인용(>)으로 표현
- 월 단위로 파일 분할 (<채널명>-YYYY-MM.md)
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .downloader import is_canvas, sanitize_filename

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]


def build_user_map(users: list[dict]) -> dict[str, str]:
    """유저 ID → 표시 이름 매핑"""
    result = {}
    for u in users:
        profile = u.get("profile") or {}
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or u.get("real_name")
            or u.get("name")
            or u.get("id", "")
        )
        result[u["id"]] = name
    return result


def build_channel_map(channels: list[dict]) -> dict[str, str]:
    return {c["id"]: c.get("name", c["id"]) for c in channels}


def convert_mrkdwn(text: str, users: dict[str, str], channels: dict[str, str]) -> str:
    """Slack mrkdwn → Markdown"""

    def repl_user(m: re.Match) -> str:
        return f"@{users.get(m.group(1), m.group(1))}"

    def repl_channel(m: re.Match) -> str:
        label = m.group(2) or channels.get(m.group(1), m.group(1))
        return f"#{label}"

    text = re.sub(r"<@(\w+)(?:\|[^>]*)?>", repl_user, text)
    text = re.sub(r"<#(\w+)(?:\|([^>]*))?>", repl_channel, text)
    text = re.sub(r"<!(here|channel|everyone)>", r"@\1", text)
    # <url|라벨> → [라벨](url), <url> → url
    text = re.sub(r"<([^>|@#!][^>|]*)\|([^>]+)>", r"[\2](\1)", text)
    text = re.sub(r"<([^>|@#!][^>|]*)>", r"\1", text)
    # 볼드/취소선 변환 (코드 블록은 건드리지 않도록 단순 케이스만)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"**\1**", text)
    text = re.sub(r"(?<![~\w])~([^~\n]+)~(?![~\w])", r"~~\1~~", text)
    # HTML 이스케이프 복원
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text


def _author(msg: dict, users: dict[str, str]) -> str:
    if msg.get("user"):
        return users.get(msg["user"], msg["user"])
    return msg.get("username") or msg.get("bot_id") or "(알 수 없음)"


def _ts_to_dt(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=KST)


def _format_message(
    msg: dict, users: dict[str, str], channels: dict[str, str]
) -> list[str]:
    """메시지 하나를 Markdown 줄 목록으로 변환"""
    dt = _ts_to_dt(msg["ts"])
    lines = [f"**{_author(msg, users)}** — {dt.strftime('%H:%M')}"]

    text = convert_mrkdwn(msg.get("text", ""), users, channels)
    if text:
        lines.extend(text.split("\n"))

    # 첨부파일: 이미지는 임베드, 나머지는 링크
    for f in msg.get("files", []):
        if not f.get("id"):
            continue
        # 캔버스는 첨부로 렌더링하지 않음 — 메시지 text에 이미 내용이 표시됨
        if is_canvas(f):
            continue
        fname = f"{f['id']}_{sanitize_filename(f.get('name', 'file'))}"
        rel = f"files/{fname}"
        if (f.get("mimetype") or "").startswith("image/"):
            lines.append(f"![{f.get('name', '이미지')}]({rel})")
        else:
            lines.append(f"📎 [{f.get('name', fname)}]({rel})")

    # 리액션
    reactions = msg.get("reactions", [])
    if reactions:
        parts = [f":{r['name']}: {r['count']}" for r in reactions]
        lines.append(f"리액션: {' · '.join(parts)}")

    return lines


def _load_messages(ch_dir: Path) -> tuple[list[dict], dict[str, list[dict]]]:
    """히스토리 메시지(중복 제거, 시간순)와 스레드 답글 맵을 로드"""
    by_ts: dict[str, dict] = {}
    history_dir = ch_dir / "raw" / "history"
    if history_dir.exists():
        for path in sorted(history_dir.glob("*.json")):
            for msg in json.loads(path.read_text()):
                by_ts[msg["ts"]] = msg
    messages = sorted(by_ts.values(), key=lambda m: float(m["ts"]))

    threads: dict[str, list[dict]] = {}
    threads_dir = ch_dir / "raw" / "threads"
    if threads_dir.exists():
        for path in sorted(threads_dir.glob("*.json")):
            replies = json.loads(path.read_text())
            if not replies:
                continue
            parent_ts = replies[0].get("thread_ts") or replies[0]["ts"]
            # 첫 항목은 부모 메시지이므로 제외하고 시간순 정렬
            reply_only = sorted(
                (r for r in replies if r["ts"] != parent_ts),
                key=lambda m: float(m["ts"]),
            )
            threads[parent_ts] = reply_only

    return messages, threads


def render_channel(
    ch_dir: Path, users: dict[str, str], channels: dict[str, str]
) -> list[Path]:
    """채널 하나를 월별 Markdown 파일로 렌더링. 생성된 파일 목록 반환"""
    name = ch_dir.name
    messages, threads = _load_messages(ch_dir)
    if not messages:
        logger.warning("#%s: 렌더링할 메시지가 없습니다 (export를 먼저 실행하세요)", name)
        return []

    # 기존 렌더링 결과 제거 후 재생성 (render는 언제든 다시 실행 가능)
    for old in ch_dir.glob(f"{name}-*.md"):
        old.unlink()

    # 월 단위로 그룹핑
    by_month: dict[str, list[dict]] = {}
    for msg in messages:
        month = _ts_to_dt(msg["ts"]).strftime("%Y-%m")
        by_month.setdefault(month, []).append(msg)

    written: list[Path] = []
    for month, msgs in sorted(by_month.items()):
        out_lines = [f"# #{name} — {month}", ""]
        current_date = None

        for msg in msgs:
            dt = _ts_to_dt(msg["ts"])
            date_str = dt.strftime("%Y-%m-%d")
            if date_str != current_date:
                current_date = date_str
                weekday = WEEKDAYS_KO[dt.weekday()]
                out_lines.extend([f"## {date_str} ({weekday})", ""])

            out_lines.extend(_format_message(msg, users, channels))

            # 스레드 답글은 인용 블록으로
            for reply in threads.get(msg["ts"], []):
                rdt = _ts_to_dt(reply["ts"])
                prefix_lines = _format_message(reply, users, channels)
                # 답글 헤더에는 날짜까지 표기 (부모와 날짜가 다를 수 있음)
                prefix_lines[0] = (
                    f"**{_author(reply, users)}** — {rdt.strftime('%Y-%m-%d %H:%M')}"
                )
                out_lines.extend(f"> {line}" if line else ">" for line in prefix_lines)
                out_lines.append("")

            out_lines.append("")

        out_path = ch_dir / f"{name}-{month}.md"
        out_path.write_text("\n".join(out_lines))
        written.append(out_path)

    logger.info(
        "#%s: 메시지 %d건, 스레드 %d개 → %d개 파일 렌더링 완료",
        name,
        len(messages),
        len(threads),
        len(written),
    )
    return written

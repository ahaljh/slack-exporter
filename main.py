"""슬랙 채널 백업 익스포터 CLI

사용법:
  uv run main.py channels                    # 접근 가능한 채널 목록
  uv run main.py estimate 채널명 [채널명...]  # 다운로드 없이 첨부파일 용량 예측
  uv run main.py export 채널명 [채널명...]    # 메시지/스레드/파일 수집 (중단 시 재개 가능)
  uv run main.py render 채널명 [채널명...]    # 수집된 JSON → Markdown 렌더링
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("slack-exporter")


def get_client():
    from slack_exporter.client import SlackClient

    load_dotenv()
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.error(
            "SLACK_BOT_TOKEN이 설정되지 않았습니다.\n"
            ".env.example을 .env로 복사하고 토큰을 입력하세요."
        )
        sys.exit(1)
    return SlackClient(token)


def load_channels_meta(client, out_root: Path, refresh: bool = False) -> list[dict]:
    """channels.json 로드 (없거나 refresh 시 API로 갱신)"""
    from slack_exporter.fetcher import fetch_channels

    path = out_root / "channels.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    return fetch_channels(client, out_root)


def resolve_channels(names: list[str], channels: list[dict]) -> list[dict]:
    """채널명(# 접두 허용) → 채널 메타 정보"""
    by_name = {c["name"]: c for c in channels}
    resolved = []
    for raw in names:
        name = raw.lstrip("#")
        if name not in by_name:
            logger.error("채널을 찾을 수 없습니다: #%s", name)
            logger.error("`uv run main.py channels`로 접근 가능한 채널을 확인하세요.")
            sys.exit(1)
        resolved.append(by_name[name])
    return resolved


def cmd_channels(args):
    client = get_client()
    channels = load_channels_meta(client, args.out, refresh=True)
    print(f"\n{'채널명':<30} {'유형':<8} {'봇 참여':<6} ID")
    print("-" * 60)
    for c in sorted(channels, key=lambda c: c["name"]):
        kind = "비공개" if c.get("is_private") else "공개"
        member = "O" if c.get("is_member") else "-"
        archived = " (보관됨)" if c.get("is_archived") else ""
        print(f"#{c['name']:<29} {kind:<7} {member:<7} {c['id']}{archived}")
    print(
        "\n비공개 채널은 슬랙에서 봇을 초대해야 합니다: /invite @봇이름\n"
        "공개 채널은 export 시 자동 참여합니다."
    )


def human_size(n: float) -> str:
    """바이트 수를 사람이 읽기 좋은 단위로 변환"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# mimetype 대분류 → 한글 표기
_MIME_LABELS = {"image": "이미지", "video": "동영상", "audio": "오디오", "text": "텍스트"}


def cmd_estimate(args):
    from slack_exporter.fetcher import estimate_channel_files

    client = get_client()
    channels = load_channels_meta(client, args.out)
    grand_total = 0

    for channel in resolve_channels(args.channels, channels):
        files = estimate_channel_files(client, channel["id"])
        total = sum(f.get("size", 0) for f in files)
        grand_total += total

        print(f"\n#{channel['name']} — 첨부파일 {len(files)}개, 총 {human_size(total)}")
        if not files:
            continue

        # 유형별 집계
        by_kind: dict[str, list[int]] = {}
        for f in files:
            kind = (f.get("mimetype") or "기타").split("/")[0]
            by_kind.setdefault(_MIME_LABELS.get(kind, "기타"), []).append(f.get("size", 0))
        parts = [
            f"{kind} {len(sizes)}개 ({human_size(sum(sizes))})"
            for kind, sizes in sorted(by_kind.items(), key=lambda kv: -sum(kv[1]))
        ]
        print(f"  유형별: {' · '.join(parts)}")

        # 용량 큰 파일 Top 5
        top = sorted(files, key=lambda f: f.get("size", 0), reverse=True)[:5]
        print("  큰 파일 Top 5:")
        for f in top:
            print(f"    {human_size(f.get('size', 0)):>10}  {f.get('name', '(이름 없음)')}")

    print(
        f"\n합계: {human_size(grand_total)}"
        "\n(메시지 텍스트 JSON은 보통 수 MB 이내라 첨부파일이 실제 용량을 결정합니다)"
    )


def cmd_export(args):
    from slack_exporter.fetcher import export_channel, fetch_users

    client = get_client()
    args.out.mkdir(parents=True, exist_ok=True)

    # 유저 매핑은 렌더링에 필요하므로 없으면 먼저 수집
    if not (args.out / "users.json").exists():
        fetch_users(client, args.out)

    channels = load_channels_meta(client, args.out)
    for channel in resolve_channels(args.channels, channels):
        export_channel(client, channel, args.out)

    logger.info("\n모든 채널 수집 완료. 다음 단계: uv run main.py render %s", " ".join(args.channels))


def cmd_render(args):
    from slack_exporter.renderer import build_channel_map, build_user_map, render_channel

    users_path = args.out / "users.json"
    channels_path = args.out / "channels.json"
    users = build_user_map(json.loads(users_path.read_text())) if users_path.exists() else {}
    channels = (
        build_channel_map(json.loads(channels_path.read_text()))
        if channels_path.exists()
        else {}
    )
    if not users:
        logger.warning("users.json이 없어 유저 ID가 그대로 표시됩니다")

    for raw in args.channels:
        name = raw.lstrip("#")
        ch_dir = args.out / name
        if not ch_dir.exists():
            logger.error("수집된 데이터가 없습니다: %s (export를 먼저 실행하세요)", ch_dir)
            sys.exit(1)
        for path in render_channel(ch_dir, users, channels):
            logger.info("  → %s", path)


def main():
    parser = argparse.ArgumentParser(description="슬랙 채널 백업 익스포터")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("export"),
        help="출력 디렉토리 (기본: ./export)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("channels", help="접근 가능한 채널 목록 출력")
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("estimate", help="다운로드 없이 채널 첨부파일 용량 예측")
    p.add_argument("channels", nargs="+", help="채널명 (여러 개 가능)")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("export", help="채널 메시지/스레드/첨부파일 수집")
    p.add_argument("channels", nargs="+", help="채널명 (여러 개 가능)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("render", help="수집된 JSON을 Markdown으로 렌더링")
    p.add_argument("channels", nargs="+", help="채널명 (여러 개 가능)")
    p.set_defaults(func=cmd_render)

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        logger.info("\n중단되었습니다. 재실행하면 이어받습니다.")
        sys.exit(130)


if __name__ == "__main__":
    main()

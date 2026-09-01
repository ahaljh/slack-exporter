"""첨부파일 다운로드 — 이미 받은 파일은 스킵하므로 재실행에 안전"""

import logging
import re
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    """파일명에 쓸 수 없는 문자를 _로 치환"""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip() or "file"


def is_canvas(f: dict) -> bool:
    """슬랙 캔버스 여부 — 일반 파일이 아니라서 다운로드 대상에서 제외"""
    return f.get("mode") == "quip" or f.get("mimetype") == "application/vnd.slack-docs"


def download_channel_files(token: str, ch_dir: Path, cookie: str | None = None) -> None:
    from .fetcher import iter_raw_messages

    files_dir = ch_dir / "files"
    files_dir.mkdir(exist_ok=True)

    # 원본 JSON 전체에서 첨부파일 목록 수집 (중복 제거)
    targets: dict[str, dict] = {}
    for msg in iter_raw_messages(ch_dir):
        for f in msg.get("files", []):
            if (
                f.get("id")
                and not is_canvas(f)
                and (f.get("url_private_download") or f.get("url_private"))
            ):
                targets[f["id"]] = f

    logger.info("첨부파일 %d개 다운로드 시작", len(targets))
    downloaded = skipped = failed = 0

    # 세션 토큰(xoxc) 방식은 파일 서버 인증에도 d 쿠키가 필요하다
    headers = {"Authorization": f"Bearer {token}"}
    if cookie:
        headers["Cookie"] = f"d={cookie}"

    with httpx.Client(
        headers=headers,
        follow_redirects=True,
        timeout=60,
    ) as http:
        for f in targets.values():
            fname = f"{f['id']}_{sanitize_filename(f.get('name', 'file'))}"
            path = files_dir / fname
            if path.exists():
                skipped += 1
                continue
            url = f.get("url_private_download") or f["url_private"]
            try:
                resp = http.get(url)
                resp.raise_for_status()
                # 인증 실패 시 파일 대신 HTML 로그인 페이지가 내려옴
                if resp.headers.get("content-type", "").startswith("text/html"):
                    raise RuntimeError("인증 실패 (HTML 응답) — files:read 스코프 확인 필요")
                path.write_bytes(resp.content)
                downloaded += 1
            except Exception as e:
                failed += 1
                logger.warning("파일 다운로드 실패: %s (%s)", fname, e)

    logger.info(
        "첨부파일 완료 — 신규 %d, 스킵 %d, 실패 %d", downloaded, skipped, failed
    )

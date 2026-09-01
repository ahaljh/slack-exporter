"""Slack API 클라이언트 래퍼 — 레이트 리밋(429) 대응 포함"""

import logging
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


class SlackClient:
    """레이트 리밋 시 Retry-After 만큼 대기 후 재시도하는 래퍼.

    2025년 5월 이후 생성된 비마켓플레이스 앱은 conversations.history /
    conversations.replies 가 분당 1회 수준으로 제한되므로,
    대기가 길어질 수 있음을 로그로 알려준다.
    """

    def __init__(self, token: str, cookie: str | None = None):
        """token: xoxb-(봇) 또는 xoxc-(브라우저 세션) 토큰.
        cookie: 세션 토큰 사용 시 필요한 d 쿠키 값 (xoxd-...)
        """
        self.token = token
        self.cookie = cookie
        # 세션 토큰(xoxc)은 브라우저 로그인 세션에 묶여 있어 d 쿠키를 함께 보내야 인증된다
        headers = {"Cookie": f"d={cookie}"} if cookie else None
        self._client = WebClient(token=token, headers=headers)

    @property
    def is_session(self) -> bool:
        """브라우저 세션 토큰(xoxc + 쿠키) 방식인지 여부"""
        return self.cookie is not None

    def call(self, method_name: str, **params):
        while True:
            try:
                method = getattr(self._client, method_name)
                return method(**params)
            except SlackApiError as e:
                if e.response.status_code == 429:
                    wait = int(e.response.headers.get("Retry-After", 30))
                    logger.info(
                        "레이트 리밋 도달 (%s) — %d초 대기 후 재시도합니다. "
                        "중단(Ctrl+C)해도 다음 실행 때 이어받습니다.",
                        method_name,
                        wait,
                    )
                    time.sleep(wait + 1)
                else:
                    raise

    def paginate(self, method_name: str, items_key: str, **params):
        """cursor 페이지네이션을 돌며 페이지 단위로 (items, next_cursor)를 반환"""
        cursor = params.pop("cursor", None)
        while True:
            if cursor:
                params["cursor"] = cursor
            resp = self.call(method_name, **params)
            items = resp.get(items_key, [])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
            yield items, cursor
            if not cursor:
                return

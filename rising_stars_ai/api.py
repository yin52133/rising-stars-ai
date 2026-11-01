"""HTTP 客户端：GitHub 限速退避。

失败抛 ApiError，由调用方记为缺测——绝不把失败的采集写成 0。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
REPO_URL = API + "/repos/{full_name}"
USER_AGENT = "rising-stars-ai"


def repo_from_api(item: dict) -> dict:
    """GitHub 仓库响应 → repositories 字段；未知值保持 None，绝不用 0 顶替。"""
    return {
        "repo_id": item["id"],
        "full_name": item["full_name"],
        "description": item.get("description"),
        "url": item.get("html_url"),
        "topics": item.get("topics") or [],
        "stars": item.get("stargazers_count"),
        "created_at": item.get("created_at"),
        "pushed_at": item.get("pushed_at"),
        "archived": item.get("archived", False),
        "is_fork": item.get("fork", False),
        "is_template": item.get("is_template", False),
    }


class ApiError(Exception):
    """重试后仍失败的请求。"""


def _sleep_seconds(exc: Exception, attempt: int) -> float:
    if isinstance(exc, urllib.error.HTTPError):
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 60.0)
        reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
        if exc.code in (403, 429) and reset and reset.isdigit():
            return min(max(int(reset) - time.time(), 1.0), 60.0)
    return float(2 ** attempt)


class Client:
    def __init__(self, token: str | None = None, opener=urllib.request.urlopen,
                 sleep=time.sleep, retries: int = 3, timeout: float = 30.0):
        self.token = token
        self._opener = opener
        self._sleep = sleep
        self.retries = retries
        self.timeout = timeout
        self.requests = 0
        self.errors = 0

    def _fetch(self, url: str, accept: str) -> bytes:
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, headers=headers)
        self.requests += 1
        resp = self._opener(req, timeout=self.timeout)
        try:
            return resp.read()
        finally:
            resp.close()

    def _get(self, url: str, accept: str) -> bytes:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._fetch(url, accept)
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (401, 404) or attempt == self.retries:
                    break
                self._sleep(_sleep_seconds(exc, attempt))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt == self.retries:
                    break
                self._sleep(_sleep_seconds(exc, attempt))
        self.errors += 1
        raise ApiError(f"{url}: {last}") from last

    def get_json(self, url: str, params: dict | None = None, accept: str = "application/vnd.github+json"):
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        body = self._get(url, accept)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.errors += 1
            raise ApiError(f"{url}: 响应不是 JSON") from exc

    def get_text(self, url: str, accept: str = "application/vnd.github.raw") -> str:
        return self._get(url, accept).decode("utf-8", errors="replace")

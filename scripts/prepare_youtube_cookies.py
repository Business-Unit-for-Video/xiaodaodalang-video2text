"""Normalize an optional repository secret without printing credential values."""
from __future__ import annotations

import os
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path

from normalize_youtube_cookies import normalize


def prepare_cookie_export(content: str, path: Path) -> tuple[int, int]:
    if not content.strip():
        path.unlink(missing_ok=True)
        return 0, 0
    content = content.lstrip("\ufeff").strip()
    if content.startswith("```"):
        lines = content.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        content = "\n".join(lines)
    path.write_text(content + "\n", encoding="utf-8", newline="\n")
    try:
        status = normalize(path)
        if not status.startswith(("netscape", "converted-json:", "converted-header:")):
            raise ValueError("Cookie 必须是 Netscape、浏览器 JSON 或 Cookie 请求头")
        jar = MozillaCookieJar(str(path))
        jar.load(ignore_discard=True, ignore_expires=True)
        youtube = [
            cookie for cookie in jar
            if cookie.domain.lstrip(".").lower() == "youtube.com"
            or cookie.domain.lstrip(".").lower().endswith(".youtube.com")
            or cookie.domain.lstrip(".").lower() == "youtu.be"
        ]
        usable = [cookie for cookie in youtube if cookie.expires is None or cookie.expires == 0 or cookie.expires > time.time()]
        if not usable:
            raise ValueError("Cookie 中没有未过期的 YouTube 条目，请更新仓库 Secret")
        path.chmod(0o600)
        return len(youtube), len(usable)
    except Exception:
        path.unlink(missing_ok=True)
        raise ValueError("YouTube Cookie 格式无效或已过期，请重新导出后更新仓库 Secret") from None


def main() -> None:
    try:
        rows, usable = prepare_cookie_export(
            os.environ.get("YOUTUBE_COOKIE_EXPORT", ""), Path("youtube_cookies.txt")
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if rows:
        print(f"YouTube Cookie 格式检查通过：{rows} 条 YouTube Cookie，{usable} 条未过期/会话 Cookie")
    else:
        print("未配置 Cookie，使用公开访问；若 YouTube 要求登录请设置仓库 Secret")


if __name__ == "__main__":
    main()


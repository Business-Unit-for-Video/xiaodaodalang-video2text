"""Normalize common browser cookie exports to yt-dlp's Netscape format."""

import base64
import json
import math
import sys
import ast
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable


COOKIE_HEADER_NAMES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID", "PSID", "LOGIN_INFO",
    "PREF", "VISITOR_INFO1_LIVE", "VISITOR_PRIVACY_METADATA", "YSC", "SOCS",
    "CONSENT", "GPS", "SIDCC", "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
}


def _is_netscape_header(line: str) -> bool:
    normalized = line.strip().lower()
    return normalized.startswith("# netscape http cookie file") or normalized.startswith("# http cookie file")


def _unwrap_transport(raw: str) -> str:
    """Unwrap common secret transport encodings without touching cookie values."""
    text = raw.lstrip("\ufeff")
    seen = set()
    for _ in range(4):
        if text in seen:
            break
        seen.add(text)
        first_line = text.splitlines()[0] if text.splitlines() else ""
        if _is_netscape_header(first_line) and "\\n" not in first_line:
            return text

        stripped = text.strip()
        # A secret copied from JSON or a shell variable may contain the whole
        # Netscape file as a JSON/Python string rather than as real newlines.
        for parser in (json.loads, ast.literal_eval):
            try:
                decoded = parser(stripped)
            except (ValueError, SyntaxError, json.JSONDecodeError):
                continue
            if isinstance(decoded, str) and decoded != text:
                text = decoded
                break
        else:
            decoded = urllib.parse.unquote(text)
            if decoded != text:
                text = decoded
                continue

            # Some secret-management commands store the entire file as one
            # Base64 value. Only accept it when the decoded payload is clearly
            # a Netscape export, so ordinary cookie values are never decoded.
            compact = "".join(stripped.split())
            if compact and len(compact) % 4 == 0:
                try:
                    candidate = base64.b64decode(compact, validate=True).decode("utf-8-sig")
                except (ValueError, UnicodeDecodeError):
                    candidate = ""
                if _is_netscape_header(candidate.splitlines()[0] if candidate else ""):
                    text = candidate
                    continue

            # Literal escaped line/tab separators are only decoded when the
            # payload advertises a Netscape header, avoiding changes to JSON
            # cookie values that legitimately contain backslashes.
            if "\\n" in text and "Netscape HTTP Cookie File\\n" in text:
                text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
                continue
            break
        continue
    return text


def _cookie_items(data: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(data, list):
        return (item for item in data if isinstance(item, dict))
    if isinstance(data, dict):
        if data.get("name") and (data.get("domain") or data.get("host")):
            return (data,)
        for key in ("cookies", "items", "entries"):
            value = data.get(key)
            if isinstance(value, list):
                return (item for item in value if isinstance(item, dict))
    return ()


def _expiry(cookie: Dict[str, Any]) -> str:
    value = cookie.get("expirationDate", cookie.get("expires", cookie.get("expiration", 0)))
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0
    if not math.isfinite(number) or number <= 0:
        return "0"
    return str(int(number))


def normalize(path: Path) -> str:
    raw = _unwrap_transport(path.read_text(encoding="utf-8-sig", errors="replace"))
    raw_lines = raw.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(raw_lines)
            if _is_netscape_header(line)
        ),
        None,
    )
    if header_index is not None:
        # yt-dlp expects the Netscape marker to be the first meaningful line.
        normalized_lines = raw_lines[header_index:]
        path.write_text("\n".join(normalized_lines).rstrip() + "\n", encoding="utf-8", newline="\n")
        return "netscape"

    # Some exporters omit the marker but still emit valid tab-separated rows.
    tab_rows = [line for line in raw_lines if line.strip() and not line.lstrip().startswith("#") and len(line.split("\t")) >= 7]
    if tab_rows:
        path.write_text(
            "# Netscape HTTP Cookie File\n" + "\n".join(tab_rows) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return f"netscape-data:{len(tab_rows)}"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            data = None
        if data is not None:
            # Continue through the same browser-export handling below.
            pass
        else:
            decoded = urllib.parse.unquote(raw).strip()
            if decoded != raw:
                try:
                    data = json.loads(decoded)
                except json.JSONDecodeError:
                    data = None
        if data is None:
            # A browser's request-header copy is not a formal export, but can be
            # normalized when it contains recognizable YouTube cookie names.
            header = raw.strip()
            if header.lower().startswith("cookie:"):
                header = header.split(":", 1)[1].strip()
            pairs = []
            for part in header.split(";"):
                if "=" not in part:
                    continue
                name, value = part.strip().split("=", 1)
                if name in COOKIE_HEADER_NAMES and value:
                    pairs.append((name, value))
            if pairs:
                lines = ["# Netscape HTTP Cookie File", "# Converted from a Cookie request header on the runner."]
                lines.extend(f".youtube.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}" for name, value in pairs)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
                return f"converted-header:{len(pairs)}"
            return "unsupported"

    # A JSON secret can itself contain a JSON string, or be newline-delimited
    # cookie objects. Both forms are common in browser export tools.
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = None
    if data is None:
        objects = []
        for line in raw_lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                objects.append(item)
        data = objects

    lines = ["# Netscape HTTP Cookie File", "# This file was normalized on the ephemeral GitHub Actions runner."]
    converted = 0
    for cookie in _cookie_items(data):
        domain = str(cookie.get("domain") or cookie.get("host") or "").strip()
        name = str(cookie.get("name") or "").strip()
        if not domain or not name:
            continue
        host_only = bool(cookie.get("hostOnly", False))
        include_subdomains = "FALSE" if host_only and not domain.startswith(".") else "TRUE"
        if include_subdomains == "TRUE" and not domain.startswith("."):
            domain = "." + domain
        if bool(cookie.get("httpOnly", False)) and not domain.startswith("#HttpOnly_"):
            domain = "#HttpOnly_" + domain
        cookie_path = str(cookie.get("path") or "/")
        secure = "TRUE" if bool(cookie.get("secure", False)) else "FALSE"
        value = str(cookie.get("value") or "")
        lines.append("\t".join((domain, include_subdomains, cookie_path, secure, _expiry(cookie), name, value)))
        converted += 1

    if not converted:
        return "unsupported"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return f"converted-json:{converted}"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: normalize_youtube_cookies.py PATH", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.exists() or path.stat().st_size == 0:
        print("missing-or-empty")
        return 0
    print(normalize(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

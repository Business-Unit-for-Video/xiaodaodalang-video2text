import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from transcript_catalog import build_catalog
from transcription_integrity import (
    atomic_write_text_pair,
    probe_audio_duration,
    transcribe_audio_chunked,
    transcribe_audio_whole,
    validate_download_duration,
    validate_transcription,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANNEL_CONFIG = json.loads((REPO_ROOT / "channel.json").read_text(encoding="utf-8"))
CHANNEL_URL = os.getenv("YOUTUBE_CHANNEL_URL", CHANNEL_CONFIG["channel_url"]).strip()
COOKIES_FILE = Path(os.getenv("YOUTUBE_COOKIES_FILE", "youtube_cookies.txt"))

MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
LANGUAGE = os.getenv("WHISPER_LANGUAGE", "zh")
INITIAL_PROMPT = os.getenv("WHISPER_INITIAL_PROMPT", "").strip()

AUDIO_FORMAT = os.getenv("AUDIO_FORMAT", "mp3")
AUDIO_QUALITY = os.getenv("AUDIO_QUALITY", "7")
YOUTUBE_FALLBACK_CLIENTS = [
    client.strip()
    for client in os.getenv(
        "YOUTUBE_FALLBACK_CLIENTS",
        "default,web_safari,web_embedded,android",
    ).split(",")
    if client.strip()
]
BEAM_SIZE = int(os.getenv("BEAM_SIZE", "1"))
VAD_FILTER = os.getenv("VAD_FILTER", "1") in {"1", "true", "True"}
TRANSCRIBE_CHUNK_SECONDS = int(os.getenv("TRANSCRIBE_CHUNK_SECONDS", "1800"))
TRANSCRIBE_CHUNKED = os.getenv("TRANSCRIBE_CHUNKED", "0").strip().lower() in {"1", "true", "yes", "on"}
MAX_TRAILING_GAP_SECONDS = float(os.getenv("MAX_TRAILING_GAP_SECONDS", "120"))
MAX_TRAILING_GAP_RATIO = float(os.getenv("MAX_TRAILING_GAP_RATIO", "0.10"))

MEMBERSHIP_TITLE_RE = re.compile(
    r"(?:会员|member(?:s)?[- ]only|join this channel|members?[- ]exclusive)",
    re.IGNORECASE,
)

INCLUDE_MEMBERS = False
FORCE_RETRANSCRIBE = str(os.getenv("FORCE_RETRANSCRIBE", "false")).strip().lower() in {"1", "true", "yes", "on"}
GIT_BRANCH = os.getenv("GITHUB_REF_NAME", "").strip()


def log(msg: str):
    print(msg, flush=True)


def clean_url(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return text

    if text.startswith("[") and "](" in text and text.endswith(")"):
        m = re.match(r'^\[(?:.*?)\]\((https?://.+)\)$', text)
        if m:
            return m.group(1).strip()

    m = re.search(r'https?://[^\s]+', text)
    if m:
        url = m.group(0).strip()
        if url.endswith(")") and url.count("(") < url.count(")"):
            url = url[:-1]
        return url

    return text


def sanitize_key(name: str, max_len: int = 80) -> str:
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name)
    name = re.sub(r"\s+", "_", name)
    name = name.strip("._ ")
    return (name[:max_len] or "default")


DESTINATION_RAW = os.getenv("DESTINATION", CHANNEL_CONFIG["destination"]).strip()
DESTINATION = sanitize_key(DESTINATION_RAW or "default")
OUTPUT_ROOT_RAW = os.getenv("OUTPUT_ROOT", "").strip()
OUTPUT_ROOT = Path(OUTPUT_ROOT_RAW) if OUTPUT_ROOT_RAW else Path("youtube_channels")

STATE_DIR = Path("state_youtube") / DESTINATION
OUTPUT_DIR = OUTPUT_ROOT / DESTINATION
WITH_TS_DIR = OUTPUT_DIR / "with_timestamps"
PLAIN_DIR = OUTPUT_DIR / "plain"
TMP_DIR = Path("tmp_audio") / DESTINATION
ERRORS_DIR = STATE_DIR / "errors"

QUEUE_FILE = STATE_DIR / "queue.json"
DONE_FILE = STATE_DIR / "done.txt"
FAILED_FILE = STATE_DIR / "failed.txt"
PROGRESS_FILE = STATE_DIR / "progress.json"
CONTINUE_FLAG = STATE_DIR / "continue.flag"
MANIFEST_FILE = OUTPUT_DIR / "_manifest.json"


def ensure_dirs():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    WITH_TS_DIR.mkdir(parents=True, exist_ok=True)
    PLAIN_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8"):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding=encoding)
    tmp.replace(path)


def append_line(path: Path, line: str):
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_set(path: Path) -> set:
    if not path.exists():
        return set()
    return {x.strip() for x in path.read_text(encoding="utf-8").splitlines() if x.strip()}


def cookie_file_is_netscape(path: Path) -> bool:
    """Return whether a cookie export has the format yt-dlp can read."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                return line.startswith("# Netscape HTTP Cookie File") or line.startswith("# HTTP Cookie File")
    except OSError:
        return False
    return False


_invalid_cookie_warning_shown = False


def add_cookie_arg(cmd: List[str], enabled: bool = True) -> bool:
    """Add a cookie argument only for a valid Netscape cookie export."""
    global _invalid_cookie_warning_shown
    if not enabled:
        return False
    if cookie_file_is_netscape(COOKIES_FILE):
        cmd.extend(["--cookies", str(COOKIES_FILE)])
        return True
    if COOKIES_FILE.exists() and not _invalid_cookie_warning_shown:
        log("[warn] cookie file is not Netscape format; ignoring it and continuing without cookies")
        _invalid_cookie_warning_shown = True
    return False


def fallback_client_spec(client: str) -> tuple[str, bool]:
    """Return an extractor client spec and whether it may use cookies.

    android_vr is intentionally excluded: yt-dlp reports that it does not
    support cookies and often exposes only storyboard images.  The plain
    android client is used only as a last anonymous attempt with the
    documented missing-PO-token format mode.
    """
    name = client.strip()
    if name in {"android_vr", "tv_embedded"}:
        return "", False
    if name == "default":
        return "default,web_embedded", True
    if name == "web_safari":
        return "web_safari,web_embedded,-tv_downgraded", True
    if name == "android":
        return "android;formats=missing_pot", False
    return name, True


def run(cmd: List[str], capture: bool = False, check: bool = True):
    log("[cmd] " + " ".join(cmd))
    if capture:
        return subprocess.run(cmd, text=True, capture_output=True, check=check)
    return subprocess.run(cmd, text=True, check=check)


def git_run(cmd: List[str], check: bool = True):
    log("[git] " + " ".join(cmd))
    return subprocess.run(cmd, text=True, check=check)


def normalize_channel_url(url: str) -> str:
    url = clean_url(url)
    if not url:
        raise ValueError("YOUTUBE_CHANNEL_URL is empty")

    parts = urlsplit(url)
    clean_path = parts.path.rstrip("/")
    clean_path = re.sub(r"/(videos|streams|shorts|live|featured|playlists)$", "", clean_path)
    return urlunsplit((parts.scheme or "https", parts.netloc, clean_path, "", ""))


def seconds_to_mmss_mmm(sec: float) -> str:
    total_ms = max(0, int(round(sec * 1000)))
    total_seconds, ms = divmod(total_ms, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}.{ms:03d}"


def build_output_basename(item: Dict) -> str:
    vid = item.get("id") or "NOID"
    return f"{DESTINATION}_{vid}"


def plain_output_path(item: Dict) -> Path:
    return PLAIN_DIR / f"{build_output_basename(item)}.txt"


def ts_output_path(item: Dict) -> Path:
    return WITH_TS_DIR / f"{build_output_basename(item)}.txt"


def save_progress(status: str, note: str = "", current: Optional[Dict] = None, queue_total: int = 0, queue_index: int = 0):
    payload = {
        "status": status,
        "note": note,
        "destination": DESTINATION,
        "channel_url": clean_url(CHANNEL_URL),
        "include_members": INCLUDE_MEMBERS,
        "queue_total": queue_total,
        "queue_index": queue_index,
        "updated_at": int(time.time()),
        "updated_at_readable": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }
    if current:
        payload["current_video_id"] = current.get("id", "")
        payload["current_title"] = current.get("title", "")
        payload["current_url"] = clean_url(current.get("url", ""))
        payload["current_tab"] = current.get("tab", "")
    save_json(PROGRESS_FILE, payload)


def record_done(video_id: str):
    done = load_set(DONE_FILE)
    if video_id not in done:
        append_line(DONE_FILE, video_id)


def record_failed(video_id: str):
    failed = load_set(FAILED_FILE)
    if video_id not in failed:
        append_line(FAILED_FILE, video_id)


def clear_failed(video_id: str):
    """Remove a recovered video from the durable failed list."""
    failed = load_set(FAILED_FILE)
    if video_id not in failed:
        return
    remaining = sorted(failed - {video_id})
    atomic_write_text(FAILED_FILE, ("\n".join(remaining) + "\n") if remaining else "")


def write_error_file(item: Dict, err: Exception):
    path = ERRORS_DIR / f"{item['id']}.txt"
    body = (
        f"video_id: {item['id']}\n"
        f"title: {item.get('title', '')}\n"
        f"url: {clean_url(item.get('url', ''))}\n"
        f"tab: {item.get('tab', '')}\n"
        f"time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}\n"
        f"error: {repr(err)}\n"
    )
    atomic_write_text(path, body)


class MissingChannelTab(RuntimeError):
    """YouTube explicitly reports that this channel has no requested tab."""


def fetch_tab_entries(tab_url: str, use_cookies: bool) -> List[Dict]:
    tab_url = clean_url(tab_url)
    cmd = ["yt-dlp", "--remote-components", "ejs:github"]
    add_cookie_arg(cmd, enabled=use_cookies)
    cmd.extend(["--socket-timeout", "30", "--retries", "2",
                "--flat-playlist", "--dump-single-json", tab_url])

    result = subprocess.run(cmd, text=True, capture_output=True, timeout=300)
    log("[cmd] " + " ".join(cmd))

    raw = (result.stdout or "").strip()
    if result.returncode != 0 or not raw:
        requested_tab = urlsplit(tab_url).path.rstrip("/").rsplit("/", 1)[-1]
        absent_pattern = rf"\bdoes not have (?:a|an) {re.escape(requested_tab)} tab\b"
        if result.returncode != 0 and re.search(absent_pattern, result.stderr or "", re.IGNORECASE):
            raise MissingChannelTab(f"channel does not have a {requested_tab} tab")
        raise RuntimeError(f"cannot scan {tab_url}: {(result.stderr or '')[-1200:]}")

    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"invalid listing JSON for {tab_url}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries", []), list):
        raise RuntimeError(f"invalid listing envelope for {tab_url}")

    return data.get("entries") or []


def build_item_from_entry(entry: Dict, tab: str) -> Optional[Dict]:
    video_id = str(entry.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return None

    url = f"https://www.youtube.com/watch?v={video_id}"

    title = (entry.get("title") or video_id).strip()

    return {
        "id": video_id,
        "title": title,
        "url": url,
        "tab": tab,
        "duration": entry.get("duration"),
        "availability": entry.get("availability"),
        "live_status": entry.get("live_status"),
        "is_live": bool(entry.get("is_live")),
        "is_upcoming": bool(entry.get("is_upcoming")),
    }


def is_members_only(item: Dict) -> bool:
    """Conservatively exclude membership-only entries from a public queue."""
    availability = str(item.get("availability") or "").strip().lower()
    if availability and availability != "public":
        return True
    return bool(MEMBERSHIP_TITLE_RE.search(str(item.get("title") or "")))


def extract_queue_from_channel() -> List[Dict]:
    base = normalize_channel_url(CHANNEL_URL)

    scanned_tabs = []
    failures = []
    for tab in ("videos", "streams", "shorts"):
        try:
            scanned_tabs.append((tab, fetch_tab_entries(f"{base}/{tab}", use_cookies=False)))
        except MissingChannelTab as exc:
            failures.append(str(exc))
            log(f"[warn] {tab} tab unavailable: {exc}")
    if not scanned_tabs:
        raise RuntimeError("all YouTube channel tabs failed: " + "; ".join(failures))

    queue = []
    excluded_members = 0
    seen = set()

    for tab, entries in scanned_tabs:
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = build_item_from_entry(entry, tab)
            if not item:
                continue
            if is_members_only(item) or item.get("is_live") or item.get("is_upcoming") or item.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
                excluded_members += 1
                continue
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            queue.append(item)

    if not queue:
        raise RuntimeError("queue is empty")

    if excluded_members:
        log(f"[info] excluded non-public or unfinished live entries: {excluded_members}")
    return queue


def write_manifest(queue: List[Dict]):
    payload = {
        "destination": DESTINATION,
        "channel_url": clean_url(CHANNEL_URL),
        "include_members": INCLUDE_MEMBERS,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "items": queue,
    }
    save_json(MANIFEST_FILE, payload)


def rebuild_queue() -> List[Dict]:
    log("[info] rebuilding queue from youtube channel")
    queue = extract_queue_from_channel()
    save_json(QUEUE_FILE, queue)
    write_manifest(queue)
    build_catalog(REPO_ROOT)
    log(f"[info] queue saved: {len(queue)} items")
    return queue


def is_item_completed(item: Dict) -> bool:
    return plain_output_path(item).exists() and ts_output_path(item).exists()


def find_next_item(queue: List[Dict], done: set, failed: set) -> Optional[Dict]:
    for item in queue:
        vid = item["id"]
        # A manual retry may revisit failed items, but never reprocesses a completed item.
        if vid in done or (vid in failed and not FORCE_RETRANSCRIBE) or is_item_completed(item):
            continue
        return item
    return None


def has_more_pending(queue: List[Dict], done: set, failed: set) -> bool:
    return find_next_item(queue, done, failed) is not None


def download_audio(video_url: str, video_id: str) -> Path:
    raw_url = video_url
    video_url = clean_url(video_url)

    log(f"[debug] raw_url={raw_url}")
    log(f"[debug] cleaned_url={video_url}")

    if not video_url.startswith("http://") and not video_url.startswith("https://"):
        raise ValueError(f"invalid cleaned url for {video_id}: {video_url}")

    outtmpl = str(TMP_DIR / f"{video_id}.%(ext)s")
    base_cmd = ["yt-dlp", "--remote-components", "ejs:github"]
    add_cookie_arg(base_cmd)
    download_args = [
        "--no-playlist",
        "--match-filter", "availability = public & !is_live & !is_upcoming",
        "--socket-timeout", "30",
        "--retries", "2",
        "--fragment-retries", "2",
        # Some YouTube clients expose only a combined format.  Keep the
        # audio-only preference, but allow yt-dlp to select a combined stream
        # and extract its audio when no separate audio URL is available.
        "-f", "ba/bestaudio/best",
        "-x",
        "--audio-format", AUDIO_FORMAT,
        "--audio-quality", AUDIO_QUALITY,
        "-o", outtmpl,
        video_url
    ]
    cmd = base_cmd + download_args
    try:
        run(cmd)
    except subprocess.CalledProcessError as first_error:
        # GitHub-hosted IPs can be challenged by YouTube even for public
        # videos. Try a small set of documented public clients before failing.
        last_error = first_error
        for client in YOUTUBE_FALLBACK_CLIENTS:
            client_spec, use_cookies = fallback_client_spec(client)
            if not client_spec:
                log(f"[info] skipping unsupported cookie client: {client}")
                continue
            fallback_cmd = ["yt-dlp", "--remote-components", "ejs:github"]
            add_cookie_arg(fallback_cmd, enabled=use_cookies)
            fallback_cmd.extend(["--extractor-args", f"youtube:player_client={client_spec}"])
            fallback_cmd.extend(download_args)
            log(f"[warn] default YouTube client failed; retrying player client: {client_spec}")
            try:
                run(fallback_cmd)
                files = [p for p in TMP_DIR.glob(f"{video_id}.*") if p.is_file() and not p.name.endswith(".part")]
                if files:
                    break
            except subprocess.CalledProcessError as exc:
                last_error = exc
                continue
        else:
            raise last_error

    files = [p for p in TMP_DIR.glob(f"{video_id}.*") if p.is_file() and not p.name.endswith(".part")]
    if not files:
        raise RuntimeError(f"audio file not found for {video_id}")
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


def load_model():
    from faster_whisper import WhisperModel
    log(f"[info] loading model: {MODEL_NAME}, device={DEVICE}, compute_type={COMPUTE_TYPE}")
    return WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)


def transcribe_audio(model, audio_path: Path, audio_duration: float) -> Dict:
    kwargs = {
        "language": LANGUAGE if LANGUAGE else None,
        "beam_size": BEAM_SIZE,
        "vad_filter": VAD_FILTER,
        "condition_on_previous_text": False,
    }
    if INITIAL_PROMPT:
        kwargs["initial_prompt"] = INITIAL_PROMPT

    if TRANSCRIBE_CHUNKED:
        return transcribe_audio_chunked(model, audio_path, audio_duration, TMP_DIR, run, kwargs, seconds_to_mmss_mmm, TRANSCRIBE_CHUNK_SECONDS)
    return transcribe_audio_whole(model, audio_path, audio_duration, kwargs, seconds_to_mmss_mmm)


def write_outputs(item: Dict, result: Dict):
    ts_body = (result.get("timestamp_text", "").strip() + "\n") if result.get("timestamp_text") else ""
    plain_body = (result.get("plain_text", "").strip() + "\n") if result.get("plain_text") else ""
    atomic_write_text_pair(ts_output_path(item), ts_body, plain_output_path(item), plain_body)


def cleanup_temp_file(path: Optional[Path]):
    try:
        if path and path.exists():
            path.unlink(missing_ok=True)
    except Exception as e:
        log(f"[warn] cleanup failed: {e}")


def touch_continue():
    CONTINUE_FLAG.write_text("1\n", encoding="utf-8")


def clear_continue():
    CONTINUE_FLAG.unlink(missing_ok=True)


def git_commit_and_push(message: str):
    build_catalog(REPO_ROOT)
    git_run(["git", "add", str(OUTPUT_ROOT), "state_youtube", "docs"])
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        log("[info] no git changes to commit")
        return

    git_run(["git", "commit", "-m", message])

    if GIT_BRANCH:
        git_run(["git", "pull", "--rebase", "origin", GIT_BRANCH])
        git_run(["git", "push", "origin", f"HEAD:{GIT_BRANCH}"])
    else:
        git_run(["git", "push"])


def main():
    ensure_dirs()
    clear_continue()

    queue = rebuild_queue()
    done = load_set(DONE_FILE)
    failed = load_set(FAILED_FILE)

    next_item = find_next_item(queue, done, failed)
    if not next_item:
        save_progress("finished", note="all items completed", queue_total=len(queue), queue_index=len(queue))
        git_commit_and_push(f"youtube: finished {DESTINATION}")
        log("[info] all items completed")
        return

    audio_path = None
    idx = next((i for i, x in enumerate(queue) if x["id"] == next_item["id"]), 0)

    try:
        save_progress("downloading_audio", current=next_item, queue_total=len(queue), queue_index=idx)
        audio_path = download_audio(next_item["url"], next_item["id"])
        audio_duration = probe_audio_duration(audio_path, run)
        validate_download_duration(next_item, audio_duration)
        log(f"[ok] downloaded audio duration: {audio_duration:.3f}s")

        save_progress("transcribing", current=next_item, queue_total=len(queue), queue_index=idx)
        model = load_model()
        result = transcribe_audio(model, audio_path, audio_duration)
        validate_transcription(result, MAX_TRAILING_GAP_SECONDS, MAX_TRAILING_GAP_RATIO)

        save_progress("writing_outputs", current=next_item, queue_total=len(queue), queue_index=idx)
        write_outputs(next_item, result)

        record_done(next_item["id"])
        clear_failed(next_item["id"])
        done.add(next_item["id"])
        failed.discard(next_item["id"])

        if has_more_pending(queue, done, failed):
            touch_continue()
            save_progress(
                "done_one",
                note="one video processed, more pending",
                current=next_item,
                queue_total=len(queue),
                queue_index=idx + 1
            )
        else:
            clear_continue()
            save_progress(
                "finished",
                note="one video processed, no more pending",
                current=next_item,
                queue_total=len(queue),
                queue_index=idx + 1
            )

        git_commit_and_push(f"youtube: {DESTINATION} {next_item['id']}")
        log(f"[ok] completed: {next_item['id']} {next_item['title']}")

    except Exception as e:
        record_failed(next_item["id"])
        failed.add(next_item["id"])
        write_error_file(next_item, e)

        # Stop after a failed download/transcription. Automatic continuation
        # would repeatedly hit the same platform/authentication problem on
        # every pending video and can create an unbounded Actions chain.
        clear_continue()
        save_progress(
            "finished_with_errors",
            note=repr(e),
            current=next_item,
            queue_total=len(queue),
            queue_index=idx + 1
        )

        git_commit_and_push(f"youtube: failed {DESTINATION} {next_item['id']}")
        log(f"[error] {next_item['id']}: {e}")
        raise

    finally:
        cleanup_temp_file(audio_path)


if __name__ == "__main__":
    main()

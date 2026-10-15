import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from prepare_youtube_cookies import prepare_cookie_export
from transcript_catalog import build_catalog

spec = importlib.util.spec_from_file_location("channel_pipeline", ROOT / "scripts/transcribe_youtube_channel.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def test_public_listing_excludes_private_members_unlisted_and_live(monkeypatch):
    entries = [
        {"id": "public_0001", "title": "普通视频", "availability": "public"},
        {"id": "unknown_001", "title": "公开视频页面发现的条目"},
        *({"id": f"hidden{i:05d}", "availability": status} for i, status in enumerate(
          ["private", "unlisted", "subscriber_only", "premium_only", "needs_auth"])),
        {"id": "livevid0001", "availability": "public", "live_status": "is_live"},
        {"id": "upcoming001", "availability": "public", "is_upcoming": True},
    ]
    monkeypatch.setattr(pipeline, "fetch_tab_entries", lambda *args, **kwargs: entries)
    queue = pipeline.extract_queue_from_channel()
    assert [item["id"] for item in queue] == ["public_0001", "unknown_001"]


def test_all_tab_errors_fail_instead_of_finishing_empty(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("simulated network failure")
    monkeypatch.setattr(pipeline, "fetch_tab_entries", fail)
    with pytest.raises(RuntimeError, match="simulated network failure"):
        pipeline.extract_queue_from_channel()


def test_missing_stream_tab_does_not_lose_public_videos(monkeypatch):
    def listing(url, **kwargs):
        if url.endswith("/streams"):
            raise pipeline.MissingChannelTab("channel does not have a streams tab")
        return [{"id": "public_0001", "title": "已完成视频", "availability": "public"}]
    monkeypatch.setattr(pipeline, "fetch_tab_entries", listing)
    assert pipeline.extract_queue_from_channel()[0]["id"] == "public_0001"


def test_short_and_live_urls_are_canonical_watch_urls():
    for raw_url in ("https://www.youtube.com/shorts/ABCDEFGHIjk",
                    "https://www.youtube.com/live/ABCDEFGHIjk"):
        item = pipeline.build_item_from_entry({"id": "ABCDEFGHIjk", "url": raw_url}, "shorts")
        assert item["url"] == "https://www.youtube.com/watch?v=ABCDEFGHIjk"


@pytest.mark.parametrize("video_id", ["", "short", "ABCDEFGHIjkl", "ABCDEFGHIj!", "../badpath!"])
def test_invalid_video_ids_are_not_enqueued(video_id):
    assert pipeline.build_item_from_entry({"id": video_id}, "videos") is None


@pytest.mark.parametrize("message,missing", [
    ("This channel does not have a streams tab", True),
    ("HTTP Error 429: Too Many Requests", False),
    ("Unable to download webpage: timed out", False),
    ("This channel does not have a shorts tab", False),
])
def test_only_explicit_requested_tab_absence_is_ignorable(monkeypatch, message, missing):
    result = pipeline.subprocess.CompletedProcess([], 1, stdout="", stderr=message)
    monkeypatch.setattr(pipeline.subprocess, "run", lambda *args, **kwargs: result)
    expected = pipeline.MissingChannelTab if missing else RuntimeError
    with pytest.raises(expected) as exc:
        pipeline.fetch_tab_entries("https://www.youtube.com/@channel/streams", False)
    if not missing:
        assert not isinstance(exc.value, pipeline.MissingChannelTab)


@pytest.mark.parametrize("error", [
    RuntimeError("network unavailable"),
    RuntimeError("invalid listing JSON"),
    pipeline.subprocess.TimeoutExpired(["yt-dlp"], 300),
])
def test_partial_scan_failure_preserves_existing_manifest_and_queue(monkeypatch, tmp_path, error):
    queue_path = tmp_path / "queue.json"
    manifest_path = tmp_path / "_manifest.json"
    for path in (queue_path, manifest_path):
        path.write_text("existing contents", encoding="utf-8")
    monkeypatch.setattr(pipeline, "QUEUE_FILE", queue_path)
    monkeypatch.setattr(pipeline, "MANIFEST_FILE", manifest_path)
    def listing(url, **kwargs):
        if url.endswith("/videos"):
            return [{"id": "public_0001", "availability": "public"}]
        raise error
    monkeypatch.setattr(pipeline, "fetch_tab_entries", listing)
    with pytest.raises(type(error)):
        pipeline.rebuild_queue()
    assert queue_path.read_text(encoding="utf-8") == "existing contents"
    assert manifest_path.read_text(encoding="utf-8") == "existing contents"


def test_retry_never_reprocesses_completed_items(monkeypatch):
    monkeypatch.setattr(pipeline, "FORCE_RETRANSCRIBE", True)
    monkeypatch.setattr(pipeline, "is_item_completed", lambda item: False)
    queue = [{"id": "done"}, {"id": "failed"}]
    assert pipeline.find_next_item(queue, {"done"}, {"failed"})["id"] == "failed"


def test_catalog_uses_real_titles_and_only_existing_documents(tmp_path):
    config = {"destination": "channel", "repository": "example/channel-video2text",
              "channel_name": "频道", "channel_url": "https://www.youtube.com/@channel"}
    (tmp_path / "channel.json").write_text(json.dumps(config), encoding="utf-8")
    output = tmp_path / "youtube_channels/channel"
    output.mkdir(parents=True)
    (output / "_manifest.json").write_text(json.dumps({"items": [
        {"id": "video1", "title": "真实标题一", "duration": 61},
        {"id": "video2", "title": "真实标题二"},
    ]}), encoding="utf-8")
    for subdir in ("plain", "with_timestamps"):
        (output / subdir).mkdir()
        (output / subdir / "channel_video1.txt").write_text("正文", encoding="utf-8")
    catalog = build_catalog(tmp_path)
    first, second = catalog["items"]
    assert first["title"] == "真实标题一"
    assert first["transcript_status"] == "completed"
    assert first["plain_url"].endswith("/plain/channel_video1.txt")
    assert second["transcript_status"] == "pending"
    assert second["plain_url"] is None
    assert second["timestamps_url"] is None
    assert "真实标题一" in (tmp_path / "docs/video-index.md").read_text(encoding="utf-8")


def test_cookie_accepts_http_only_netscape_rows(tmp_path):
    path = tmp_path / "cookie.txt"
    counts = prepare_cookie_export(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\ttest-value\n", path
    )
    assert counts == (1, 1)


def test_invalid_cookie_is_removed_without_exposing_value(tmp_path):
    path = tmp_path / "cookie.txt"
    with pytest.raises(ValueError) as exc:
        prepare_cookie_export("invalid-secret-do-not-print", path)
    assert "invalid-secret-do-not-print" not in str(exc.value)
    assert not path.exists()


def test_cookie_accepts_json_lines(tmp_path):
    content = "\n".join(json.dumps({"domain": ".youtube.com", "name": name,
                                    "value": "test-value", "secure": True})
                        for name in ("SID", "HSID"))
    assert prepare_cookie_export(content, tmp_path / "cookies.txt") == (2, 2)


def test_download_failure_never_loads_whisper(monkeypatch):
    item = {"id": "video", "title": "失败视频", "url": "https://www.youtube.com/watch?v=video"}
    monkeypatch.setattr(pipeline, "rebuild_queue", lambda: [item])
    monkeypatch.setattr(pipeline, "load_set", lambda path: set())
    monkeypatch.setattr(pipeline, "find_next_item", lambda *args: item)
    for name in ("ensure_dirs", "clear_continue", "save_progress", "record_failed",
                 "write_error_file", "git_commit_and_push", "cleanup_temp_file"):
        monkeypatch.setattr(pipeline, name, lambda *args, **kwargs: None)
    calls = []
    monkeypatch.setattr(pipeline, "load_model", lambda: calls.append("model"))
    def failed_download(*args):
        raise RuntimeError("download unavailable")
    monkeypatch.setattr(pipeline, "download_audio", failed_download)
    with pytest.raises(RuntimeError, match="download unavailable"):
        pipeline.main()
    assert calls == []


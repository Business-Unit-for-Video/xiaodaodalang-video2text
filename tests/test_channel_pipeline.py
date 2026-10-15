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
        {"id": "public-one", "title": "普通视频", "availability": "public"},
        {"id": "unknown-one", "title": "公开视频页面发现的条目"},
        *({"id": status, "availability": status} for status in
          ["private", "unlisted", "subscriber_only", "premium_only", "needs_auth"]),
        {"id": "live", "availability": "public", "live_status": "is_live"},
        {"id": "upcoming", "availability": "public", "is_upcoming": True},
    ]
    monkeypatch.setattr(pipeline, "fetch_tab_entries", lambda *args, **kwargs: entries)
    queue = pipeline.extract_queue_from_channel()
    assert [item["id"] for item in queue] == ["public-one", "unknown-one"]


def test_all_tab_errors_fail_instead_of_finishing_empty(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("simulated network failure")
    monkeypatch.setattr(pipeline, "fetch_tab_entries", fail)
    with pytest.raises(RuntimeError, match="all YouTube channel tabs failed"):
        pipeline.extract_queue_from_channel()


def test_missing_stream_tab_does_not_lose_public_videos(monkeypatch):
    def listing(url, **kwargs):
        if url.endswith("/streams"):
            raise RuntimeError("no streams tab")
        return [{"id": "public-one", "title": "已完成视频", "availability": "public"}]
    monkeypatch.setattr(pipeline, "fetch_tab_entries", listing)
    assert pipeline.extract_queue_from_channel()[0]["id"] == "public-one"


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


"""Build a readable transcript index without downloading media or loading Whisper."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import quote


def build_catalog(repo_root: Path) -> dict:
    repo_root = Path(repo_root).resolve()
    config = json.loads((repo_root / "channel.json").read_text(encoding="utf-8"))
    destination = config["destination"]
    output = repo_root / "youtube_channels" / destination
    manifest_path = output / "_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"items": []}
    failed_file = repo_root / "state_youtube" / destination / "failed.txt"
    failed = set(failed_file.read_text(encoding="utf-8").splitlines()) if failed_file.exists() else set()
    records = []
    counts = {"completed": 0, "pending": 0, "failed": 0, "partial": 0}
    for item in manifest.get("items", []):
        video_id = item["id"]
        plain = output / "plain" / f"{destination}_{video_id}.txt"
        timestamps = output / "with_timestamps" / f"{destination}_{video_id}.txt"
        plain_exists = plain.is_file() and plain.stat().st_size > 0
        timestamps_exist = timestamps.is_file() and timestamps.stat().st_size > 0
        status = ("completed" if plain_exists and timestamps_exist else
                  "failed" if video_id in failed else
                  "partial" if plain_exists or timestamps_exist else "pending")
        counts[status] += 1

        def link(path: Path, exists: bool) -> str | None:
            if not exists:
                return None
            relative = quote(path.relative_to(repo_root).as_posix(), safe="/")
            return f"https://github.com/{config['repository']}/blob/main/{relative}"

        records.append({
            "id": video_id,
            "title": item.get("title") or video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "duration": item.get("duration"),
            "tab": item.get("tab"),
            "availability": item.get("availability"),
            "transcript_status": status,
            "plain_url": link(plain, plain_exists),
            "timestamps_url": link(timestamps, timestamps_exist),
        })
    catalog = {
        "channel_name": config["channel_name"],
        "channel_url": config["channel_url"],
        "repository": config["repository"],
        "destination": destination,
        "generated_at": manifest.get("generated_at"),
        "counts": counts,
        "items": records,
    }
    docs = repo_root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "video-catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    names = {"completed": "已转写", "pending": "待转写", "failed": "失败待处理", "partial": "文件不完整"}
    lines = [
        f"# {config['channel_name']}：视频与文档目录",
        "",
        f"来源：[YouTube 频道]({config['channel_url']})。",
        "",
        f"共 {len(records)} 条：已转写 {counts['completed']}，待转写 {counts['pending']}，"
        f"失败待处理 {counts['failed']}，文件不完整 {counts['partial']}。",
        "",
        "标题取自 YouTube 视频清单；只有实际生成的文本才显示文档链接。",
        "",
        "| 视频 | 时长 | 状态 | 纯文本 | 时间戳文本 |",
        "|---|---:|---|---|---|",
    ]
    for item in records:
        title = str(item["title"]).replace("\\", "\\\\").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]").replace("\n", " ")
        duration = item["duration"]
        duration_text = f"{int(float(duration)) // 60}:{int(float(duration)) % 60:02d}" if duration else "未知"
        plain_link = f"[打开]({item['plain_url']})" if item["plain_url"] else "—"
        ts_link = f"[打开]({item['timestamps_url']})" if item["timestamps_url"] else "—"
        lines.append(f"| [{title}]({item['url']}) | {duration_text} | {names[item['transcript_status']]} | {plain_link} | {ts_link} |")
    (docs / "video-index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="离线生成视频和转录文档目录")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    catalog = build_catalog(args.repo_root)
    print(json.dumps({"videos": len(catalog["items"]), **catalog["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()


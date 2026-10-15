# 小岛大浪吹-非正经政经频道：视频转文字

这是 [小岛大浪吹-非正经政经频道](https://www.youtube.com/@xiaodaodalang) 的公开转录仓库，基于组织的 [Video2Text](https://github.com/Business-Unit-for-Video/Video2Text) 和 [zrzjpl-video2text](https://github.com/Business-Unit-for-Video/zrzjpl-video2text) 建立。

[查看视频与文档目录](docs/video-index.md) · [机器可读目录](docs/video-catalog.json) · [运行转录](https://github.com/Business-Unit-for-Video/xiaodaodalang-video2text/actions/workflows/transcribe_youtube_channel.yml)

## 如何运行

在 Actions 打开“转写 YouTube 频道”，点击 **Run workflow**：

- `whisper_model`：默认 `medium`，沿用 Video2Text 的精度设置；快速验证可选 `small`。首次单条验证使用 `small`。
- `force_retranscribe`：勾选后允许重试以前失败的项目，已完成项目仍会跳过。
- `continue_chain`：默认开启。一条视频成功后自动触发下一次运行；关闭时只处理一条。

全部下载和 Whisper 转录在 GitHub Actions 上执行，不需要在本机运行 Whisper。每个 run 只处理一条视频并立即提交文本及状态，后续运行从未完成项目继续。每六小时扫描一次新视频，扫描公开的视频、直播回放和 Shorts 并按视频 ID 去重。

发现列表不使用账号 Cookie，不收录私密、非公开、会员专属以及尚未结束的直播或预约视频。只有频道公开页面可发现的条目会进入队列。所有标签页读取失败会明确报错；频道不存在的单个标签页只记录警告。

## 输出目录

- `youtube_channels/xiaodaodalang/plain/`：纯文本。
- `youtube_channels/xiaodaodalang/with_timestamps/`：带时间戳文本。
- `youtube_channels/xiaodaodalang/_manifest.json`：真实视频标题、来源 URL 和视频元数据。
- `state_youtube/xiaodaodalang/`：队列、完成和失败 ID、错误原因与当前进度。
- `docs/video-index.md`：按真实标题浏览的视频与转录文档目录。
- `docs/video-catalog.json`：供 `authorized-media-publisher` 同步的文档清单。

目录只为已经生成的文件提供文本链接；尚未完成的条目标为“待转写”。文档列表可独立离线更新，不需要安装或运行 Whisper：

```powershell
python scripts/transcript_catalog.py
```

## Cookie 与失败处理

仓库级 Secret 名称为 `YOUTUBE_SOURCE_COOKIE_FILE_VIDEO2TEXT`。接受 Netscape cookies.txt、浏览器 JSON/JSONL 或 Cookie 请求头，运行时归一化；Cookie 不提交到仓库。未配置时先使用公开访问；若 YouTube 对 GitHub 机器要求登录，请更新此 Secret 后重新运行。

下载或转录失败会记录错误并停止当次自动续跑，防止账号验证问题在整个队列反复重试。六小时定时扫描仍会尝试其他待处理项目；更新 Cookie 或解决错误后，可以勾选 `force_retranscribe` 重试。音频下载完整性及转录末尾覆盖情况均有检查，不会把不完整文本标记完成。

本仓库只负责转录。B 站发布由 [authorized-media-publisher](https://github.com/Business-Unit-for-Video/authorized-media-publisher) 的独立频道入口手动执行，转录成功不会自动投稿。


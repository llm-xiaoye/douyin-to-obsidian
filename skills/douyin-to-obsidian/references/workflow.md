# 底层提取与排障

常规导入使用 cc-connect.md 中的统一队列。以下命令用于本地媒体和排障，不替代知识提炼。

本机 Python：`<python>`。
本机 skill：`<skill>`。

## 依赖

Python 3.12、yt-dlp、imageio-ffmpeg、mlx-whisper、PyYAML、F2、Pillow；项目根目录 requirements.lock.txt 固定当前安装版本。视频 ASR 需要 Apple Silicon，图文 OCR 使用 macOS Vision 和 Xcode Command Line Tools。默认 Whisper 模型为 mlx-community/whisper-small-mlx，首次使用下载，之后复用本机缓存。

```sh
<python> <skill>/scripts/pipeline.py prepare <链接或分享文本>
<python> <skill>/scripts/pipeline.py prepare <本地视频> --url <抖音原链接>
<python> <skill>/scripts/pipeline.py extract <返回的bundle.json>
```

prepare 识别官方 `/video/`、`/note/`、modal_id 和短链。视频通过 yt-dlp 获取容器并只解码真实音轨；失败时自动恢复详情，并可下载 F2 返回的视频。图文使用完整图片列表并按页缓存。详情获取按 douyin_access.py 的有限恢复流程自动刷新请求会话，必要时回退官方公开分享页。不能将图文的背景音乐送入 ASR 作为文章内容。numeric ID 无媒体类型时可通过详情回退识别图文。

extract（兼容名 transcribe）按 media_kind 分流：视频本地 MLX Whisper，图文本地 Apple Vision。两者都返回 transcript.json；图文记录 pages、OCR 文本、置信度、边框、图片哈希和 caption，无伪造语音时间戳。

当前模型读取完整证据，图文还要逐页读取图片，随后写 knowledge.md 和 analysis.json，格式见 knowledge-notes.md。保存时：

```sh
<python> <skill>/scripts/pipeline.py save <bundle.json> --knowledge-file <knowledge.md> --analysis <analysis.json>
```

save 核对证据 ID 与内容哈希、learning_review、图文页数、reviewed_pages 及每页原图占位符，再由 library.py 事务写入文章、原图、索引和日志。不要直接写到 Vault 来绕过校验。`--summary` 是 knowledge-file 的兼容别名。显式 `--archive-source` 可另外保存原始材料附件，仅用于用户要求且内容允许的情况；音频/转写额外归档默认关闭，图文原图默认保存并嵌入。

## 常见失败与恢复

| 环节 | 可观察原因 | 处理方式 |
| --- | --- | --- |
| resolve/plan | 非官方域名、失效短链、无作品 ID | 核对用户本次输入；不根据旧消息猜作品。 |
| detail / Fresh cookies | 接口未返回匹配媒体，不能据此认定 Cookie 过期 | 按 [recovery.md](recovery.md) 在原批次自动打开浏览器一次，并有界等待迟到响应；耗尽后停止，agent 不另建批次绕过上限。 |
| images | 某页缺地址、下载或解码失败 | 保留缓存，重试获取完整图片列表；不跳过缺页后称完整。 |
| ocr | 缺少 xcrun/Swift 或图像无法识别 | 检查 `xcrun swift --version` 和 Pillow；无文字页交由模型读图核对。 |
| extract/ASR | 无真实音轨、空转写或模型缺失 | 不生成空笔记；核对本地依赖/模型，必要时使用用户提供的视频。 |
| analysis | 专业词识别不明、图片关键文字读不清 | 记录具体疑点；不足以支持知识时 fail/skip，不补造内容。 |
| commit | analysis 缺字段、页码不全、证据改变 | 修正分析文件或重新读取证据后提炼，再提交。 |
| library | 索引管理标记冲突、中断后文件被改动 | 保留现场及备份，恢复一致状态；不覆盖个人改动。 |

单条失败记入运行 state.json。`next` 可以继续下一项；再次提交同一批原始输入时，已有作品跳过、失败项可以重试。收藏夹数量与实际分页不符时仍报告缺口，不无限循环。

## 实现依据

图片识别使用 Apple 的 [VNRecognizeTextRequest](https://developer.apple.com/documentation/vision/vnrecognizetextrequest)，设置准确识别、简体中文和英文。图片列表使用已安装 [F2](https://github.com/Johnserf-Seed/f2) 的 PostDetail 接口；本脚本只保留必要媒体元数据，不记录 Cookie 或请求凭据。

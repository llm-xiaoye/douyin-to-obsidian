# GitHub 调研与选型

调查日期：2026-09-13。能力依据仓库文档及以下代码检查；仅本地验证记录中列出的流程属于实测。

| 项目 | 可以借鉴的部分 | 对本任务的判断 |
| --- | --- | --- |
| [chubbyguan/chubbyskills：douyin-transcribe](https://github.com/chubbyguan/chubbyskills/tree/main/douyin-transcribe) | 现成的“抖音链接 → 音轨 → SenseVoice → Markdown” skill | 最接近单条需求；还需加收藏夹、知识整理和 Obsidian 写入。“无需 Cookie”不能视为始终有效的保证。 |
| [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2) | 区分收藏作品、收藏夹清单、指定收藏夹视频 | 适合作为批量入口；选为可选收藏夹适配器。 |
| [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader) | 单条、批量下载、SQLite 去重和转写模块 | 可作下载后端替代；需核对当前分支接口。 |
| [MQTQW/douyinmind](https://github.com/MQTQW/douyinmind) | 登录 → 收藏夹 → 音频 → ASR → RAG 整体架构 | 可借鉴采集和媒体模块；直接写 Obsidian 无需部署 React、ChromaDB 及对话服务。本次未部署。 |
| [Mintnoii/DouYin-Downloader](https://github.com/Mintnoii/DouYin-Downloader) | README 提供 `--collect-ids`、`--list-collections` | 与 jiji262 的版本和参数不同，不能混用。 |

还发现了 [BiliNote](https://github.com/JefferyHcool/BiliNote) 和 [obsidian-clipper-wechat-feishu](https://github.com/destineylu/obsidian-clipper-wechat-feishu)，分别可参考视频笔记和抖音媒体剪藏。本次未验证这两个项目能完成指定收藏夹到语音知识的完整流程，因此未选为当前后端。

## 关键代码证据

- F2 的 [user-collects.py](https://github.com/Johnserf-Seed/f2/blob/7dab3e2ffffaa2535834d28fca99dbc2e89fa9d3/docs/snippets/douyin/user-collects.py) 和 [handler.py](https://github.com/Johnserf-Seed/f2/blob/7dab3e2ffffaa2535834d28fca99dbc2e89fa9d3/f2/apps/douyin/handler.py) 提供收藏夹列表和接收 `collects_id` 的单夹视频枚举。
- jiji262 的 [collect_strategy.py](https://github.com/jiji262/douyin-downloader/blob/1f540317aa09338e1c33abdd6099c53c1918caf5/core/user_modes/collect_strategy.py) 支持指定 `collects_id`；不传则枚举所有收藏夹，封装时必须保留筛选。
- DouyinMind 的 [media_service.py](https://github.com/MQTQW/douyinmind/blob/50e12ef65b781f7b72bf16e3d790eb5ebe8bf4a5/backend/app/services/media_service.py) 使用 yt-dlp 的 `bestaudio/best` 再经 ffmpeg 转换。本 skill 独立实现类似调用，没有复制其业务代码。
- [yt-dlp 的 Douyin 提取器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/tiktok.py) 在详情解析失败时提示需要新 Cookie；支持 Douyin 不意味着匿名请求必定可用。
- [Apple MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) 提供本地推理和时间戳，适合当前 Apple Silicon 环境。

## 采用的流程

```text
指定收藏夹 → F2 获取精确视频 ID（可选入口）
                    ↓
单条链接 → yt-dlp + 本机浏览器 Cookie
                    ↓
              ffmpeg 提取真实音轨
                    ↓
              MLX Whisper 本地中文转写
                    ↓
          当前会话根据转写整理知识要点
                    ↓
         Obsidian 知识笔记 + 时间线 + 来源
```

Cookie 和临时 CDN 地址不写进笔记。视频仅语音理解，无需抽帧；图文使用逐页 OCR 与读图。音频和转写默认保留在工作缓存；可归档用户提供的原媒体。转写文本会进入当前会话，不宣称知识整理也完全离线。当前能力与排障方式见 [workflow.md](workflow.md)。

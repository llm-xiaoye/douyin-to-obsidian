# douyin-to-obsidian

把抖音视频、图文和收藏夹中的知识整理到自己的 Obsidian。面向 **cc-connect + Claude Code** 的 /dy 技能：Python 负责材料获取、本地识别、查重和事务写入，当前会话的模型负责理解、纠错和知识提炼。

Douyin videos, image posts and collections → structured Obsidian notes. Full support: Apple Silicon macOS, Chrome, Python 3.12 and a configured cc-connect Claude Code workspace.

## 能做什么

- 导入单个作品、分享口令或多个链接；收藏夹只补充尚未入库的作品。
- 视频使用本地 MLX Whisper 转写；图文逐页 Apple Vision OCR，再由支持读图的模型核对原图。
- 按主题生成知识笔记、自测和有依据的模型补充，附上原作品链接。
- 用作品 ID 查重；已有笔记改名、移动后仍能识别，不覆盖其正文或个人修改。
- 笔记、图文附件、知识总索引、导入总日志通过同一事务提交，失败时回滚。
- 保留批次状态、有限浏览器恢复、清单缺口和逐条失败链接；不无限重试。
- 粘贴包含“复制打开抖音”的分享口令可以自动路由；普通讨论或明确拒绝导入时不启动导入。

这不是独立的自动摘要服务。只运行 Python 不会代替模型生成知识正文；宿主需要 Read、Write、命令执行和读图能力。公开视频也可能受平台接口、网络或权限限制，不能保证任意链接均可访问。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Apple Silicon Mac；Intel Mac、Windows、Linux 未验证，不提供完整支持 |
| Python | 3.12；安装命令使用你自己的解释器绝对路径 |
| Node.js | 本机可执行的 Node.js，供 F2 的 JavaScript 签名使用 |
| Chrome | 已安装，使用你自己的用户资料；私人收藏夹需要该资料已登录抖音 |
| OCR | Xcode Command Line Tools，包含 xcrun 和 Swift |
| 宿主 | 已配置好模型和渠道的 cc-connect，后端为 Claude Code |
| Obsidian | 已存在、可读写的 Vault；无须额外 Obsidian 插件 |

安装器只配置所选工作区的 /dy 技能、命令与路由 Hook，不配置模型账号或聊天渠道。先参照 [cc-connect 官方说明](https://github.com/chenhg5/cc-connect) 完成宿主配置。

如果尚未安装 Command Line Tools，可在终端运行：

~~~sh
xcode-select --install
~~~

## 1. 下载

在 GitHub 页面选择 Code → Download ZIP，解压后进入目录；也可以：

~~~sh
git clone https://github.com/llm-xiaoye/douyin-to-obsidian.git
cd douyin-to-obsidian
~~~

建议使用 [Releases](https://github.com/llm-xiaoye/douyin-to-obsidian/releases) 中的版本。ZIP 与 Git clone 使用相同安装步骤。

## 2. 确定自己的路径

你需要提供：

1. **cc-connect 工作区**：该项目中 Claude Code 实际使用的 work_dir，不是 cc-connect 程序目录。
2. **Obsidian Vault**：你的知识库根目录，里面可以已有其他笔记。
3. **Chrome Profile**：在用于登录抖音的 Chrome 中打开 chrome://version，复制“个人资料路径 / Profile Path”。例如 Default 或 Profile 1 所在的完整目录，不能只填用户数据父目录。

默认 Chrome 数据目录可自动推导对应缓存。自定义 --user-data-dir 必须另传 --browser-cache-dir，指向同一资料的 Cache_Data。安装器不会创建 Chrome Profile、复制 Cookie 或要求你导出登录凭据。

## 3. 安装

在下载目录执行，先把下面四个值换成你自己的绝对路径。保留双引号以支持中文和空格：

~~~sh
DY_PYTHON="/absolute/path/to/python3.12"
DY_WORKSPACE="/absolute/path/to/cc-connect-workspace"
DY_VAULT="/absolute/path/to/YourVault"
DY_PROFILE="$HOME/Library/Application Support/Google/Chrome/Default"

"$DY_PYTHON" -B scripts/install.py \
  --workspace "$DY_WORKSPACE" \
  --vault "$DY_VAULT" \
  --browser-profile "$DY_PROFILE"
~~~

其他可选参数：

~~~text
--browser-cache-dir "/absolute/path/to/Chrome/Cache_Data"
--cache-dir "/absolute/path/to/dy-runtime-cache"
~~~

默认运行缓存为当前用户的 ~/Library/Caches/douyin-to-obsidian。依赖和首次下载的识别模型可能需要较多磁盘空间及网络流量。

安装器会创建：

~~~text
工作区/
└── .claude/
    ├── skills/douyin-to-obsidian/    技能、独立 .venv、本地 config.json
    ├── commands/dy.md               本机路径生成的 /dy 命令
    └── settings.json               合并 UserPromptSubmit Hook
~~~

已有设置会保留，首次修改前留本地备份；重复执行相同安装命令不会重复添加 Hook。已有非受管安装、软链接或被手工改过的命令出现冲突时会停止，请先备份处理，不要盲目删除原目录。

首次使用全新 Vault 时，安装器会在两份总览都不存在的情况下创建空的知识总索引.md 和导入总日志.md。已有且格式有效的总览会原样保留；只存在其中一份、使用符号链接或格式不兼容时会停止，避免覆盖你自己的索引。请先备份检查，或选择独立 Vault。初始化总览不会自动扫描、迁移已有笔记。

如果依赖安装中断，失败目录会保留；请保存错误信息，把该不完整安装移到备份位置后再安装。安装器不会把不完整目录当成成功安装继续使用。

## 4. 检查配置

安装成功会输出 doctor_command，原样复制运行：

~~~sh
"$DY_WORKSPACE/.claude/skills/douyin-to-obsidian/.venv/bin/python" -B \
  "$DY_WORKSPACE/.claude/skills/douyin-to-obsidian/scripts/doctor.py"
~~~

ok 为 true 表示本机依赖、路径和集成检查通过。doctor 不读取 Cookie、不下载作品、不写笔记，也不证明当前抖音登录或网络可用。首次真实转写会下载默认 Whisper 模型。

在使用这个工作区的 **新 cc-connect 会话** 中尝试 /dy。宿主需允许所需工具，不能处于仅规划模式；首次控制 Chrome 或读取本机凭据时可能出现系统权限提示。技能只输出最终中文汇报，聊天平台自身的工具状态展示由宿主控制。

## 5. 使用

| 发送内容 | 行为 |
| --- | --- |
| /dy 抖音作品链接 | 导入这个视频或图文 |
| /dy 链接一 链接二 | 处理本次列出的作品，合并重复项 |
| 含“复制打开抖音”和完整链接的分享文本 | 自动进入同一增量流程 |
| /dy 收藏夹名称 | 精确匹配收藏夹，只处理新增作品 |
| /dy 收藏夹一,收藏夹二 | 多收藏夹去重后处理 |
| /dy 收藏夹ID:数字ID | 收藏夹重名时指定 ID |
| /dy --list | 列出收藏夹名称、ID、数量，不导入 |

例如先发送 /dy --list，再发送列表中确切的收藏夹名称。链接和收藏夹参数请分开发送。已有笔记保持不变；失败汇报包含具体环节、完整作品链接和可复制的 retry_command，由你决定是否重试。

**只有标题、空音轨或缺页图片时不会编写成功笔记。** 视频必须有完整可用的讲解材料；图文需要模型实际核对每页原图。

## 配置项

实际配置位于安装目录的 config.json。仓库中的 config.example.json 是结构示例，安装器会生成真实路径；不要把本地配置提交到公开仓库。

| 配置项 | 用途 / 默认值 |
| --- | --- |
| vault | Obsidian 根目录，由 --vault 生成 |
| python | 安装内 .venv/bin/python，由安装器生成 |
| cache_dir | 运行批次、媒体、识别结果和恢复记录；可用 --cache-dir 指定 |
| browser、browser_profile | chrome 及你的完整 Profile 目录 |
| browser_cache_dir | 同一 Profile 的 Cache_Data 目录 |
| notes_dir | 知识库；当前统一目录和主题分类合同使用此名称 |
| attachments_dir | 附件；图文图片按文章分目录保存 |
| review_state_file | .dy-review-state.json，复审技术状态 |
| unified_only | true，使用统一总索引和总日志 |
| asr_model | mlx-community/whisper-small-mlx；改变模型需自己验证效果 |
| prepare_workers | 2；支持 1–3，准备可并行，模型提炼和提交逐条进行 |
| auto_browser_recovery | true，详情获取失败时有界打开浏览器恢复 |
| background_detail_first | true，优先后台详情路线 |
| prefer_browser_media | false，保留现有后台优先策略 |
| close_opened_tabs | true，只尝试关闭本次新开的匹配作品标签页 |
| browser_recovery_wait_seconds | 8，范围 0–20 |
| browser_media_timeout_seconds | 40，范围 0–40 |

目录结构会生成知识库/主题目录、附件/文章目录，以及 Vault 根目录的知识总索引.md 和导入总日志.md。知识提炼规则见 [知识笔记规范](skills/douyin-to-obsidian/references/knowledge-notes.md)。

config.json 的变更作用于新批次；已有批次保存着创建时的配置快照。手动修改后重新运行 doctor，再创建新批次。统一目录合同的名称不建议任意修改。

## 数据与隐私

| 数据 | 去向 |
| --- | --- |
| Chrome Cookie | 仅在本机请求进程内使用，不要求粘贴到聊天，不导出成文件 |
| 音轨和 OCR | 在本机处理，默认媒体和完整识别结果留运行缓存 |
| 文本、图片、知识正文 | 当前宿主模型会读取材料；是否发送到云端由你的模型服务配置决定 |
| 图文原图 | 事务提交到自己的 Vault 附件目录，并嵌入笔记 |
| 作品链接、作者元数据 | 保存到本地笔记和导入日志 |
| 依赖与模型下载 | Python 包源、固定的 F2 GitHub 源码及模型仓库 |

本项目没有新增遥测或独立模型服务。缓存和安装设置可能包含你的路径、作品记录和材料，分享故障报告前请自行脱敏。仓库只包含通用代码、示例、测试和说明，不包含作者的个人配置或知识库。

请只处理自己有权访问和使用的内容。私人、已删除、受限或仅配乐内容会按实际材料和错误处理；不绕过登录、验证码或访问权限。

## 常见问题

| 问题 | 排查方式 |
| --- | --- |
| /dy 没有命中 | 确认 cc-connect 使用了安装时同一工作区，新建会话并运行 doctor |
| 分享口令没有自动识别 | 检查 UserPromptSubmit Hook；显式 /dy 仍可单独测试 |
| 收藏夹为空或鉴权失败 | 用配置的 Chrome Profile 登录；公开视频可访问不证明私人收藏夹可读取 |
| Fresh cookies / DETAIL_UNAVAILABLE | 它不等同于登录过期；按恢复文档查看实际阶段和清单完整性 |
| CACHE_LOOKUP_INCOMPLETE | 缓存查找未完成，不代表作品无媒体，保留原批次诊断 |
| OCR 缺少 Swift | 安装 Command Line Tools，运行 doctor；不把配乐替代为图文材料 |
| ASR 模型下载失败 | 检查模型仓库网络和空间；已准备好的媒体仍在缓存 |
| 新笔记未保存 | 查看 commit 的字段、骨架、页码或事务错误，不能手工绕过校验 |

详细流程：[cc-connect 队列](skills/douyin-to-obsidian/references/cc-connect.md) · [底层提取](skills/douyin-to-obsidian/references/workflow.md) · [恢复边界](skills/douyin-to-obsidian/references/recovery.md)

## 开发与验证

使用安装成功的 Python，在下载仓库根目录执行：

~~~sh
"$DY_WORKSPACE/.claude/skills/douyin-to-obsidian/.venv/bin/python" -B \
  -m unittest discover -s tests -v
~~~

测试使用临时目录和合成材料，不访问你的个人 Vault 或 Cookie。发布验证的具体范围见 [验证报告](docs/validation.md)；单元测试、真实 ASR/OCR 和在线媒体获取分别记录。

升级前备份本地 config.json 和工作区设置。删除本技能时移除其安装目录、/dy 命令及对应 Hook 即可；保留 Vault 和你希望留存的缓存。

## 许可

本项目采用 [MIT](LICENSE)。第三方依赖和识别模型按各自许可提供。本项目不附带修改过的 cc-connect 二进制。

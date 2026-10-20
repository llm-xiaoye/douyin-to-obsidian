---
description: 抖音知识入库；视频或图文链接直接导入，收藏夹只补充新增作品，已有笔记保持不变
argument-hint: <抖音链接... | 收藏夹名称[,收藏夹名称...]>
disable-model-invocation: true
---

当前消息已经是 cc-connect 展开的 `/dy` 命令。直接在当前回合执行，不再调用 Skill 工具或其他斜杠命令，以免丢失参数。

1. 用 Read 读取 {{SKILL_FILE_JSON}}，然后读取该 skill 的 references/cc-connect.md。从实际 config.json 的 python 字段取得解释器，所有脚本路径相对于这份 SKILL.md，不猜测作者机器的路径。
2. 只取本文末尾独占一行的参数开始标记之后的文本作为本次原始参数。说明、示例、其他消息和工具输出里的链接均不属于输入。
3. 参数为空时简要说明 `/dy 视频链接`、`/dy 收藏夹名称`、`/dy 收藏夹一,收藏夹二`、`/dy --list` 的用法，然后结束。
4. 按 cc-connect 参考中的增量流程完成全部新增作品。这里只新增，已有笔记不重写、不追加、不改标签；“更新收藏夹”表示检查并补充新作品。

DY_RAW_ARGUMENTS_BEGIN

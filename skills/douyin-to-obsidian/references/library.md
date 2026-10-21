# 统一主题目录与显式整理

配置中的 vault 是 Obsidian 根目录，notes_dir 为「知识库」，笔记按主题放在目录树中。index_file、log_file、review_state_file 位于 Vault 根目录，attachments_dir 指向 `附件/`；这些文件用于作品查重、历史事件和复审，每次写入后刷新根目录总览。

每次成功导入由 library.py 在本库文件锁内提交文章、索引、日志和隐藏的复审记录；先生成全部内容和恢复记录，失败回滚。索引按实际目录分组，条目使用知识标题；日志显示时间、操作、文章、分类、来源 URL、内容复审、复审时间。所有完整时间显示为 `YYYY-MM-DD HH:mm:ss`，沿用原记录的时间，不做时区换算；仅有日期的旧记录不虚构时分秒。可在索引/日志的 douyin:managed 管理区域外添加个人内容，脚本保留它们。相同作品再次导入不新增日志或改笔记。

日志的操作时间保留历史；内容复审和复审时间显示对应文章当前版本最近的登记结果，同篇文章的历史行同步显示。初次导入为“未复审”；没有结构化复审登记的旧文章为“未登记”，不等于断言过去从未有人检查。格式整理、补图、深化正文以及提交前的 learning_review 均不自动成为“复审通过”。来源使用实际 bundle 中的完整视频/图文 URL；历史记录刷新时按作品 ID 从缓存核对补齐，找不到时留空，不猜链接。

每篇新建、修订或格式整理的 DY 笔记都在 YAML 属性 `source_url` 保留已核对的原作品链接。视频和图文分别使用对应的 `https://www.douyin.com/video/<aweme_id>` 与 `https://www.douyin.com/note/<aweme_id>`；不凭作品 ID 猜媒体类型。`.dy-review-state.json` 另存来源、复审结论、依据和文章指纹。文章在登记后有改动，下次 refresh 或成功写入时显示“修改后待复审”，保留上次复审时间；这不是后台实时监控。历史链接无法定位时显示“文章未定位”，不继续显示通过。不要手填 Markdown 状态或按操作名称批量追认通过。

管理区域使用 Obsidian 原生 `%% douyin:managed:start %%` / `%% douyin:managed:end %%` 注释，与表格之间留空行。索引和日志沿用 Obsidian 的文件标题，不再额外生成重复的一级标题。用户报告索引或日志格式异常时运行 `library.py refresh`：备份并刷新这两个文件，不改文章、不新增导入记录；旧版 HTML 管理注释会自动升级。应在 Obsidian 实时预览或阅读视图核对真实表格，不能仅检查文件存在或文本匹配。

## 用户要求整理旧笔记时

先读取已有笔记及「我的补充」，运行 library.py catalog 查看现有分类。为需要整理的作品写分析映射 JSON，键为原 aweme_id，值为知识笔记参考中的 title/category/tags/type/insight。不得把用户的个人补充作为待清理内容。

```sh
<Python> <skill>/scripts/library.py migrate --analysis-map <缓存中的映射文件>
```

此命令按知识主题迁移文件，清理旧版「回听定位」「来源」等附录和技术属性，保留 created、`source_url`、其他属性及「我的补充」，同步本库双链，重建索引并记入更新日志。若旧笔记、复审记录与缓存均无可核对的来源，可在该作品的分析映射中提供 `source_url`；仍缺失时停止，不猜视频或图文类型。迁移前的原始文件备份在 cache_dir/migrations/<批次>/before.json。结果返回实际路径和备份位置。检查内容、个人补充、链接和索引一致后报告。

普通 /dy 不自动触发此命令，也不因新分类偏好重写已有文章。若中断后的文件被其他程序修改，恢复程序会保留现场并报告冲突；读取 .dy-transaction.json 和备份来恢复，不删除用户的新内容。

## 用户明确要求深化正文或补配图时

读取现有全文和个人补充，核对相应缓存的完整 transcript.json/原图。保留旧文中用户添加或修改的有用内容，不能只保留固定个人补充区而忽略其他手改段落。开始分析时记录旧笔记与 transcript.json 的 SHA-256；把新版正文和 analysis.json 写入本次独立缓存。分析规范与原图占位符见 knowledge-notes.md。

```sh
<Python> <skill>/scripts/pipeline.py revise <bundle.json> --knowledge-file <knowledge.md> --analysis <analysis.json> --expected-note-sha256 <审阅时笔记指纹> --expected-evidence-sha256 <审阅时转写指纹>
```

正文不要含“我的补充”。脚本在锁内再次校验原笔记指纹，自动接回原个人补充区；主标题和路径不变，分类/标题调整另用 migrate。任一指纹变化时重新读文件并合并修改，不机械换新指纹强行覆盖。旧文章及将修改的索引/日志备份到 cache_dir/revisions/<批次>/before.json；原图与文章一起提交，失败回滚。原图文件使用内容指纹命名，重用相同文件，冲突不覆盖，不自动删除旧附件。

只有用户明确要求修订旧文时执行 revise；普通分享、收藏夹增量更新始终跳过已有作品。

## 用户要求内容复审时

先读取当前文章全文、个人补充和对应原材料，记录文章与 transcript.json 的 SHA-256。按 knowledge-notes.md 实际复核正确性、解析深度、名词解释、难点的通俗解释、模型判断；图文查看全部原图，核对图片完整并位于相应解析附近。重要技术事实到官方材料复核。不能仅凭篇幅、标题、小节数量或脚本成功判断通过。

需要完善正文时先 revise，随后重新读取实际保存的文章并检查，记录这个最终版本的指纹。把独立复审结论写入缓存中的 review.json：

```json
{
  "outcome": "复审待完善",
  "summary": "关键概念已核对，但写入规则还缺少适用边界。",
  "checks": {
    "correctness": {"result": "通过", "details": "填写具体核对的主张、材料位置和结果"},
    "depth": {"result": "待完善", "details": "写入流程解释缺少迟到更新的反例"},
    "terminology": {"result": "通过", "details": "填写首次出现的重点术语及解释位置"},
    "plain_explanation": {"result": "通过", "details": "填写实际检查的通俗例子与机制对应关系"},
    "model_judgment": {"result": "通过", "details": "填写模型判断及其依据是否充分"},
    "images": {"result": "不适用", "details": "本作品是语音视频"}
  },
  "issues": ["补充迟到更新的反例和适用边界"]
}
```

示例用于说明结构，details 必须替换成实际检查结果。图文的 images 不可为“不适用”，并增加实际查看的完整 `reviewed_pages` 数组。结论只有“复审通过”和“复审待完善”；通过要求各适用项通过且 issues 为空，待完善要求明确问题项和 issues。未能取得完整证据时保留未登记/未复审状态，并报告缺口，不冒充完成复审。

```sh
<Python> <skill>/scripts/pipeline.py review <bundle.json> --review-file <review.json> --expected-note-sha256 <最终文章指纹> --expected-evidence-sha256 <已核对材料指纹>
```

该命令核对作品、音频/图文证据和文章指纹后，只登记复审记录与日志，不改正文；每次追加“内容复审”操作，旧元数据备份到 cache_dir/reviews。具体复审依据保存在隐藏记录中，正文不增加技术属性。脚本校验记录完整性和版本一致性，语义正确性仍由执行复审的模型负责。批量复审按文章逐篇完成，不能提前批量标记通过。

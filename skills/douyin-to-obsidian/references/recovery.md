# 作品详情、Cookie 与媒体下载的自动恢复

遇到 Fresh cookies、需要刷新 Cookie、空详情、没有匹配作品或 detail 失败时，先执行本流程，不直接让用户导出 Cookie、运行命令或下载视频。

## 先区分提示与事实

当前 yt-dlp 的 Douyin 提取器在旧详情接口没有返回 aweme_detail 时，会统一提示 Fresh cookies；这不等于已经证明登录失效。可能是游客会话失效、请求签名/接口兼容问题、限流，也可能是作品确实不可访问。缺少匹配材料时仍不入库，不能用标题、页面章节要点或模型常识替代完整音轨/图片。

## 脚本已经自动执行的步骤

正常使用 dy.py 队列，无需增加参数：

1. 优先复用哈希与时长校验通过的完整音轨。否则 browser_cache.py 只读 Chrome 最近收到的官方详情响应，支持实际观察到的 www.douyin.com 和 www-hj.douyin.com；请求与响应的作品 ID 必须一致，缓存结构、CRC、key SHA 和解压完整性必须通过。不会重放请求签名或导出 Cookie。配置 browser_cache_dir 可指定 Chrome 的 Cache_Data 目录；默认使用 macOS Chrome Default。
2. 默认使用 background_detail_first（prefer_browser_media=false）：没有匹配缓存时，先在后台由 douyin_access.recover_detail 用固定资料的 Cookie 调 F2（含一次进程内会话刷新与公开分享页），取得与作品 ID 匹配的同一份详情 JSON 后，照旧用 download_video 下载该作品的播放地址，bundle 记 media_route=background_detail；作者 sec_uid、音轨校验与 Chrome 路线完全相同。后台仍取不到才进入下面的浏览器恢复（打开页面 → 读缓存 → yt-dlp → F2）。显式 HTTP 429 直接停止，不开页面。
3. 仍无匹配详情时，在同一内存 CookieJar 中访问目标官方页面，接收新的 Set-Cookie，再调用一次 F2。这里刷新的是请求进程内的会话；没有执行网页 JavaScript，也不修改浏览器 Cookie 数据库。
4. 仍失败时，用独立匿名会话读取官方移动分享页，只解析其 JSON 中匹配 ID 的完整媒体详情。分享页有时也不提供媒体，这条回退不保证成功。
5. 取得视频详情就下载该作品 video 字段中的播放地址，再提取真实音轨；取得图文详情则下载全部图片。不会把背景音乐、封面、推荐作品作为材料。

HTTP 恢复记录在 .cache/<作品ID>/recovery/<独立编号>.json，包含路由、结果代码、时间和作品 ID，不含 Cookie、完整响应、签名媒体 URL。浏览器成功路径记录在 bundle.json 的 media_route=chrome_received_detail。每次 prepare 最多调用两轮 F2、一次官方页面刷新、一次公开分享页；显式 HTTP 429 停止当前恢复。缓存可复用；视频还需校验解码音轨时长与详情时长一致（容差最大为 3 秒或 2%），不把截断音轨交给模型。

## dy.py 自动打开真实浏览器并重试一次

2026-09-16 起，这一步由 dy.py 的 prepare_item 执行，不依赖模型或 MCP 是否记得操作浏览器，也不再由 agent 另建恢复批次。

1. 首轮返回 DETAIL_UNAVAILABLE 时，先把首次失败和 browser_recovery.attempted=true 写入原批次 state.json，再执行后续动作。进程中断后继续同一批次，也不能重复打开浏览器。
2. 本机 browser_profile 已固定为用户确认的 `<browser_profile>`。macOS 直接调用 Chrome 可执行文件，以独立 argv 传入其父目录 `--user-data-dir`、资料名 `--profile-directory=Default` 和规范作品页；不依赖当前活动窗口选择资料。Cookie 读取（视频、图文、收藏夹）也使用同一完整资料路径，browser_cache_dir 对应 `~/Library/Caches/Google/Chrome/Default/Cache/Cache_Data`。资料目录缺失时明确报错，不自动新建或切换资料。未配置 browser_profile 的旧批次仍使用原 LaunchServices 行为；配置在 init 时快照，新绑定用于新批次。默认等待 8 秒，重新执行 prepare 并先读取新收到的官方详情缓存。2026-09-20 起，首次缓存读取未命中时，再在默认 40 秒的统一截止时间内等待迟到响应；每轮刷新文件索引，扫描与读取共用剩余预算，命中立即继续，不再次打开浏览器、不调用模型。预算耗尽才进入 HTTP 恢复。只打开页面，不点击、不输入、不启用远程调试、不导出 Cookie，也不操作登录或验证码。close_opened_tabs=true 时，打开前用 osascript 记下已在显示该作品的标签页 ID，本条读完（成功或失败）后只关闭新出现的、网址含 douyin.com 与该作品 ID 的标签页；作品 ID 与标签页 ID 以 argv 传入、脚本不会启动 Chrome。首次使用 macOS 会询问是否允许控制 Google Chrome；未授权、超时或 Chrome 未运行时不关闭任何标签页，也不影响本条结果，state.json 的 browser_recovery.opened_tab_closed 记为 unavailable。
3. 第二次仍没有匹配媒体时，返回 RECOVERY_EXHAUSTED 和明确的“抖音内容获取异常”说明，停止本条并继续队列下一条。显式 HTTP 429 直接停止，不触发浏览器恢复。不得另建批次绕过该上限。
4. config.json 可设置 auto_browser_recovery（默认 true）、prefer_browser_media（默认 false；为 true 时恢复旧的“先开页面”顺序）、background_detail_first（默认 true）、close_opened_tabs（默认 true）、browser_recovery_wait_seconds（默认 8，范围 0–20）及 browser_media_timeout_seconds（默认 40，范围 0–40）。state.json 记录本次媒体等待上限。不支持的宿主/浏览器返回具体能力错误。单独 pipeline.py prepare 可读已有浏览器响应，但不主动开浏览器；批量自动恢复入口是 dy.py prepare-all。已取得匹配详情后的下载错误必须保留 download 环节，不再用后续详情接口失败掩盖它。

state.json 保留 first_failure、开始/结束时间、打开的 URL、恢复状态及失败类别。page_state=not_observed 明确表示脚本没有观察播放器或登录状态；打开成功不能被描述为已登录、已播放或已刷新到有效 Cookie。需要进一步排错时，agent 可以通过宿主浏览器工具核对真实页面，但不能据此把失败记为媒体获取成功。

## 需要用户的实际边界

缓存目录较大时，旧文件的逐项 stat 和排序可能耗尽扫描预算。浏览器恢复现在先建立有界的文件名快照，打开作品后优先严格解析新出现的缓存，再回退旧缓存扫描；完整性、CRC、官方域名与作品 ID 校验不变。state.json 的 browser_recovery.cache_probe 记录实际检查数、新条目数、读取错误与扫描是否被截断，不能把扫描预算耗尽当成登录失败。缓存文件 mtime 早于失败时间也不能单独证明当时该响应已完整落盘。

2026-09-22 起，浏览器缓存查找在新文件优先路径后读取 Chrome 自带的 Simple Index，按最近访问顺序定位响应，避免先对十万级目录逐项 stat。索引先校验格式、长度及 CRC，仅作为文件定位线索；响应仍需完整解压、CRC、官方域名和请求/响应作品 ID 一致。索引不可用则回退原查找。扫描未完成或超时返回 CACHE_LOOKUP_INCOMPLETE，不能推断作品无媒体或不兼容。

连续多批 detail 失败本身不构成登录态或作品不兼容的证据。先查 cache_baseline_entries、cache_probe.scan_incomplete/deadline_reached 及 chrome_index_status；查找未完成先排查本地缓存读取。没有实际登录/验证码页面时，只报告已观察的失败及扫描记录；禁止追加“建议重新登录”“过几天再试”的猜测，也不要要求用户先手动打开才能继续诊断。用户反馈新的失败批次后，先检查对应记录与当前匹配材料，不让模型重读历史全文或按标题补写笔记。

- 页面明确要求扫码、短信登录或验证码：请用户只完成该浏览器交互，完成后由 agent 继续失败作品；不让用户把 Cookie、密码或验证码粘贴到聊天里。
- 页面明确显示已删除、私密或无访问权限：记录实际状态并停止，不反复刷新。
- 网络超时、HTTP 429 或页面仅显示“出错了”：如实说明已观察的状态，不把它改写成登录失效；当前请求不无限重试、不创建定时任务。
- 页面可正常播放但两层恢复仍拿不到媒体：说明是提取适配问题，保留诊断并修复可复用脚本。尚未取得完整音轨时不能靠页面摘要入库。

## 验证边界

缓存、接口和浏览器恢复均以实际取得的匹配作品材料为准；打开页面或历史成功不证明当前作品可提取。发布版验证范围见仓库验证报告。

# 更新记录

## 1.1.0 - 2026-10-22

审阅意见落地：文档里写的几条安全线原本大多没有真正生效，这版让它们真的起作用。

安全线

- 媒体库保护真正生效。原来靠 `brief["file_ids"]` 找库文件，可直连 qBittorrent 的结果和宿主
  `dl.tasks` 里都没有这个字段，判断永远拿到空集、也就永远放行。现在监听
  `library.ingest.completed`（自带 `info_hashes` 与 `files`）自己建入库台账：
  - 「还供着别的条目 / 没识别的文件」按台账里各文件的 `media_item_id` 判断；
  - 「订阅还在等这些季集」再问一次 `library.items.relations`；
  - 台账按入库时间留最近 400 条，控制在 `PLUGIN_DATA` 单值 64 KB 以内。
- 去掉缺必填参数 `library_id` 的 `library.items.list` 调用（它也不在申请的权限里）。
  条目详情只用来给通知卡片取标题与海报，取不到就当没有，不参与任何删除判断。
- H&R 改从下载器标签识别（`hit&run` / `H&R` / `考核` 等）。原来读 `dl.tasks` 的
  `subscriptions[]`，那里没有这个字段，恒为 False。
- `removal_blocker` 真的和 `hr_min_hours` 比较了：做够考核时长就不再拦，没做够才拦。
- 做种时长不再把 `added_on` 算进去。已完成的种子只认累计做种秒数与完成时刻，
  `added_on` 只在两者都缺失时兜底——否则「下了三天才下完、刚做种就被判成做了三天」。
- 「正在播放」按片长推进保护窗口，兜底 4 小时（原来固定 30 分钟，两小时的片看到后半段
  就出了窗口）。

默认值与容量

- `dry_run` 默认改为开、`delete_files` 默认改为关、`handle_missing_files` 默认改为关。
  删除不可逆，先演练几轮再放开。
- 每轮删除上限 `MAX_PER_RUN` 由 200 降到 20，避免一次改动在几秒内删光上千个任务。
- `seen` 上限 1000 条、`history` 50 条。原来 20000 / 300 条会超过 64 KB 上限，
  写不进去之后所有种子被当成「刚见到」重新计时。
- 处理记录改到删种与发通知之后再写，不会再出现「删了、没通知、状态没落盘」。

多下载器

- 设置的 qBittorrent 地址不再套在 `dl.list` 的每一台下载器上：按主机名 + 端口比对，
  对不上就退回任务中心的口径。直连拿到的种子带上 `downloader_id`，删除时用同一个 id。

其它

- `should_remind` 的单位修正：`overdue_days` 是天，原来当小时算，兜底时间只有设定值的 1/24。
- 提醒不再要求「只清理媒体库里已有这份文件的种子」也打开；只要到期被挡住就提醒。
- 清单补上 `network = true`（直连 qBittorrent WebAPI），去掉没用到的 `library.scan.start`。
- 不再导入 `movieclaw_sdk.events._base.Frozen`，改用 `BaseModel + ConfigDict(frozen=True)`。
- 补纯函数单测：`removal_blocker`、`library_verdict`、`should_remind`、`has_hr_tag`、
  `open_until`、台账与 URL 归一化。

## 1.0.0 - 2026-10-20

首个版本。

- 给下载器里的种子设「做种多久」：到点自动从下载器删任务，可连数据文件一起删。
- 做种时长以 qBittorrent 自己的记录为准（累计做种秒数、完成时刻、添加时刻取最长），
  不从插件装上那天起算，所以插件中途装上也能正确判断老种子。
- 安全线默认全开：站点 H&R 考核的种子至少保到 72 小时、刷流引擎登记的种子不碰、
  最近 30 分钟有人在网页上打开过的片先留着、MovieClaw 订阅还在等着用的种子先保留。
- 不强制依赖入库：默认「做种满时长就删」，只把 MovieClaw 当下载器用也行；
  想更保守可以打开「只清理媒体库里已有这份文件的种子」。
- 到期但被安全线挡住、没删掉的种子会发一条提醒，交给你处理。
- 按 qBittorrent 的状态分类逐类开关：正在做种、文件已丢失、仍在下载、出错、校验中、其他状态，
  默认只处理做种与文件已丢失两类，正在下载和出错的先不碰。
- 做种时长可从 qBittorrent WebUI 直连读取全部种子（任务中心只列还在跑的任务）。
- 提供「只记录不删除」的演练模式，先看几轮再放开。
- 发出 `seed-scheduler.seed-removed` 与 `seed-scheduler.seed-overdue` 两个事件，各配一条被动通知。
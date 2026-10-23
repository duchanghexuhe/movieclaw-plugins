# 种子做种定时清理（`seed-scheduler`）

给下载器里的种子设「做种多久」：到点自动删任务（可连数据文件一起删）。
不依赖 MovieClaw 入库——只把 MovieClaw 当下载器用也行。

## 怎么判断该删

1. **做种时长以 qBittorrent 自己的记录为准**。已完成的种子只认两个信号：累计做种秒数
   （`seeding_time`）与下载完成时刻（`completion_on`），两者取最长；`added_on` 只在
   这两个都缺失时兜底（未完成的任务按它算宽限期）。把 `added_on` 也算进去会高估时长——
   一个下了三天才下完的种子，刚做完就会被判成「已做种三天」删掉。三个信号都缺失时才用
   「插件第一次看到它」的时间兜底。
   qB 重启或重新校验会把累计秒数归零，所以取最长而不是取最短。
2. **按状态分类**：正在做种 / 文件已丢失 / 仍在下载 / 出错 / 校验中 / 其他状态，
   每一类在设置里单独开关，默认只处理「正在做种」。
3. **安全线**（命中任一条就只提醒、不删）：
   - 站点 H&R 考核：从下载器标签识别（`hit&run`、`H&R`、`考核` 等），做种没到
     `hr_min_hours` 就拦，到了就放行；
   - 刷流（分享率自动提升）引擎登记的种子由它自己汰换，本插件不碰；
   - 最近 4 小时有人在网页上打开过这部片，或正在播放（长片会按片长把窗口往后推）；
   - MovieClaw 订阅刚投递、还在等这个种子里的季集（删了会被打回缺资源重下）；
   - 这个种子还供着媒体库里别的条目或没识别的文件（合集、季包，删了连带毁别的片）；
   - 带「免清理 / noreap / seed-keep」标签的种子，永不处理。
4. **媒体库那层关系靠插件自己的入库台账**。宿主 `library.ingest.completed` 事件自带
   `info_hashes` 与 `files`，插件在那一刻记下「哪个种子入库了哪些文件、属于哪个条目」；
   `dl.tasks` 不返回 `file_ids`，宿主也没有「按 infohash 查库文件」的操作，所以只能这么记。
   台账按入库时间留最近 400 条，控制在插件数据的单值 64 KB 以内。
5. 到期但被安全线挡住的种子会发一条提醒。打开「只清理媒体库里已有这份文件的种子」后，
   没入库的一律不删，同样只提醒。

## 设置

| 字段 | 标题 | 默认 |
|---|---|---|
| `seed_hours` | 单种做种时长（小时） | 72 |
| `overdue_days` | 没入库的种子再等几天后提醒（天） | 3 |
| `dry_run` | 只记录不删除（演练） | **开** |
| `delete_files` | 删除时连数据文件一起删 | **关** |
| `library_required` | 只清理媒体库里已有这份文件的种子 | 关 |
| `keep_waiting_subscriptions` | 订阅还等着用的种子先保留 | 开 |
| `protect_unwatched` | 库里的还没看完就先保留 | 关 |
| `protect_recent_open` | 最近打开过的先保留 | 开 |
| `respect_hr` | 带 H&R 考核的种子多保一段 | 开 |
| `hr_min_hours` | H&R 种子最短做种时长（小时） | 72 |
| `respect_boost` | 不碰刷流引擎的种子 | 开 |
| `handle_seeding` | 处理·正在做种 | 开 |
| `handle_missing_files` | 处理·文件已丢失 | **关** |
| `handle_downloading` | 处理·仍在下载 | 关 |
| `handle_errored` | 处理·出错 | 关 |
| `handle_checking` | 处理·校验中 | 关 |
| `handle_unknown` | 处理·其他未知状态 | 关 |
| `qb_url` | qBittorrent WebUI 地址 | 空 |
| `qb_username` | qBittorrent 用户名 | 空 |
| `qb_password` | qBittorrent 密码（加密保存） | 空 |

`qb_url` 留空时只用任务中心的口径，只看得见还在跑的任务；填上 WebUI 地址才能扫到下载器里的全部种子。

有多台下载器时，只有登记的地址和某一台对得上（主机名 + 端口）才直连那一台；对不上的按任务中心
的口径读，不会把这一台的种子算到另一台头上。

## 命令

| 命令 | 作用 |
|---|---|
| `mclaw plugins seed-scheduler status` | 设置、累计统计与最近的处理记录 |
| `mclaw plugins seed-scheduler preview` | 只读预演：哪些种子会到期、各自为什么删 / 为什么留 |
| `mclaw plugins seed-scheduler queue` | 每个种子还要做种多久、什么时候清理（按时间排好） |
| `mclaw plugins seed-scheduler scan` | 立刻扫描一次（`dry_run` 不传时按设置里的演练开关） |

## 定时与后台

- 定时任务「做种定时清理扫描」，周期在「设置 → 定时任务」里改。
- 后台任务 `seed-scheduler:sweep` 做实际删除，进度在「活动 → 任务」里看。
- 常驻监听四个事件：下载完成、入库完成、条目详情被打开、开始播放。

## 事件与通知

- `seed-scheduler.seed-removed`：种子已到期删除，可带「已做种 N 小时、是否连文件一起删」。
- `seed-scheduler.seed-overdue`：到期但被安全线挡着没删，交给你处理。

两条都配了被动通知，发给管理员。

## 权限与契约

- 宿主操作：`dl.list`、`dl.tasks`、`dl.file-sources.list`、`library.list`、`library.items.get`、
  `library.items.relations`、`dl.torrent.delete`
- 网络：`network = true`（直连 qBittorrent WebAPI 读种子状态与标签）
- 契约：`host-ops`、`plugin-data`、`plugin-events`、`job-handlers`、`plugin-jobs`、`scheduled-tasks`、
  `download.completed`、`library.ingest.completed`、`library.item.viewed`、`playback.started`
- 敏感事件：`playback.*`、`library.item.viewed`
- `dl.torrent.delete` 是破坏性操作：默认 `dry_run` 开着，扫描只把「本来会删什么」写进记录、
  不会真的调用它；把演练关掉之后才会删，而 `delete_files` 默认关，所以默认只移除任务、磁盘文件留着。

## 说明

站点 H&R 的具体考核时长 MovieClaw 拿不到，所以 `hr_min_hours` 只是一个兜底值（常见站点是 72 小时
加分享率 1.0 以上），实际以站点要求为准。拿不准就先开「只记录不删除」跑几轮。
# 04 验收

> 完成标准（AGENTS.md）：单测过 ≠ 完成。真实服务器安装 + 真实用户场景 + 诊断页无 error +
> 卸载重装，才算完成。以下分宿主侧、插件侧两段。

## 验证状态（2026-10-18 实现完成；同日复查后修订）

**已自动验证：**

- [x] 宿主单测 26 个（`tests/api/test_site_proxy.py`）全绿；
- [x] sites/tracker 回归 115 通过（10 个预存在的 Windows 进程外用例失败与本次无关，见下）；
- [x] 插件单测 57 个全绿（判定正反例、状态机、适配器分派、mock 宿主操作的整轮：
      失效告警去重、告警发送失败重试、连续失败只报一次、跳过语义、签到开关、需人工短路）；
- [x] `check_plugin.py` 通过（含 `site.list` / `site.catalog` / `site.proxy.request` 操作核对）；
- [x] 打包 26 KB，零 vendor / 零依赖；
- [x] uidev 真实服务器（inline 本地插件路径）装载成功：授权操作、定时任务贡献、
      启动告警自动消退；
- [x] 真实保活轮端到端：假 mteam 凭据（ACTIVE）→ 适配器生成 `updateLastBrowse` 签名请求 →
      宿主代请求打到**真实 M-Team API** → 401「Full authentication is required」→
      插件判凭据失效 → 状态机落库（`alerted_expired=true`、失败计数直抵阈值）→
      第二轮去重（不再重复告警）。
- [x] 无站点时空轮不炸、正确写空状态。

**复查后修订（2026-10-18，本轮）：**

- 日报模板引用的 `{detail}` 此前载荷里没有 → 日报正文为空；现在 `DailyReport.detail`
  在插件里拼好，模板可用。
- 连续失败此前只进「默认关闭」的日报 → 用户永远看不到；现在拆出
  `pt-keepalive.report.failures`（默认开），每段失败只报一次。
- 未适配的纯 API 站（sunnypt）此前套 NexusPHP 模板 → 每轮假失败；现在按
  `auth_type=apikey` 判跳过，不发请求。
- NexusPHP 此前只访问 `attendance.php`（没有该页的站 = 404 假失败）→ 现在先 `index.php`
  确认登录态，签到页 404 记「登录在线」。
- Cloudflare 特征此前扫整页 HTML（站点 JS 里的字符串会误判）；现在只看 title 与可见文本。
- 宿主 409「未配置 / 已停用 / 尚未验证通过」此前一律当凭据失效 → 现在记跳过，不告警。
- 失效告警此前先置位再发送 → 发送失败就永久丢失；现在先发后置位，失败下一轮重试。
- 移除了用不到的 `network = true` 声明（插件无直连出网）。

**已知平台限制（非本任务缺陷）：**

- Windows 上进程外插件 IPC 依赖 `AF_UNIX`（宿主整个 process-runtime 传输层），
  本机只能走 inline 路径验证；Linux（NAS/CI）不受影响。独立宿主工程，已报。
- 事件不落 `domain_event` 表是**设计行为**（`publish_now` 零开销：没有订阅者不落库），
  通知在真实安装（用户批准后建立偏好）时才会真正推送。

**留待用户（需要真实凭据 / 登录态）：**

1. 在真实环境 `mclaw plugin dev plugins/pt-keepalive --once` 或上传正式包走批准安装；
2. 配置一个真实 NexusPHP 站点 + mteam API Key，观察一轮真实签到/触碰与日报通知；
3. M-Team `x-api-key` 是否可调 `updateLastBrowse` 的最终确认（当前以 401 失效链路验证）。

## 宿主侧（SITE_PROXY）验收

- [x] `site.proxy.request` 进入 OpenAPI 与宿主操作目录（`_operation_index`）；
- [x] 单测：站内相对路径校验（拒绝 `//`、协议、反斜杠、CRLF）；认证头黑名单
      （`cookie`/`authorization`/`x-api-key` 被剥除）；响应剥 `set-cookie`；512KB 截断；
      凭据非 ACTIVE → 409；网络错误 → 503；
- [x] 对已配置站点的真实请求（假凭据 401 响应透传）；
- [x] 既有回归：torrent_sync、site.verify、站点凭据 CRUD 行为不变。

## 插件侧验收（端到端）

前置：主程序 dev 服务器（`G:\Github\movieclaw\.tmp-uidev\start-uidev.ps1`，API 8001）；
至少 1 个真实 NexusPHP 站点已配置且 ACTIVE；mteam 已配置（x-api-key）且 ACTIVE。

1. **本地检查**：`check_plugin.py plugins/pt-keepalive` 无 ✗；单测全绿
   （三态判定正反例：成功词/幂等词/否定词优先/密码框/CF 特征/剥 script 后匹配）。
2. **开发安装**：`mclaw plugin dev plugins/pt-keepalive --once` → `已加载`；
   `app plugins list` 里 `pt-keepalive` state=active；`设置→定时任务` 出现
   `pt-keepalive:run`（默认每天 09:17）。
3. **真实保活轮**（等调度或临时把 cron 改近）：
   - NexusPHP 站：日志出现该站签到请求与三态结果；宿主日志可见代请求记录；
   - mteam：`updateLastBrowse` 成功（兼容 `code:1/FAIL` 判据）；站内「最后浏览时间」刷新
     （用户肉眼核对，或下一轮 profile 里 lastBrowse 变化）；
   - 站点配置了代理（site:<id>）时请求走代理（宿主出口层日志核对）。
4. **通知链路**：`pt-keepalive.report.daily` 事件触发 → App/IM 收到日报（标「来自 PT 站点保活」）；
   偏好关闭日报 → 不再收；`auth.expired` 与 `report.failures` 独立于日报开关。
5. **失效告警流**：临时改坏某站凭据（或用已失效站点）→ 下一轮判定 auth_expired →
   收到一次告警（不是每轮一条）；修复凭据 + `POST /verify/{site_id}` → 下轮恢复正常，
   失败计数与告警位清零。
6. **自检降级**：在**旧宿主**（无 site.proxy.request）上装插件 → 加载成功但不注册任务，
   日志说明「宿主不支持，需升级宿主」，诊断页无 error。
7. **状态查询**：`mclaw plugins pt-keepalive status` 返回各站最近结果表。
8. **诊断页**：`设置→插件→pt-keepalive` 无 error；贡献的定时任务显示在详情页。
9. **卸载重装**：`app plugins packages uninstall pt-keepalive --yes` → 条目消失、
   定时任务撤下；重装 → 任务回来、PLUGIN_DATA 里状态延续（默认不 purge）。

## 关键故障场景（预期行为）

| 场景 | 预期 |
|---|---|
| NexusPHP 站改版，签到页选择器全变 | 判 LOGIN_ONLY（保守）；连续 3 轮进「连续失败」通知；不虚报成功 |
| NexusPHP 站没有签到页（attendance.php 404） | 首页确认登录在线即记 LOGIN_ONLY，**不算失败** |
| mteam 站点轮换 `_sgin` 密钥 | 触碰判 FAILED（签名拒），3 轮后连续失败告警；升级插件（改常量）恢复 |
| mteam 返回 `{"code":"1","message":"FAIL","data":null}` | **判成功**（2026-10 反直觉判据），日志注明 |
| 某站 Cloudflare challenge / WAF 拦截（401/403） | need_human，不计数、不熔断、不发失效告警；日报提示「需人工」 |
| 未适配的纯 API 站（sunnypt） | 不发请求，记 skipped，不进失败统计 |
| 站点凭据被用户禁用 / 尚未验证通过 | site.list 不返回 usable，或宿主 409「已停用 / 尚未验证通过」→ 记 skipped，不告警 |
| 代请求遇到网络错误 / 5xx | 单站本轮 FAILED，不重试；连续 3 轮进「连续失败」通知（每段失败只报一次） |
| 代理配置只给 site:mteam | mteam 请求走代理，其余站直连（宿主 egress 标签粒度） |
| 调度器到点但上一轮还没跑完 | APScheduler max_instances=1 + coalesce（宿主既有行为），不并发 |
| 同一版本重复上传 | 服务器拒绝（同版本字符串）；走正常升 version 流程 |

## 风险告知（面向最终用户的文案要点，随插件 description/README）

- 程序化签到/访问未必被站方计为「用户活跃」（MoviePilot 官方警告，馒头是已知例子）；
  插件显示「成功」只代表请求与判据成立，**不承诺免于封号**。
- 签到默认开启，但「仅保活不签到」是一键开关（对站点规则敏感的用户）。
- 频率默认每天一次，刻意不做高频（mtlogin 的每 2 小时不必要——宿主管登录态续期）。

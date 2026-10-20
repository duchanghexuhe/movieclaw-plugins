# PT 站点保活（pt-keepalive）

对宿主里已配置且验证通过的 PT 站点，每天跑一轮保活：访问首页确认登录态、尝试签到
（可关）、M-Team 刷新「最后活跃时间」。两类站点走的路不同：

| 站点类型 | 动作 | 凭据 |
|---|---|---|
| NexusPHP（hdsky、chdbits 等） | GET `index.php` 确认登录态 → GET `attendance.php` 尝试签到 | 用宿主已配置的会话代发，插件接触不到 |
| M-Team（`mteam`） | 账号密码 + TOTP 登录 → POST `/api/member/profile` → POST `/api/member/updateLastBrowse` | 插件设置里填（宿主 SecretBox 加密） |
| 其余纯 API 站（如 sunnypt） | 不访问 | — |

## 为什么 M-Team 要自己登录

宿主的 API Key 调不动它的保活接口（2026-10-20 实测）：同一个 Key 调
`/api/member/profile` 正常返回 `code:"0"`，调 `/api/member/updateLastBrowse` 却一律回
`{"code":401,"message":"Full authentication is required to access this resource"}`
——那个接口只认网页会话 JWT。所以这一站按社区方案
[scjtqs2/mtlogin](https://github.com/scjtqs2/mtlogin) 的做法，在插件里用
**账号密码 + TOTP 动态码**登录拿 JWT，再刷新最后浏览时间；登录态（JWT + 设备指纹）
存在插件数据里加密保存，下一轮复用。

- 账号开了两步验证时，TOTP 密钥填绑定页面那串 Base32（整条 `otpauth://` 链接也认）。
- 没开两步验证就把 TOTP 密钥留空。
- 账号密码留空 = 不做这一站（本轮记「跳过」，不报错）。
- **登录失败不重试**：站点会扣账号的尝试次数（「您還有 N 次機會」）。

## 它做什么

- **凭据不出宿主（NexusPHP 系）**：这些站点的请求经宿主操作 `site.proxy.request` 用宿主
  自己的已认证会话代发，插件接触不到 Cookie / API Key。
- **不虚报**：签到成功、仅登录在线、失败是三种不同结果，页面证据不明时保守判「仅在线」。
  人机验证 / WAF 拦截记「需人工」，不算失败也不发失效告警。
- **M-Team「无需刷新」不算失败**：站点认为间隔过短、最后浏览时间已经新鲜时回
  `{"code":"1","message":"FAIL"}`——此时它**没有**刷新，但也不是账号失效。记「登录在线」，
  不进失败统计（本机还跑着 mtlogin 容器时，每轮都会撞上）。
- **签名错误的处置**：签名密钥来自站点前端 JS，站点发版轮换后请求会被回「簽名錯誤」。
  这时记失败并提示「插件需要更新」，不当成用户凭据失效去打搅。
- **不自动重登 NexusPHP**：PT 站普遍禁止脚本登录，失效只通知用户去「设置 → 站点」更新凭据。

## 设置

插件详情页：

- `enabled` — 总开关。关闭后定时任务照常触发，但跑空轮、不访问任何站点。
- `signin_enabled` — 尝试每日签到。关闭后只确认登录态，不请求签到页。
- `daily_report` — 每天发保活日报。凭据失效告警不受它控制，始终发。
- `mteam_username` / `mteam_password` / `mteam_totp_secret` — M-Team 登录信息（密码与
  TOTP 密钥加密保存、界面不回显；留空即不做这一站）。

周期不在设置里：「设置 → 定时任务」里的 `PT 站点保活轮`（默认每天 09:17）可以改。

## 通知

| 事件 | 默认 | 内容 |
|---|---|---|
| `pt-keepalive.auth.expired` | 开（active） | 「{站点} 登录已失效」；同一失效期只发一次，恢复后清零 |
| `pt-keepalive.report.daily` | 关（passive） | 在线 / 签到成功 / 失败 / 需人工 / 跳过各多少站；连续 3 轮失败的站点带明细 |
| `pt-keepalive.report.failures` | 开（active） | 有站点连续 3 轮失败（每段失败只报一次） |

## 状态查询

```bash
mclaw plugins pt-keepalive status
```

返回每站最近一轮的结果、说明、连续失败数、上次成功时间。

## 前置条件

宿主必须提供 `site.list`、`site.catalog`、`site.proxy.request` 三个宿主操作
（`site.proxy.request` 随主程序的新版本提供）。旧宿主上插件照常加载但不注册定时任务，
日志会说明需要升级主程序。

M-Team 的请求直连 `api.m-team.io`，与宿主会话无关；插件声明了 `network = true`。
需要走代理时，在「设置 → 网络」给 `pt-keepalive` 配代理即可（与其它插件同一套出口）。

## 已知限制

- **M-Team 依赖站点前端的签名算法与版本号**：签名密钥（`HLkPcWmycL57mfJt`）、
  `version=1.1.4` / `webversion=1140` 都可能随站点发版变化；变了会以「签名错误」或异常
  响应暴露出来，改 `pt_keepalive/mteam.py` 顶部的常量即可。
- **站点改走强制 Turnstile 人机验证后**账号密码模式会失效（mtlogin 也一样），届时只能手工
  透传 JWT，插件暂不支持。
- 适配分派按 `auth_type` 大小写不敏感比对（宿主 `site.list` 回的是枚举值 `APIKEY`）。

## 风险

程序化签到 / 访问未必被站方计为「用户活跃」，插件显示成功只代表请求与判据成立，
**不承诺免于封号**。M-Team 的账号密码登录属于脚本登录，站点规则敏感的用户可以只在
宿主里保留 API Key、不填插件里的 M-Team 账号（其余站点照常保活）。

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

单测全部用假响应，不连真实站点，也不启动宿主。

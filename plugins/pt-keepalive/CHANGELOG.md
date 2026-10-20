# 变更记录

## 0.2.0 (2026-10-20)

M-Team 改为「账号密码 + TOTP 登录」保活（与调研选定的 mtlogin 路线一致），
不再拿宿主的 API Key 去调它的保活接口。

- **修正**：上一版用宿主 `site.proxy.request` 带 API Key 调 `/api/member/updateLastBrowse`，
  真实站点一律回 `{"code":401,"Full authentication is required..."}`——这个接口只认网页会话
  JWT，API Key 只能读（`/api/member/profile` 正常）。原实现会把这一站每轮判成「凭据失效」。
- 新增 `pt_keepalive/mteam.py`：按站点前端的行为发 `POST /api/login`（账号 + 密码 + 空
  turnstile），`code 1001` 时补 TOTP 动态码重发，取响应头的 `Authorization`（JWT）与 `Did`；
  随后 `profile` 验会话、`updateLastBrowse` 刷新。签名 `_sgin` 用 HMAC-SHA1，TOTP 用标准库
  自算（不引入 pyotp）。
- 插件设置新增三个 M-Team 字段（账号 / 密码 / 两步验证密钥），密码与密钥是 `SecretStr`，
  宿主 SecretBox 加密落盘、界面不回显；留空即不做这一站。
- 登录态（JWT + 设备指纹）存插件数据（`secret=True`）跨轮复用：少走登录（登录失败会扣账号
  尝试次数），并保住服务端认可的 `Did`。
- **判定修正**：`updateLastBrowse` 的 `{"code":"1","message":"FAIL"}` 是「站点认为无需刷新」
  （间隔过短 / 已被别的工具刷新），记「登录在线」而不是「成功」——它确实没刷新；
  也不算失败。签名错误（「簽名錯誤」）与限流（「請求過於頻繁」）不置 `auth_expired`。
- 清单声明 `network = true`：M-Team 的请求由插件直连 `api.m-team.io`（其余站点仍经宿主代
  请求）；插件出口跟随用户在「设置 → 网络」给 `pt-keepalive` 配的代理。
- 单测 80 个全绿（假响应，不连站点）：签名算法、RFC 6238 的 TOTP 向量、各失败形态的分类、
  登录→刷新流程、会话复用、无人值守时的「无需刷新」判定、网络错误与签名轮换的告警分级。

## 0.1.1 (2026-10-20)

装到 MovieClaw v0.39.0（NAS/Docker）前与安装时的修正：

- 入口模块不再从 `movieclaw_api.plugins.keys` 导入 `PLUGIN_DATA` / `PLUGIN_ROUTES`：
  插件包在独立进程里只有 `movieclaw_sdk` 在导入路径上，原来那样写会让插件起不来。
  改为在插件侧按同名同版本造 `ServiceKey`（服务按名字 + 版本认）。
- 站点适配分派按大小写不敏感比对 `auth_type`：宿主 `site.list` 回的是枚举值
  `APIKEY`，原来只认小写 `apikey`，会把 API 站误判成 NexusPHP 站。

## 0.1.0 (2026-10-18)

首个版本。

- 每日定时对已配置且验证通过的 PT 站点跑一轮保活：NexusPHP 系先访问 `index.php`
  确认登录态、再访问签到页 `attendance.php` 尝试签到（可整体关闭签到，只做登录确认），
  没有签到页的站点（404）不算失败。
- 四态结果判定（签到成功 / 仅登录在线 / 失败 / 需人工），外加「跳过」表示本轮未访问：
  判据来自社区项目（MoviePilot autosignin、flexget_qbittorrent_mod）的成熟经验——
  先剥脚本与隐藏元素再匹配证据、登录失效证据优先、人机验证与 WAF 拦截判「需人工」、
  未知响应保守判「仅在线」。
- 未适配的纯 API 站点（如 sunnypt）本轮跳过、不发请求，也不进失败统计。
- 凭据失效发通知（同一失效期只告警一次，告警发送失败下一轮自动重试），
  恢复后自动清零；连续 3 轮失败的站点进日报明细。日报通知默认关闭。
- NexusPHP 站点的请求经宿主的 `site.proxy.request` 操作代发：复用宿主登录态、
  每站限流与代理出口，插件全程接触不到凭据。
- `mclaw plugins pt-keepalive status` 查看各站最近一轮结果。

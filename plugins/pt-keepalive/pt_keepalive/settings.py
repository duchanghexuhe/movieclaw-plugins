"""设置模型（设置页表单）。刻意保持最少：不提供站点排除与间隔配置——
启用范围 = 宿主已配置且验证通过的站点全集，周期在「设置→定时任务」改。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Settings(BaseModel):
    enabled: bool = Field(
        default=True,
        title="启用保活",
        description="关闭后定时任务跑空轮，不再访问任何站点",
    )
    signin_enabled: bool = Field(
        default=True,
        title="尝试每日签到",
        description="NexusPHP 系站点访问签到页并按页面提示判定；关闭后只确认登录态，不产生签到动作",
    )
    daily_report: bool = Field(
        default=False,
        title="每天发保活日报",
        description="每轮跑完发一条汇总通知；凭据失效告警不受此开关控制（始终发）",
    )

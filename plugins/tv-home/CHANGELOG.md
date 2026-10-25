# 电视桌面

## 0.1.0

- 首个版本（P1 自用闭环）。连接电视、摸底、安装 MovieClaw 与桌面壳「MovieClaw 桌面」、换默认桌面（需要时停用厂商桌面）、验证、还原、关闭调试，全部由 agent 经 `mclaw plugins tv-home …` 引导完成。
- 换桌面与还原的顺序、防护规则沿用 cleanyourtv 踩出来的做法（MIT）：不 root、只动 /data、先写状态记录再改设备、第一次的记录不被覆盖。

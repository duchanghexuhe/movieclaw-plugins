# MovieClaw 桌面（aerial fork）

基于 [pengchujin/aerial-tv](https://github.com/pengchujin/aerial-tv) **v1.48**（MIT，`LICENSE` 与 `FONT-LICENSE-OFL.txt` 原样保留）。
设计稿 `docs/design/androidtv-home.md` §13。与上游的差异（薄补丁，pin 上游版本、定期评估合并）：

1. 品牌资源：应用名「MovieClaw 桌面」、启动图标（深底白播放三角）、本 README
2. `LayoutStore.arrange`：首启预置 MovieClaw 为第一格（未安装时无害）
3. `TileRecipes`：`io.movieclaw.androidtv` 品牌磁贴（紫→青渐变 + 白 logo）
4. `MainActivity`：MC 磁贴点击走 `movieclaw://home`（回首页根部，§7.1 的深链）
5. `MainActivity`：停用 `Updates.init`——更新走「电视桌面」插件，不跟上游 Release
6. `app/build.gradle.kts`：构建产物进隐藏的 `.build/`（插件打包跳过点开头目录）

上游未改动的设计（guard、天气、壁纸、屏保）全部保留。

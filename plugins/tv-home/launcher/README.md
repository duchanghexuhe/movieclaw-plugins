# MovieClaw 桌面（桌面壳）

「电视桌面」插件装到电视上的默认桌面：开机、按主页键打开 MovieClaw，从 MovieClaw 退出是「全部应用」。
设计见主仓库 `docs/design/androidtv-home.md` §5、§6；包名 `io.movieclaw.androidtv.home`。

APK 由这个独立的 Gradle 工程编出来，打进插件包，插件在电视上安装它；源码本身不需要进插件包。

```bash
cd plugins/tv-home/launcher
./gradlew assembleRelease    # 产物在 .build/outputs/apk/release/
```

- 编译输出放在隐藏目录 `.build/`：插件打包跳过以点开头的目录，普通的 `build/` 会被整个打进 `.mcplugin`。
- 正式签名用环境变量 `ANDROID_KEYSTORE_PATH`、`ANDROID_KEYSTORE_PASSWORD`、`ANDROID_KEY_ALIAS`、`ANDROID_KEY_PASSWORD`，
  没给时出未签名的包。电视上覆盖安装要求签名不变，所以密钥要固定；换了密钥就得先在电视上卸掉旧的再装。
- 内容变了就递增 `version.properties` 的 `versionCode`。

import java.util.Properties

// 「MovieClaw 桌面」（主仓库 docs/design/androidtv-home.md §6）：只当默认桌面的小 APK，随「电视桌面」插件发版、打进插件包。
// 不用 Compose、不申请网络权限、没有后台服务、不含原生库
plugins {
    id("com.android.application") version "9.4.1"
    // 钉住 Kotlin 编译器版本（AGP 9 自带 Kotlin 支持，不再单独应用 Kotlin 插件），与主仓库 Android TV 工程一致
    id("org.jetbrains.kotlin.jvm") version "2.4.20" apply false
}

// 编译输出放进隐藏目录：插件打包时跳过以点开头的目录，build/ 会被整个打进 .mcplugin
layout.buildDirectory.set(file(".build"))

val versions = Properties().apply { file("version.properties").inputStream().use(::load) }
require(versions.getProperty("versionName").matches(Regex("\\d+\\.\\d+\\.\\d+"))) { "MovieClaw 桌面 versionName 必须为 X.Y.Z" }

android {
    namespace = "io.movieclaw.androidtv.home"
    compileSdk = 37
    defaultConfig {
        applicationId = "io.movieclaw.androidtv.home"
        minSdk = 23
        targetSdk = 37
        versionName = versions.getProperty("versionName")
        versionCode = versions.getProperty("versionCode").toInt()
    }
    // 正式签名用固定的一把密钥（ANDROID_KEYSTORE_* 环境变量）：电视上 `pm install -r` 升级要求签名不变；没给密钥时出未签名的 release 包
    val keystore = providers.environmentVariable("ANDROID_KEYSTORE_PATH").orNull
    signingConfigs {
        create("release") {
            if (keystore != null) {
                storeFile = file(keystore)
                storePassword = providers.environmentVariable("ANDROID_KEYSTORE_PASSWORD").get()
                keyAlias = providers.environmentVariable("ANDROID_KEY_ALIAS").get()
                keyPassword = providers.environmentVariable("ANDROID_KEY_PASSWORD").get()
            }
        }
    }
    buildTypes {
        release {
            if (keystore != null) signingConfig = signingConfigs.getByName("release")
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("androidx.recyclerview:recyclerview:1.4.0")
}

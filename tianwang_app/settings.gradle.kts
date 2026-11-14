// 天网 · 手机端（Compose，唯一在维护的 Android 工程）
//
// 历史：第一代 tianwang_android/ 是 aapt2+javac 手搓五步、零第三方依赖、
//   产物一百多 KB，已于 2026-11-14 删除；本工程要的是 Compose + Material3
//   （照 Operit 抄），那是几百 MB 的依赖树，只能走 Gradle。
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "tianwang-next"
include(":app")

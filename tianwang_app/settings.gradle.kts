// 天网 · 手机端第二代（Compose）
//
// 为什么要另起一个 Gradle 工程，而不是改老的那份：
//   老的 tianwang_android/ 是 aapt2+javac 手搓五步、零第三方依赖、产物一百多 KB，
//   它跑得好好的，并且 CI 和打包线都挂在那份 build.sh 上。这里的目标是**换界面
//   体系**（Compose + Material3，照 Operit 抄），那是几百 MB 的依赖树，跟「手搓
//   五步」是两条路，硬塞进同一份构建脚本只会把两条都弄坏。
//   ⇒ 两份并存：老的是能出货的，新的是在追的；追上了再谈替换。
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

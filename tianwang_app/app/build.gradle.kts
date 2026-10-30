import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// 版本号唯一真源 = version.properties（`bash release.sh` 会自动 +1 再构建安装）。
// 不写死在 gradle 里，是为了「一条命令发一版」不用改代码。
val verProps = Properties().apply {
    val f = rootProject.file("version.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "app.skynet.tianwang.next"
    compileSdk = 35

    defaultConfig {
        applicationId = "app.skynet.tianwang.next"
        // 跟老工程一致：minSdk 28 不是随便挑的（libfleet_sd.so 用到 std::aligned_alloc，
        // Android libc 到 API 28 才有）。将来合并 native 时这里不能降。
        minSdk = 28
        targetSdk = 35
        versionCode = verProps.getProperty("VERSION_CODE", "1").trim().toInt()
        versionName = verProps.getProperty("VERSION_NAME", "0.1-proto").trim()
    }

    buildFeatures {
        compose = true
    }

    // 沿用老工程那份密钥（tianwang_android/keystore.jks）。
    // 换一份新密钥会导致「覆盖安装」失败——手机只认同一个签名的更新。
    signingConfigs {
        create("rel") {
            storeFile = file("../../tianwang_android/keystore.jks")
            storePassword = "tianwang"
            keyAlias = "tianwang"
            keyPassword = "tianwang"
        }
        // 迭代用的 debug 密钥也收进项目（原来靠 ~/.android/debug.keystore，
        // 换机器/清目录就换签名 ⇒ 手机再也覆盖不上，只能卸载重装、连带删掉模型）。
        // 这份就是本机那份的副本，证书 SHA-256 与手机上已装的包一致。
        create("dbg") {
            storeFile = file("../keystore-debug.jks")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        debug {
            // 固定签名 ⇒ `adb install -r` 可长期迭代，升级不清数据、不丢模型。
            signingConfig = signingConfigs.getByName("dbg")
        }
        release {
            signingConfig = signingConfigs.getByName("rel")
            // 🔴 必须开：material-icons-extended 里有几千个图标，
            // debug 包 55 MB 主要就是它。R8 只留真正引用到的那十几个。
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        resources.excludes += setOf("/META-INF/{AL2.0,LGPL2.1}")
    }
}

dependencies {
    // Compose 用 BOM 统一版本，免得各库版本互相打架（这是官方推荐做法，
    // 也是踩过「ui 1.6 + material3 1.0」组合崩在运行期之后改的）
    implementation(platform("androidx.compose:compose-bom:2024.10.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.navigation:navigation-compose:2.8.4")

    debugImplementation("androidx.compose.ui:ui-tooling")

    // 手机端原来一条测试都没有。这里先只上 JVM 单测：不需要真机、
    // 不占 USB、几秒钟跑完，覆盖的是「拼地址 / 解 SSE / 解 JSON」这类
    // 出了错却只能靠人眼在手机上发现的地方。
    // org.json 是必要依赖：android.jar 里的 JSONObject 在单测里是空壳，
    // 不带这个 jar 一 new 就抛 "Stub!"。
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}

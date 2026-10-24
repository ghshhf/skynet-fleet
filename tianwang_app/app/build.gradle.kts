plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
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
        versionCode = 1
        versionName = "0.1-proto"
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
    }

    buildTypes {
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
}

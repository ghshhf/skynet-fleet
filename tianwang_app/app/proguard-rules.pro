# Compose + Activity 的 R8 规则
#
# 这份文件存在的原因：开了 minify 之后，反射/序列化那条路最容易断。
# 现在还没有反射代码，但下面几条是 Compose 项目的通用底线——
# 少了它们，报错会出现在运行期（点某个按钮才崩），而不是构建期。

# 保留 Compose 的运行时注解与 ViewModel 相关签名
-keepattributes *Annotation*, InnerClasses, Signature
-keepclassmembers class **.R$* { public static <fields>; }

# Activity 由系统反射实例化，名字不能动
-keep public class * extends android.app.Activity
-keep public class * extends androidx.activity.ComponentActivity

# Kotlin metadata 保留（协程/反射依赖）
-keep class kotlin.Metadata { *; }
-dontwarn kotlin.**

# 原型阶段先把行号留着，崩溃日志才读得懂（代价是包大一点点）
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile

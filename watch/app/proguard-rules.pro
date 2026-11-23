# Keep the JSON models the backend's shape is read into (kotlinx.serialization).
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.AnnotationsKt
-keepclassmembers class kotlinx.serialization.json.** { *** Companion; }
-keepclasseswithmembers class kotlinx.serialization.json.** { kotlinx.serialization.KSerializer serializer(...); }
-keep,includedescriptorclasses class care.ira.watch.**$$serializer { *; }
-keepclassmembers class care.ira.watch.** { *** Companion; }
-keepclasseswithmembers class care.ira.watch.** { kotlinx.serialization.KSerializer serializer(...); }
# OkHttp and Health Services use reflection on platform classes only.
-dontwarn okhttp3.**
-dontwarn org.conscrypt.**

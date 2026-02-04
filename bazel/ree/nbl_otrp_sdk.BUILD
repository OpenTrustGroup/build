filegroup(
    name = "all",
    srcs = glob(
        ["**"],
        exclude = [
            ".git/**/*",
            ".gradle/**/*",
            "app/build/**/*",
            "app/.cxx/**/*",
            "app/.externalNativeBuild/**/*",
            "app/src/main/jni/jni_libs/**/*",
        ],
    ),
    visibility = ["//visibility:public"],
)

filegroup(
    name = "ROOT",
    srcs = ["build.gradle"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "jni_libs",
    srcs = ["app/src/main/jni/jni_libs"],
    visibility = ["//visibility:public"],
)

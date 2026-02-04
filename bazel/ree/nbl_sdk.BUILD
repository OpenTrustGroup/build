filegroup(
    name = "all",
    srcs = glob(
        ["**"],
        exclude = [
            ".git/**/*",
            ".gradle/**/*",
            "nbl_sdk/build/**/*",
            "nbl_sdk/.cxx/**/*",
            "nbl_sdk/.externalNativeBuild/**/*",
        ],
    ),
    visibility = ["//visibility:public"],
)

filegroup(
    name = "ROOT",
    srcs = ["build.gradle"],
    visibility = ["//visibility:public"],
)

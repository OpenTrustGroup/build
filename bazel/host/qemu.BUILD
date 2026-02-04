filegroup(
    name = "all",
    srcs = glob(
        ["**"],
        exclude = [
            ".*/**",
            "**/.git/**/*",
        ],
    ),
    visibility = ["//visibility:public"],
)

filegroup(
    name = "ROOT",
    srcs = ["configure"],
    visibility = ["//visibility:public"],
)

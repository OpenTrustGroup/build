filegroup(
    name = "all",
    srcs = glob(
        ["**"],
        exclude = [
            ".*/**/*",
        ],
    ),
    visibility = ["//visibility:public"],
)

filegroup(
    name = "merge_config",
    srcs = ["scripts/kconfig/merge_config.sh"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "defconfig",
    srcs = ["arch/arm64/configs/defconfig"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "ROOT",
    srcs = ["Makefile"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "gen_init_cpio.c",
    srcs = ["usr/gen_init_cpio.c"],
    visibility = ["//visibility:public"],
)

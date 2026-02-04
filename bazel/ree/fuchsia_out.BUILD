filegroup(
    name = "all",
    srcs = glob([
        "*",
        "exe.unstripped/**",
        "package/**",
        "provision/**",
    ]),
    visibility = ["//visibility:public"],
)

filegroup(
    name = "ROOT",
    srcs = ["finalize_manifests.d"],
    visibility = ["//visibility:public"],
)

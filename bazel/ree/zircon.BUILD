load("@rules_cc//cc:defs.bzl", "cc_library")

filegroup(
    name = "ROOT",
    srcs = ["makefile"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "trusty_user_header_files",
    srcs = glob([
        "trusty/lib/include/user/**",
    ]),
    visibility = ["//visibility:public"],
)

filegroup(
    name = "uapi_err_hdr",
    srcs = ["trusty/lk/trusty/include/uapi/uapi/err.h"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "uapi_mm_hdr",
    srcs = ["trusty/lk/trusty/include/uapi/uapi/mm.h"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "trusty_lk_uapi_header_files",
    srcs = [
        ":uapi_err_hdr",
        ":uapi_mm_hdr",
    ],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "system_header_files",
    srcs = glob(["system/public/**"]),
    visibility = ["//visibility:public"],
)

cc_library(
    name = "system_headers",
    hdrs = [":system_header_files"],
    strip_include_prefix = "system/public",
    visibility = ["//visibility:public"],
)

cc_library(
    name = "trusty_all_headers",
    visibility = ["//visibility:public"],
    deps = [
        ":trusty_lk_headers",
        ":trusty_user_headers",
    ],
)

cc_library(
    name = "trusty_user_headers",
    hdrs = [":trusty_user_header_files"],
    strip_include_prefix = "trusty/lib/include/user",
    visibility = ["//visibility:public"],
)

cc_library(
    name = "trusty_lk_headers",
    visibility = ["//visibility:public"],
    deps = [
        ":trusty_lk_pal_headers",
        ":trusty_lk_shared_headers",
        ":trusty_lk_uapi_headers",
    ],
)

cc_library(
    name = "trusty_lk_shared_headers",
    hdrs = glob([
        "trusty/lk/trusty/include/shared/**",
    ]),
    strip_include_prefix = "trusty/lk/trusty/include",
)

cc_library(
    name = "trusty_lk_uapi_headers",
    hdrs = [":trusty_lk_uapi_header_files"],
    strip_include_prefix = "trusty/lk/trusty/include/uapi",
    visibility = ["//visibility:public"],
)

cc_library(
    name = "trusty_lk_pal_headers",
    hdrs = glob([
        "trusty/lk/trusty/include/pal/**",
    ]),
    strip_include_prefix = "trusty/lk/trusty/include/",
)

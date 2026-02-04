load("@rules_cc//cc:defs.bzl", "cc_library")

cc_library(
    name = "lib",
    srcs = glob(
        include = [
            "src/gtest-all.cc",
            "src/gtest-internal-inl.h",
            "include/gtest/**/*.h",
        ],
        exclude = ["src/gtest_main.cc"],
    ),
    includes = [
        ".",
        "include",
    ],
    textual_hdrs = [
        "src/gtest.cc",
        "src/gtest-death-test.cc",
        "src/gtest-filepath.cc",
        "src/gtest-port.cc",
        "src/gtest-printers.cc",
        "src/gtest-test-part.cc",
        "src/gtest-typed-test.cc",
    ],
    visibility = ["//visibility:public"],
)

cc_library(
    name = "main",
    srcs = [
        "src/gtest_main.cc",
    ],
    visibility = ["//visibility:public"],
    deps = [":lib"],
)

load(":version.bzl", "nebula_version_str")

# Packages under ree-trusty should use this group as visibility
package_group(
    name = "ree_trusty_group",
    packages = ["//..."],
)

package(default_visibility = [":ree_trusty_group"])

config_setting(
    name = "use_gcc",
    values = {"define": "use_gcc=true"},
)

config_setting(
    name = "use_clang10",
    values = {"define": "use_clang10=true"},
)

config_setting(
    name = "cpu-x86",
    define_values = {"cpu-x86": "true"},
)

nebula_version_str(name = "nebula_version")

filegroup(
    name = "ROOT",
    srcs = ["Android.mk"],
)

filegroup(
    name = "android_mk",
    srcs = ["Android.mk"],
)

filegroup(
    name = "application_mk",
    srcs = ["Application.mk"],
)

# Add .git for NEBULA_VERSION_STR
filegroup(
    name = "ndk_makefiles",
    srcs = [
        ":android_mk",
        ":application_mk",
    ] + glob([".git/**"]),
)

genrule(
    name = "ndk-build",
    srcs = [
        ":android_mk",
        ":application_mk",
        ":ndk_makefiles",
        "//openssl:gen-android-arm64-v8a",
        "//openssl:gen-android-armeabi-v7a",
        "//uree:all",
        "//uree/libgz_uree:all",
        "//libion_mtk:all",
        "//libion:all",
        "//libnbl_attest_ree:all",
        "//libnbl_fwk_ree:all",
        "//libnbl_gp:all",
        "//libnbl_gp/tests/gp_echo_ca:all",
        "//libnbl_gp/tests/gp_ut_ca:all",
        "//libnbl_license_ree:all",
        "//libnbl_license_ree/tests:all",
        "//libnbl_loader_ree:all",
        "//libnbl_loader_ree/tests:all",
        "//libnbl_secmgr_ree:all",
        "//libnbl_secmgr_ree/tests:all",
        "//libnebula-cpp:nebula-base",
        "//libseccam-ipc:all",
        "//libseccam-ipc/seccam-test:all",
        "//libtrusty:all",
        "//libtrusty/tipc-test:all",
        "//log:all",
        "//nbl_fwk_test:all",
        "//nbl_headers:all",
        "//ion_test:all",
        "//nbl_daemon:all",
        "//nbl_secmgr_agent:all",
        "//nblmsg:all",
        "//nbl_benchmarks:all",
        "//nblruncmd:all",
        "//storage:all",
        "//mmc-utils:all",
        "@googletest//:ROOT",
        "@hee//:ROOT",
        "@hee//nbl_attest_srv:header_files",
        "@hee//nbl_license_srv:header_files",
        "@hee//nbl_loader_srv:header_files",
        "@zircon//:ROOT",
        "@zircon//:system_header_files",
        "@zircon//:trusty_lk_uapi_header_files",
        "@zircon//:trusty_user_header_files",
    ],
    outs = [
        "ndk/libnebula-cpp/libs/arm64-v8a/libnebula-base_shared.so",
        "ndk/ion_test/libs/arm64-v8a/ion_test",
        "ndk/ion_test/libs/arm64-v8a/libc++_shared.so",
        "ndk/libnbl_attest_ree/libs/arm64-v8a/libnbl_attest_ree_shared.so",
        "ndk/libnbl_attest_ree/libs/armeabi-v7a/libnbl_attest_ree_shared.so",
        "ndk/libnbl_fwk_ree/libs/arm64-v8a/libnbl_fwk_ree_shared.so",
        "ndk/libnbl_fwk_ree/libs/armeabi-v7a/libnbl_fwk_ree_shared.so",
        "ndk/libnbl_fwk_ree/obj/local/arm64-v8a/libnbl_fwk_ree.a",
        "ndk/libnbl_fwk_ree/obj/local/armeabi-v7a/libnbl_fwk_ree.a",
        "ndk/libnbl_license_ree/libs/arm64-v8a/libnbl_license_ree_shared.so",
        "ndk/libnbl_license_ree/libs/armeabi-v7a/libnbl_license_ree_shared.so",
        "ndk/libnbl_license_ree/obj/local/arm64-v8a/libnbl_license_ree.a",
        "ndk/libnbl_license_ree/obj/local/armeabi-v7a/libnbl_license_ree.a",
        "ndk/libnbl_loader_ree/libs/arm64-v8a/libnbl_loader_ree_shared.so",
        "ndk/libnbl_loader_ree/libs/armeabi-v7a/libnbl_loader_ree_shared.so",
        "ndk/libnbl_loader_ree/obj/local/arm64-v8a/libnbl_loader_ree.a",
        "ndk/libnbl_loader_ree/obj/local/armeabi-v7a/libnbl_loader_ree.a",
        "ndk/libnbl_secmgr_ree/libs/arm64-v8a/libnbl_secmgr_ree_shared.so",
        "ndk/libnbl_secmgr_ree/libs/armeabi-v7a/libnbl_secmgr_ree_shared.so",
        "ndk/libnbl_secmgr_ree/obj/local/arm64-v8a/libnbl_secmgr_ree.a",
        "ndk/libnbl_secmgr_ree/obj/local/armeabi-v7a/libnbl_secmgr_ree.a",
        "ndk/libseccam-ipc/libs/arm64-v8a/libseccam-ipc.so",
        "ndk/libseccam-ipc/libs/armeabi-v7a/libseccam-ipc.so",
        "ndk/libseccam-ipc/obj/local/arm64-v8a/libseccam-ipc.a",
        "ndk/libseccam-ipc/obj/local/armeabi-v7a/libseccam-ipc.a",
        "ndk/libtrusty/libs/arm64-v8a/libnbl_trusty_shared.so",
        "ndk/libtrusty/libs/armeabi-v7a/libnbl_trusty_shared.so",
        "ndk/libtrusty/obj/local/arm64-v8a/libnbl_trusty.a",
        "ndk/libtrusty/obj/local/armeabi-v7a/libnbl_trusty.a",
        "ndk/storage/obj/local/arm64-v8a/libgtest.a",
        "ndk/googletest/libs/armeabi-v7a/libgoogletest_shared.so",
        "ndk/googletest/libs/arm64-v8a/libgoogletest_shared.so",
        "ndk/nbl_daemon/libs/armeabi-v7a/nbl_daemon",
        "ndk/nbl_secmgr_agent/libs/arm64-v8a/nbl_secmgr_agent",
        "ndk/nbl_secmgr_agent/libs/armeabi-v7a/nbl_secmgr_agent",
        "ndk/nblmsg/libs/arm64-v8a/nblmsg",
        "ndk/nblmsg/libs/armeabi-v7a/nblmsg",
        "ndk/nbl_benchmarks/libs/arm64-v8a/nbl_benchmarks",
        "ndk/nbl_benchmarks/libs/armeabi-v7a/nbl_benchmarks",
        "ndk/nblruncmd/libs/arm64-v8a/nblruncmd",
        "ndk/nblruncmd/libs/armeabi-v7a/nblruncmd",
        "ndk/storage/libs/arm64-v8a/nbl_storageproxyd",
        "ndk/storage/libs/armeabi-v7a/nbl_storageproxyd",
        "ndk/libtrusty/libs/arm64-v8a/tipc-test",
        "ndk/libnbl_loader_ree/libs/arm64-v8a/ta-loader_test",
        "ndk/libnbl_license_ree/libs/arm64-v8a/ta-license_test",
        "ndk/nbl_fwk_test/libs/arm64-v8a/nbl_fwk_test",
        "ndk/mmc-utils/libs/arm64-v8a/mmc_utils",
    ],
    cmd = """
    ln -sf $$(realpath $$(dirname $(location @googletest//:ROOT))/googletest) $$(dirname $(location :android_mk))/googletest;\
    make -f $(location :android_mk) \
      NDK_PROJ_ROOT=$$(dirname $(location :android_mk)) \
      NDK_HOME=$${ANDROID_NDK_HOME} \
      NDK_OUT=$(@D)/ndk \
      BINDIR=$(BINDIR) \
      HEE_DIR=$$(dirname $(location @hee//:ROOT)) \
      ZIRCON_DIR=$$(dirname $(location @zircon//:ROOT)) \
      JOBS=$$(getconf _NPROCESSORS_ONLN) \
      NEBULA_VERSION_AUTO=$(NEBULA_VERSION_STR) \
      V=0
""",
    local = True,
    toolchains = [":nebula_version"],
)

genrule(
    name = "dropbear-ssh",
    srcs = [
        "@dropbear//:all",
        "@dropbear//:ROOT",
    ],
    outs = ["dropbear"],
    cmd = """
    srcdir="$$(dirname $$(realpath $(location @dropbear//:ROOT)))"
    dstdir="$$(realpath $@)"
    outdir="$$(dirname $${dstdir})/build-dropbear"
    logfile="$$(dirname $${dstdir})/build-dropbear.log"

    export CROSS_COMPILE=$${GCC_TOOLCHAIN_PREFIX}/aarch64-linux-gnu-

    mkdir -p "$${outdir}"
    pushd "$${outdir}" > /dev/null

    $${srcdir}/configure \
      --prefix="$${dstdir}" \
      --host=aarch64-linux-gnu \
      --disable-zlib \
      --disable-lastlog \
      --disable-syslog \
      > "$${logfile}"

    make -j "$$(getconf _NPROCESSORS_ONLN)" \
      PROGRAMS="dropbear dbclient dropbearkey dropbearconvert scp" \
      >> "$${logfile}"
    make install inst_scp >> "$${logfile}"
    popd > /dev/null
    """,
    local = True,
)

genrule(
    name = "socat-tool",
    srcs = [
        "@socat//:all",
        "@socat//:ROOT",
    ],
    outs = ["socat"],
    cmd = """
    srcdir="$$(dirname $$(realpath $(location @socat//:ROOT)))"
    dstdir="$$(realpath $@)"
    logfile="$${dstdir}/build-socat.log"

    export CC=$${GCC_TOOLCHAIN_PREFIX}/aarch64-linux-gnu-gcc
    export CFLAGS="-I$${srcdir}"
    export LD=$${GCC_TOOLCHAIN_PREFIX}/aarch64-linux-gnu-ld

    mkdir -p "$${dstdir}"
    pushd "$${dstdir}" > /dev/null

    $${srcdir}/configure \
      --prefix="$${dstdir}" \
      --host=aarch64-linux-gnu \
      > "$${logfile}"

    ln -s $${srcdir}/procan.c procan.c
    make -j "$$(getconf _NPROCESSORS_ONLN)" >> "$${logfile}"
    popd > /dev/null
    """,
    local = True,
)

genrule(
    name = "iperf3-tool",
    srcs = [
        "@iperf3//:all",
        "@iperf3//:ROOT",
    ],
    outs = ["iperf3"],
    cmd = """
    srcdir="$$(dirname $$(realpath $(location @iperf3//:ROOT)))"
    dstdir="$$(realpath $@)"
    outdir="$$(dirname $${dstdir})/build-iperf3"
    logfile="$$(dirname $${dstdir})/build-iperf3.log"

    export CROSS_COMPILE=$${GCC_TOOLCHAIN_PREFIX}/aarch64-linux-gnu-

    mkdir -p "$${outdir}"
    pushd "$${outdir}" > /dev/null

    $${srcdir}/configure \
      --prefix="$${dstdir}" \
      --host=aarch64-linux-gnu \
      --with-openssl=no \
      > "$${logfile}"

    make -j "$$(getconf _NPROCESSORS_ONLN)" \
      >> "$${logfile}"
    make install
    popd > /dev/null
    """,
    local = True,
)

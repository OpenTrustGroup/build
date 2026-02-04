def copy_single_file(src, dst):
    name = src.split(':')[-1]
    native.genrule(
        name='gen_cp_' + name,
        srcs=[src],
        outs=[dst],
        cmd="cp $< $@",
    )

def build_linux_image(version):
    native.constraint_value(name = "linux_version_{}".format(version), constraint_setting = ":linux_version")
    native.platform(
        name = "linux_{}".format(version),
        constraint_values = [
            "@platforms//cpu:arm64",
            "@io_bazel_rules_nbl//platforms:aarch64-linux-gnu",
            "//:linux_version_{}".format(version),
        ],
    )
    native.genrule(
        name = "linux-image-{}".format(version),
        srcs = [
            "@linux_{}//:all".format(version),
            "@linux_{}//:merge_config".format(version),
            "@linux_{}//:ROOT".format(version),
            "@scripts//:qemu_config_{}".format(version),
        ],
        outs = [
            "linux_{}/.config".format(version),
            "linux_{}/vmlinux".format(version),
            "linux_{}/arch/arm64/boot/Image".format(version),
        ],
        target_compatible_with = [
            "//:linux_version_{}".format(version),
        ],
        cmd = """
        ARCH=arm64
        LINUX_SRC=$$(dirname $$(realpath $(location @linux_{}//:ROOT)))
        QEMU_CFG=$$(realpath $(location @scripts//:qemu_config_{}))
        ARM64_CFG=arch/arm64/configs/defconfig
        LINUX_OUT=$$(realpath $(BINDIR)/linux_{})
        if [[ -n "$${{CCACHE_DIR}}" ]] && [[ -d "$${{CCACHE_DIR}}" ]]; then
          CROSS_COMPILE="$$(which ccache) $${{GCC_TOOLCHAIN_PREFIX}}/aarch64-linux-gnu-"
        else
          CROSS_COMPILE="$${{GCC_TOOLCHAIN_PREFIX}}/aarch64-linux-gnu-"
        fi
        echo "CROSS_COMPILE: $${{CROSS_COMPILE}}"

        if [[ "{}" == "6.6" ]]; then
            KCFLAGS=""
            if [[ $${{TEE_SPD}} != "optee" ]]; then
                ARM64_CFG=arch/arm64/configs/gki_defconfig
            fi
        else
            KCFLAGS="-Wmaybe-uninitialized"
        fi

        rm -rf $(BINDIR)/linux
        ln -sf $${{LINUX_OUT}} $(BINDIR)/linux
        mkdir -p $${{LINUX_OUT}}

        cd $${{LINUX_SRC}} && ARCH=$${{ARCH}} scripts/kconfig/merge_config.sh -m -r -O $${{LINUX_OUT}} $${{ARM64_CFG}} $${{QEMU_CFG}}
        make CROSS_COMPILE="$${{CROSS_COMPILE}}" ARCH=$${{ARCH}} TEXT_OFFSET=0x10000 KCFLAGS=$${{KCFLAGS}} \
            LOCALVERSION= O=$${{LINUX_OUT}} -j$$(getconf _NPROCESSORS_ONLN) V=0
        """.format(version, version, version, version),
        local = True,
    )

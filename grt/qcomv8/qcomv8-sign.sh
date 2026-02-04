#!/bin/bash

# Copyright 2018 GoldenRiver Technologies Co., Ltd. All rights reserved.
#
# The present software is the confidential and proprietary information of
# GoldenRiver Technologies Co., Ltd. You shall not disclose the present
# software and shall use it only in accordance with the terms of the license
# agreement you entered into with GoldenRiver Technologies Co., Ltd.
# This software may be subject to export or import laws in certain countries.

set -e; [[ "${TRACE}" ]] && set -x

FUCHSIA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/" && pwd)"

readonly FUCHSIA_OUT_DIR="${FUCHSIA_DIR}/out/arm64"
readonly BOARD="$1"
readonly PRODUCT="$2"
readonly COMBO_IMG="$3"
readonly COMBO_IMG_SIGNED="$4"

readonly COMBO_IMG_TMP="${COMBO_IMG}.tmp"
readonly COMBO_EMPTY="${COMBO_IMG}.empty"

gzip -9nc "${COMBO_IMG}" > "${COMBO_IMG_TMP}"
cat "${LOCAL_DIR}/sdm670-vm.dtb" >> "${COMBO_IMG_TMP}"
rm -f "${COMBO_EMPTY}" && touch "${COMBO_EMPTY}"
"${LOCAL_DIR}/mkbootimg" --kernel "${COMBO_IMG_TMP}" --cmdline "TERM=xterm kernel.oom.redline-mb=4" --ramdisk "${COMBO_EMPTY}" --base 0 -o "${COMBO_IMG_SIGNED}" --pagesize 4096

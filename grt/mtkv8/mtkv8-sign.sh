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

readonly COMBO_IMG_CFG="${COMBO_IMG}.cfg"
readonly COMBO_IMG_PAD="${COMBO_IMG}.pad"
readonly COMBO_IMG_PAD_ENC="${COMBO_IMG}.enc"
readonly COMBO_AES128_K="$(cat ${LOCAL_DIR}/mtkv8.k)"
readonly COMBO_AES128_IV="$(cat ${LOCAL_DIR}/mtkv8.iv)"
readonly COMBO_AES128_ALIGN="16"

# Step 1: create .cfg for mkimage
# bootdata-combo-{board}-{product}.cfg = img_hdr_lk.cfg + gz_info-{product}.cfg
cat "${LOCAL_DIR}/img_hdr_lk.cfg" > "${COMBO_IMG_CFG}"
cat "${LOCAL_DIR}/gz_info-common.cfg" >> "${COMBO_IMG_CFG}"

# Step 2: create .pad for openssl
# bootdata-combo-{board}-{product}.pad = ALIGN_PADDING(bootdata-combo-{board}-{product}.bin, 16)
cp "${COMBO_IMG}" "${COMBO_IMG_PAD}"
FILE_SIZE=$(wc -c < "${COMBO_IMG_PAD}")
REMAINDER=$((${FILE_SIZE} % ${COMBO_AES128_ALIGN}))
if [[ ${REMAINDER} -ne 0 ]]; then
  dd if=/dev/zero bs=$((${COMBO_AES128_ALIGN}-${REMAINDER})) count=1 >> "${COMBO_IMG_PAD}"
fi

# Step 3: create .enc for mkimage
openssl enc -aes-128-ctr -in "${COMBO_IMG_PAD}" -out "${COMBO_IMG_PAD_ENC}" -K ${COMBO_AES128_K} -iv ${COMBO_AES128_IV}

# Step 4: create signed.bin
"${LOCAL_DIR}/mkimage" "${COMBO_IMG_PAD_ENC}" "${COMBO_IMG_CFG}" > "${COMBO_IMG_SIGNED}"

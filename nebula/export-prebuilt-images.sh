#!/bin/bash
# Copyright 2023 GoldenRiver Technology Co., Ltd.

readonly CUR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

set -eo pipefail; [[ "${TRACE}" ]] && set -x

declare OUT_DIR="${CUR_DIR}/prebuilt-images"

function usage() {
  >&2 echo "usage: $0 [-o OUT_DIR]"
  >&2 echo "default OUT_DIR=${OUT_DIR}"
}

while [[ $# -ne 0 ]]; do
  case "$1" in
    -o)
      if [[ $# -lt 2 ]]; then
        usage
        exit 1
      fi
      OUT_DIR="$(realpath ${2})"
      shift
      ;;
    *)
      usage
      exit 1
      ;;
  esac
  shift
done

IFS='-' read -ra tmp_array <<< "$(find ${CUR_DIR}/out/arm64 -name bootdata-combo-*-*-rel.bin)"
readonly PLATFORM="${tmp_array[2]}"
readonly PRODUCT="${tmp_array[3]}"
readonly IMAGE_OUT_DIR="${OUT_DIR}/${PLATFORM}-${PRODUCT}"

mkdir -p ${IMAGE_OUT_DIR}
cp ${CUR_DIR}/out/build-zircon/build-venus-hee/${PLATFORM}-boot-shim.bin ${IMAGE_OUT_DIR}/boot-shim.bin
cp ${CUR_DIR}/out/build-zircon/build-venus-hee/zircon.bin ${IMAGE_OUT_DIR}/zircon.bin
cp ${CUR_DIR}/out/arm64/bootdata-fs-${PLATFORM}-${PRODUCT}.bin ${IMAGE_OUT_DIR}/bootdata-fs.bin
cp ${CUR_DIR}/out/arm64/bootdata-plid-${PLATFORM}-${PRODUCT}.bin ${IMAGE_OUT_DIR}/bootdata-plid.bin

cp ${CUR_DIR}/out/build-zircon-release/build-venus-hee/${PLATFORM}-boot-shim.bin ${IMAGE_OUT_DIR}/boot-shim-rel.bin
cp ${CUR_DIR}/out/build-zircon-release/build-venus-hee/zircon.bin ${IMAGE_OUT_DIR}/zircon-rel.bin
cp ${CUR_DIR}/out/arm64/bootdata-fs-${PLATFORM}-${PRODUCT}-rel.bin ${IMAGE_OUT_DIR}/bootdata-fs-rel.bin
cp ${CUR_DIR}/out/arm64/bootdata-plid-${PLATFORM}-${PRODUCT}-rel.bin ${IMAGE_OUT_DIR}/bootdata-plid-rel.bin

echo "prebuilt images exported to ${IMAGE_OUT_DIR}"
echo "done."

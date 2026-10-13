#!/usr/bin/env bash
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# Runs the sealed DDK self-test: assemble the sandbox outside the product tree,
# build the declared drivers against the component alone, then prove the build
# read nothing it should not have.
#
# The three steps are separate on purpose.  Assembly makes the product tree
# physically unreachable, the build shows the component is sufficient, and the
# dependency scan shows the build did not quietly reach the host.  Dropping the
# third step leaves a build that passes on the export machine and fails on the
# customer's.

set -Eeuo pipefail

die() {
    echo "error: $*" >&2
    exit 1
}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
DDK_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PYTHON_BIN=$(command -v python3 || command -v python) || die "python is required"

libddk="" toolchain="" makefile="" sandbox=""
source_root="" out_root="" closure_out="" stamp="" jobs="" rename_rules=""
sources=()
extras=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --libddk) libddk="$2"; shift 2 ;;
        --sources) sources+=("$2"); shift 2 ;;
        --extra) extras+=("$2"); shift 2 ;;
        --toolchain) toolchain="$2"; shift 2 ;;
        --rename-rules) rename_rules="$2"; shift 2 ;;
        --makefile) makefile="$2"; shift 2 ;;
        --sandbox) sandbox="$2"; shift 2 ;;
        --source-root) source_root="$2"; shift 2 ;;
        --out-root) out_root="$2"; shift 2 ;;
        --closure-out) closure_out="$2"; shift 2 ;;
        --stamp) stamp="$2"; shift 2 ;;
        --jobs) jobs="$2"; shift 2 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ ${#sources[@]} -gt 0 ]] || die "--sources is required (repeatable, NAME=PATH)"
for required in libddk toolchain makefile sandbox source_root out_root; do
    [[ -n "${!required}" ]] || die "--${required//_/-} is required"
done

echo "==== assemble sealed sandbox ===="
assemble=("$DDK_DIR/tools/assemble_selftest.py")
# The whole-tree sandbox compiles the tree's own spelling, and libddk
# publishes its headers under the delivered one.  Rendering here is what
# closes that: a part is rendered when it is split, so the split path passes
# nothing and rendering twice would be refused.
[[ -n "$rename_rules" ]] && assemble+=(--rename-rules "$rename_rules")
# A component that belongs to one half travels inside it, so the other half's
# sandbox never sees those headers -- which is what makes building it alone
# mean something.
for extra in ${extras[@]+"${extras[@]}"}; do
    assemble+=(--extra "$extra")
done
"$PYTHON_BIN" "${assemble[@]}" \
    --libddk "$libddk" \
    ${sources[@]/#/--sources=} \
    --toolchain "$toolchain" \
    --makefile "$makefile" \
    --out "$sandbox" \
    --source-root "$source_root" \
    --out-root "$out_root"

echo "==== build the delivered sources against the component alone ===="
# Serial by default: the dependency scan below reads the compiler's own .d
# files, so there is no build-log scraping to be confused by interleaved
# parallel output.
# The compiler is named on make's command line rather than linked into the
# sandbox: it ships inside libddk, and a sandbox assembled before it is put
# there -- which is every sandbox in our own pipeline, since the delivery has
# to be built first -- would otherwise need a symlink that appears in no
# delivery.  check_hermetic.py is told the same path and permits reads
# through it.
#
# Each half is built from inside itself, with its own Makefile, the way a
# consumer builds what they receive.  A half that only compiles because the
# other is beside it would pass a build driven from above the two and fail
# for the customer who has one.
abs_toolchain=$(cd -- "$toolchain" && pwd -P)
halves=0
for half_dir in "$sandbox"/*/; do
    half=$(basename "${half_dir%/}")
    [[ "$half" == libddk || "$half" == build ]] && continue
    [[ -f "$half_dir/Makefile" ]] || continue
    echo "---- $half ----"
    make -C "$half_dir" ${jobs:+-j"$jobs"} TOOLCHAIN="$abs_toolchain" all
    halves=$((halves + 1))
done
[[ $halves -gt 0 ]] || die "no half with a Makefile under $sandbox"

echo "==== prove the build stayed inside the sandbox ===="
"$PYTHON_BIN" "$DDK_DIR/tools/check_hermetic.py" \
    --sandbox "$sandbox" \
    --toolchain "$toolchain" \
    ${closure_out:+--closure-out "$closure_out"}

if [[ -n "$stamp" ]]; then
    : > "$stamp"
fi
echo "DDK self-test passed: the component is sufficient and the build was sealed."

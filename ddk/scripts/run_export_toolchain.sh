#!/usr/bin/env bash
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# Exports the compiler that goes with a libddk release, and proves it builds
# one.
#
#   1. export the declared keep set from the pinned prebuilt, strip the
#      target-architecture libraries, and run the strip and source-path gates
#      over everything including the host binaries
#   2. build the whole DDK against the export, sealed -- the only check that
#      says the keep set is complete
#
# The licences are not here.  The export ships as libddk/toolchain, so its
# five upstream texts are declared in ddk_licenses.json alongside everything
# else and installed once, by run_trim.sh, into the one NOTICE the delivery
# has.  What stays here are the checks nothing else can do: the strip and
# source-path gates, and the provenance of those texts.
#
# Step 3 is not optional and not a smoke test.  The keep set here is declared
# rather than measured, because a compiler decides what to run from its own
# installation layout and "what did this build execute" does not answer "what
# will the next one execute": clang forks ld.lld by looking beside itself, and
# a driver that used the integrated assembler today may fork one tomorrow.
# Rebuilding the entire DDK against the export is what turns that declaration
# into evidence.
#
# --strip is required for the same reason it is in run_trim.sh: a release that
# forgot the flag would look exactly like one that did not.

set -Eeuo pipefail

die() {
    echo "error: $*" >&2
    exit 1
}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
DDK_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PYTHON_BIN=$(command -v python3 || command -v python) || die "python is required"

prebuilt="" out="" target="aarch64-fuchsia" objcopy=""
source_root="" out_root="" libddk="" sources="" sandbox="" jobs=""
source_date="" sanitizers="" rename_rules=""
host_libs=()
extras=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prebuilt) prebuilt="$2"; shift 2 ;;
        --out) out="$2"; shift 2 ;;
        --target) target="$2"; shift 2 ;;
        --strip) objcopy="$2"; shift 2 ;;
        --source-root) source_root="$2"; shift 2 ;;
        --out-root) out_root="$2"; shift 2 ;;
        --libddk) libddk="$2"; shift 2 ;;
        --sources) sources="$2"; shift 2 ;;
        --sandbox) sandbox="$2"; shift 2 ;;
        --jobs) jobs="$2"; shift 2 ;;
        --source-date) source_date="$2"; shift 2 ;;
        --rename-rules) rename_rules="$2"; shift 2 ;;
        --host-libs) host_libs+=("$2"); shift 2 ;;
        --extra) extras+=("$2"); shift 2 ;;
        --with-sanitizers) sanitizers=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done

for required in prebuilt out objcopy source_root libddk sources \
                sandbox out_root; do
    [[ -n "${!required}" ]] || die "--${required//_/-} is required"
done
[[ "$objcopy" != "$(command -v objcopy 2>/dev/null || true)" ]] \
    || die "--strip must name the pinned objcopy in the tree, not the host's"

echo "==== export the toolchain ===="
[[ ${#host_libs[@]} -gt 0 ]] || die "--host-libs is required (repeatable): the tools' own libraries must come from a pinned tree, not from this machine"
args=(--source "$prebuilt" --out "$out" --target "$target"
      --objcopy "$objcopy" --source-root "$source_root"
      ${host_libs[@]/#/--host-libs=})
[[ -n "$source_date" ]] && args+=(--source-date "$source_date")
[[ -n "$sanitizers" ]] && args+=(--with-sanitizers)
"$PYTHON_BIN" "$DDK_DIR/tools/export_toolchain.py" "${args[@]}"

echo "==== build the whole DDK against it ===="
selftest=("$SCRIPT_DIR/run_selftest.sh"
          --libddk "$libddk" --sources "$sources" --toolchain "$out"
          --makefile "$DDK_DIR/selftest.mk" --sandbox "$sandbox"
          --source-root "$source_root" --out-root "$out_root")
[[ -n "$jobs" ]] && selftest+=(--jobs "$jobs")
[[ -n "$rename_rules" ]] && selftest+=(--rename-rules "$rename_rules")
# The whole DDK includes the half that carries a component of its own, so
# that component has to be in the sandbox or its headers are missing here
# and nowhere else.
for extra in ${extras[@]+"${extras[@]}"}; do
    selftest+=(--extra "$extra")
done
bash "${selftest[@]}"

echo "toolchain export passed: the keep set builds the delivery it ships with."

#!/usr/bin/env bash
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# The split, tested end to end: whether the two halves are really two halves.
#
# Not a release.  run_release.sh is that, and runs this as its step 6 among
# nineteen others; this is the part to reach for when the question is only
# whether the partition holds.
#
#   1. check that no dependency crosses the halves
#   2. split the DDK source component into its ownership halves
#   3. build each half on its own against libddk, sealed
#   4. export each built half as binaries, and combine them into one tree
#   5. merge the halves' sources back
#   6. prove the merge reproduces what was split, byte for byte
#
# Step 3 is what the split is worth: a half that only compiles because the
# other half is sitting next to it is not a half.  A header reached across the
# boundary fails here, where it is one line to fix, rather than at MTK.
#
# Step 4 is the second delivery tier.  MTK receives both halves as source; a
# client receives only MTK's half as source and ours as binaries.  So a half
# has to be deliverable as binaries alone, and the two have to combine into
# one tree.  That is a union rather than a link, which is what step 1 checks.
#
# Step 6 is what the split is for: MTK returns a changed half, the halves
# merge into a whole again, and diffing that against ours is exactly what MTK
# changed.  Files keep their source-tree path throughout, so that diff applies
# to garnet directly.

set -Eeuo pipefail

die() {
    echo "error: $*" >&2
    exit 1
}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
DDK_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PYTHON_BIN=$(command -v python3 || command -v python) || die "python is required"

libddk="" sources="" toolchain="" rules="" sandbox="" prefix="" source_root=""
out_root="" jobs="" stamp=""
extras=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --libddk) libddk="$2"; shift 2 ;;
        --sources) sources="$2"; shift 2 ;;
        --toolchain) toolchain="$2"; shift 2 ;;
        --rules) rules="$2"; shift 2 ;;
        --sandbox) sandbox="$2"; shift 2 ;;
        --prefix) prefix="$2"; shift 2 ;;
        --source-root) source_root="$2"; shift 2 ;;
        --out-root) out_root="$2"; shift 2 ;;
        --jobs) jobs="$2"; shift 2 ;;
        --stamp) stamp="$2"; shift 2 ;;
        --extra) extras+=("$2"); shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done

for required in libddk sources toolchain rules sandbox source_root out_root; do
    [[ -n "${!required}" ]] || die "--${required//_/-} is required"
done

parts="$sandbox/parts"
merged="$sandbox/merged"
bundles="$sandbox/bundles"
delivery="$sandbox/delivery"
rm -rf "$parts" "$merged" "$bundles" "$delivery"
mkdir -p "$sandbox"

echo "==== split ===="
split_args=(split --tree "$sources" --rules "$rules" --out "$parts" --subset)
[[ -n "$prefix" ]] && split_args+=(--prefix "$prefix")
"$PYTHON_BIN" "$DDK_DIR/tools/partition.py" "${split_args[@]}"

echo "==== build each half against libddk, on its own ===="
for part_dir in "$parts"/*/; do
    part=$(basename "$part_dir")
    echo "---- $part ----"
    selftest_args=(
        --libddk "$libddk"
        --sources "$part_dir"
        --toolchain "$toolchain"
        --makefile "$DDK_DIR/selftest.mk"
        --sandbox "$sandbox/build-$part"
        --source-root "$source_root"
        --out-root "$out_root"
        --closure-out "$sandbox/$part.closure.json"
        --stamp "$sandbox/$part.sealed"
    )
    [[ -n "$jobs" ]] && selftest_args+=(--jobs "$jobs")
    # A component that belongs to one half goes into that half's sandbox and
    # no other: the point of building each alone is that the other half's
    # headers are not there to be found.
    for extra in ${extras[@]+"${extras[@]}"}; do
        [[ "${extra%%=*}" == "$part" ]] && \
            selftest_args+=(--extra "$part=${extra#*=}")
    done
    "$SCRIPT_DIR/run_selftest.sh" "${selftest_args[@]}" | tail -2
done

echo "==== no dependency may cross the halves ===="
check_args=(check)
for part_dir in "$parts"/*/; do
    check_args+=(--source "$(basename "${part_dir%/}")=${part_dir}")
done
"$PYTHON_BIN" "$DDK_DIR/tools/combine_builds.py" "${check_args[@]}"

echo "==== export each half as binaries, and combine ===="
combine_args=(combine --out "$delivery")
for part_dir in "$parts"/*/; do
    part=$(basename "${part_dir%/}")
    "$PYTHON_BIN" "$DDK_DIR/tools/combine_builds.py" export \
        --sandbox "$sandbox/build-$part" --part "$part" \
        --out "$bundles/$part"
    combine_args+=(--bundle "$bundles/$part")
done
"$PYTHON_BIN" "$DDK_DIR/tools/combine_builds.py" "${combine_args[@]}"

echo "==== merge ===="
# --rules because a part records only that a rename was applied, not what it
# was, so undoing one needs the rules file. The parts are not renamed now --
# they keep the tree's `garnet` -- and passing the rules anyway is what makes
# turning it back on a one-line change rather than a hunt.
merge_args=(merge --out "$merged" --rules "$rules")
for part_dir in "$parts"/*/; do
    merge_args+=(--part "${part_dir%/}")
done
"$PYTHON_BIN" "$DDK_DIR/tools/partition.py" "${merge_args[@]}"

echo "==== prove the merge reproduces what was split ===="
"$PYTHON_BIN" "$DDK_DIR/tools/partition.py" verify \
    --original "$sources" --merged "$merged" --rules "$rules"

if [[ -n "$stamp" ]]; then
    mkdir -p "$(dirname -- "$stamp")"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$stamp"
fi

echo "split delivery passed: each half builds alone, the halves combine into"
echo "one binary tree, and their sources merge back into what was split."

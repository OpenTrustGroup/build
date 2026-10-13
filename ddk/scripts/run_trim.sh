#!/usr/bin/env bash
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# Trims a component to what the delivery needs, and proves the result still
# builds it.
#
#   1. build the source set against the full component, and measure it
#   2. trim the component to measured + reachable + declared + policy
#   3. build the same source set again, against the trimmed component -- and
#      against its stripped binaries, which is the only check that catches an
#      objcopy that quietly corrupted them
#   4. install the compiler the delivery was built with, if one was exported
#   5. give what is left its licence texts, and refuse copyleft it cannot
#      elect
#   6. re-seal the manifest, so it describes the component that is actually
#      there rather than the one the trim started from
#
# Step 4 sits exactly there and nowhere else.  Earlier, and the trim deletes
# the compiler (nothing measured it) or rewrites strings inside libc++.so
# (redaction walks every .a and .so).  Later, and 490 files reach the licence
# step unclaimed, or worse, get claimed by the root entry, which says
# GoldenRiver owns them.
#
# --strip is required, not optional.  Every binary that leaves carries DWARF
# otherwise -- our source paths, our function names, our structure layouts --
# and a release that forgets the flag would look exactly like one that did
# not.  trim_ddk.py refuses to produce a component with debug information in
# it whether or not the flag was given, so forgetting it fails loudly.
#
# Step 3 is the whole point.  A trim is a set of deletions justified by a
# measurement, and a measurement is only as good as the build that produced
# it; rebuilding against the result is what turns "nothing appeared to use
# this" into "the delivery still works without it".

set -Eeuo pipefail

die() {
    echo "error: $*" >&2
    exit 1
}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
DDK_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
PYTHON_BIN=$(command -v python3 || command -v python) || die "python is required"

component="" sources="" toolchain="" policy="" sandbox="" out=""
source_root="" out_root="" report="" jobs="" licenses=""
strip_objcopy="" install_toolchain="" rename_rules=""
extras=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --component) component="$2"; shift 2 ;;
        --sources) sources="$2"; shift 2 ;;
        --extra) extras+=("$2"); shift 2 ;;
        --toolchain) toolchain="$2"; shift 2 ;;
        --policy) policy="$2"; shift 2 ;;
        --licenses) licenses="$2"; shift 2 ;;
        --strip) strip_objcopy="$2"; shift 2 ;;
        --install-toolchain) install_toolchain="$2"; shift 2 ;;
        --rename-rules) rename_rules="$2"; shift 2 ;;
        --sandbox) sandbox="$2"; shift 2 ;;
        --out) out="$2"; shift 2 ;;
        --source-root) source_root="$2"; shift 2 ;;
        --out-root) out_root="$2"; shift 2 ;;
        --report) report="$2"; shift 2 ;;
        --jobs) jobs="$2"; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done

for required in component sources toolchain sandbox out source_root out_root \
                strip_objcopy; do
    [[ -n "${!required}" ]] || die "--${required//_/-} is required"
done

selftest() {  # selftest <libddk> <sandbox> <closure-out>
    local args=(
        --libddk "$1" --sources "$sources" --toolchain "$toolchain"
        --makefile "$DDK_DIR/selftest.mk" --sandbox "$2"
        --source-root "$source_root" --out-root "$out_root"
        --closure-out "$3" --stamp "$2.sealed"
    )
    [[ -n "$jobs" ]] && args+=(--jobs "$jobs")
    [[ -n "$rename_rules" ]] && args+=(--rename-rules "$rename_rules")
    # The measurement is of the whole source set, so the half that carries a
    # component of its own needs it here too, or its headers go missing in
    # the very build the trim is measured from.
    local extra
    for extra in ${extras[@]+"${extras[@]}"}; do
        args+=(--extra "$extra")
    done
    "$SCRIPT_DIR/run_selftest.sh" "${args[@]}" | tail -2
}

rm -rf "$sandbox" "$out"
mkdir -p "$sandbox"

echo "==== measure the full component ===="
selftest "$component" "$sandbox/before" "$sandbox/before.closure.json"

echo "==== trim ===="
trim_args=(
    --component "$component"
    --closure "$sandbox/before.closure.json"
    --module-sources "$sources"
    --out "$out"
)
[[ -n "$policy" ]] && trim_args+=(--policy "$policy")
[[ -n "$report" ]] && trim_args+=(--report "$report")
trim_args+=(--strip "$strip_objcopy")
"$PYTHON_BIN" "$DDK_DIR/tools/trim_ddk.py" "${trim_args[@]}"

echo "==== build again, against the trimmed component ===="
selftest "$out" "$sandbox/after" "$sandbox/after.closure.json"

echo "==== nothing the second build read is missing from the trim ===="
"$PYTHON_BIN" - "$out" "$sandbox/after.closure.json" <<'PY'
import json, os, sys
out, closure_path = sys.argv[1], sys.argv[2]
with open(closure_path) as handle:
    used = set(json.load(handle)['libddk'])
present = set()
for current, _, names in os.walk(out):
    for name in names:
        present.add(os.path.relpath(os.path.join(current, name), out)
                    .replace(os.sep, '/'))
missing = sorted(used - present)
if missing:
    sys.stderr.write('the trimmed component is missing %d file(s) the build '
                     'read:\n  %s\n' % (len(missing), '\n  '.join(missing)))
    sys.exit(1)
print('  measured %d files, all present in the trimmed component'
      % len(used))
PY

if [[ -n "$install_toolchain" ]]; then
    echo "==== install the compiler into the component ===="
    # After the trim and its rebuild, and before the licences.  Both halves of
    # that matter.  The trim drops whatever no build measured and no build
    # measures the compiler it ran, so an earlier install would delete it; and
    # redact_source_paths rewrites strings in place inside every .a and .so it
    # finds, five of which would be libc++.so and the compiler runtime.  After
    # the licences would be worse still: nothing would cover 490 files, and
    # the root entry that would claim them says GoldenRiver owns them.
    [[ -d "$install_toolchain" ]] || die "not a directory: $install_toolchain"
    rm -rf "$out/toolchain"
    cp -a "$install_toolchain" "$out/toolchain"
    echo "  installed $(find "$out/toolchain" ! -type d | wc -l) files -> $out/toolchain"
fi

echo "==== install what describes the sources ===="
# BUILD_INFO.json and SDK_CONTENTS.json describe the source set, not the
# component -- which build produced it, and what it holds.  They used to ride
# along in whichever half the partition's default rule swept them into, and
# that half is kept under git by MTK: regenerated metadata that churns every
# release and belongs to nobody's change.  Here they are beside the thing
# they are generated with, and the licence step below covers them.
for name in BUILD_INFO.json SDK_CONTENTS.json; do
    [[ -f "$sources/$name" ]] || continue
    mkdir -p "$out/sources"
    cp "$sources/$name" "$out/sources/$name"
done
echo "  installed $(ls "$out/sources" 2>/dev/null | wc -l) file(s) -> $out/sources"

if [[ -n "$licenses" ]]; then
    echo "==== licences ===="
    "$PYTHON_BIN" "$DDK_DIR/tools/collect_licenses.py" \
        --component "$out" --declaration "$licenses" \
        --source-root "$source_root"
fi

echo "==== seal the manifest ===="
# Last, because everything above puts files in the component: the trim writes
# its report after the manifest, and the licence step rewrites LICENSES/ after
# that.  A manifest written before them claimed two licence texts that had
# been trimmed away and missed the report.
"$PYTHON_BIN" "$DDK_DIR/tools/materialize_ddk.py" --seal "$out"

echo "trim passed: the delivery still builds from what is left."

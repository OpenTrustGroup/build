#!/usr/bin/env bash
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# Makes a release, and proves each part of it before going on to the next.
#
# This is the procedure, not a convenience wrapper: every check below exists
# because something got through without it.  Running the pieces by hand in a
# different order is how a delivery goes out that builds here and nowhere
# else, so the order is fixed and the whole thing runs from nothing.
#
# It lived in a scratch directory for a while and was lost when that was
# cleared.  It belongs in the tree with the tools it drives.
#
#   1     the tools themselves: unit tests on both pythons
#   2     build the product, read how it builds, derive the DDK from that
#   3     materialise libddk into an empty directory
#   4     export the compiler, and rebuild the whole DDK with it
#   5     the sealed self-test over the whole source set
#   6     split into halves, build each alone, combine, merge back
#   7     mirror both halves, so their repositories carry our commits
#   8     trim, strip, redact, install the compiler, licence what is left
#   9     the partition round trip over garnet itself
#   10    the product build, which must not have been broken by any of this
#   11-12 the delivered component and the compiler inside it, audited
#   13    assemble output/, and check every file names a licence
#   14    hold libddk to the last release: nothing removed, renamed or moved
#   15    build from a copy of output/ in an empty environment
#   16    publish: tag every part's repository, write the note
#   17    restore the way MTK do -- clone the three repositories and build
#   19    the delivery and the tree agree
#   20    pack what MTK receive: a patch series per half, libddk whole
#
# 0 and 18 were the certificate the ABI ordinals were encrypted to, and the
# record decrypting.  The private key for it was lost before the second
# release, and the numbers are syscalls.abigen at the revision BUILD_INFO
# names anyway, so the record is gone; the other steps keep their numbers.
#
# Usage: run_release.sh [--out <dir>] [--work <dir>] [--jobs N] [--dirty]
#
#   --out   what the release produces: output/, publish/, logs/ and the
#           reports.  Default out/ddk in the checkout.
#   --work  everything it builds on the way there -- the sandboxes, the
#           exported compiler, the mirror, the staged component, about
#           2.5 GB.  Default ${XDG_CACHE_HOME:-~/.cache}/ddk-release.  Must be
#           outside the checkout, and outside any git checkout.
#
# Both are deleted at the start.  A release starts from nothing, including
# from the last one.
#
# The environment is not inherited on purpose where it matters: step 14 and
# step 16 run make under `env -i`, because the delivery has to build on a
# machine that has nothing set up, and ours does.

set -uo pipefail

# Run from a private copy of this file.
#
# bash reads a script as it executes, by byte offset -- it does not load the
# whole thing first.  Editing this file while a release is in flight makes the
# shell resume wherever that offset now lands, and it dies with a syntax error
# on a line that is perfectly fine.  That has happened: a run reached step 6,
# the file was rewritten underneath it, and bash reported `syntax error near
# unexpected token '('` on an echo three steps further down.
#
# The copy is made once and re-exec'd, so a later edit cannot reach a run
# already going.  DDK_RELEASE_ROOT carries the real location across the
# re-exec, because BASH_SOURCE then points at the copy.
if [ -z "${DDK_RELEASE_ROOT:-}" ]; then
    DDK_RELEASE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
    export DDK_RELEASE_ROOT
    _snapshot=$(mktemp "${TMPDIR:-/tmp}/ddk-release-XXXXXX.sh")
    cat "${BASH_SOURCE[0]}" > "$_snapshot"
    export DDK_RELEASE_SNAPSHOT=$_snapshot
    exec bash "$_snapshot" "$@"
fi

SCRIPT_DIR=$DDK_RELEASE_ROOT
NEBULA=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
OUT=$NEBULA/out/ddk
WORK=${XDG_CACHE_HOME:-$HOME/.cache}/ddk-release
JOBS=16
# A release records the revision of every repository it was built from, and
# refuses to call itself a release when one of them has uncommitted changes:
# the id would name a tree nobody else can check out.  --dirty is for the
# case where that is the point -- bisecting a failure by editing the tree --
# and it downgrades that one check to a warning rather than removing it.
ALLOW_DIRTY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --work) WORK="$2"; shift 2 ;;
        --jobs) JOBS="$2"; shift 2 ;;
        --dirty) ALLOW_DIRTY=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# Absolute, and checked before anything is deleted: both directories are
# removed wholesale below, and one of them now defaults to a path inside the
# checkout, where a slip in --out is a slip in somebody's source tree.
OUT=$(realpath -m -- "$OUT")
WORK=$(realpath -m -- "$WORK")
for pair in "out:$OUT" "work:$WORK"; do
    name=${pair%%:*} dir=${pair#*:}
    case "$NEBULA/" in
        "$dir"/*) echo "--$name $dir contains the checkout; refusing to delete it" >&2
                  exit 2 ;;
    esac
    [ "$dir" = / ] || [ "$dir" = "$HOME" ] && {
        echo "--$name $dir: refusing to delete that" >&2; exit 2; }
done
# The sandboxes are built in here, and a sandbox the product tree can reach
# through a relative path proves nothing: assemble_selftest.py refuses one
# for exactly that reason, three steps in and after a full build.  Refused
# here instead, before any of that.
case "$WORK/" in
    "$NEBULA"/*) echo "--work $WORK is inside the checkout ($NEBULA)." >&2
                 echo "The sealed sandboxes are built there and have to be" >&2
                 echo "outside it; see assemble_selftest.py." >&2
                 exit 2 ;;
esac
case "$OUT/" in "$WORK"/*|"$WORK/") echo "--out may not be inside --work" >&2; exit 2 ;; esac
case "$WORK/" in "$OUT"/*|"$OUT/") echo "--work may not be inside --out" >&2; exit 2 ;; esac
# Nor inside any git checkout -- the repo tool's top-level directory counts.
# The sandboxes are built under --work to prove the delivery builds with
# nothing of the tree in reach, and a checkout above them is something in
# reach.
probe=$(dirname -- "$WORK")
while [ "$probe" != / ]; do
    [ -e "$probe/.git" ] && {
        echo "--work $WORK is inside the git checkout at $probe." >&2
        echo "Choose a directory no commit can reach, e.g." >&2
        echo "--work \$HOME/.cache/ddk-release." >&2
        exit 2; }
    probe=$(dirname -- "$probe")
done
LOGS=$OUT/logs

O=$NEBULA/out/venus-hee/arm64
GN=$NEBULA/buildtools/linux-x64/gn
NJ=$NEBULA/buildtools/linux-x64/ninja
OBJCOPY=$NEBULA/zircon/prebuilt/downloads/gcc/bin/aarch64-elf-objcopy
LLVM=$NEBULA/zircon/prebuilt/downloads/clang/bin
POLICY=$NEBULA/build/ddk/policy/delivery.json
LOCK=$NEBULA/build/ddk/lock/libddk.lock.json
LOCK_SYMBOLS=$NEBULA/build/ddk/lock/libddk.symbols
ZIRCON_GEN=$NEBULA/out/build-zircon/build-venus-hee/gen
DERIVED=$O/ddk-derived
RULES=$NEBULA/build/ddk/policy/partition.json
# libddk is shared with MTK, so what MTK's half needs is what it carries and
# what everyone receives.  What only GRT's half needs travels inside that
# half: a customer gets mtk_ddk and libddk and never sees GRT's headers.
SHARED_HALF=mtk_ddk
PRIVATE_HALF=grt_ddk
SYSROOT=$NEBULA/buildtools/linux-x64/sysroot
# Where each part sits in output/: policy, because it is the name of the
# part's repo project on MTK's side, which nothing in the build knows.  The
# release lays output/ out the way their manifest checks it out, so the
# directory the release produces is the directory they provision.
LAYOUT=$(python3 -c "import sys; sys.path.insert(0, sys.argv[1]); import layout
print(layout.spec(layout.read_policy(sys.argv[2])))" "$SCRIPT_DIR/tools" "$POLICY") \
    || { echo "no usable layout in $POLICY" >&2; exit 2; }
declare -A PART_AT=()
for entry in ${LAYOUT//,/ }; do PART_AT[${entry%%=*}]=${entry#*=}; done
# part_dir <delivery> <part>: where the part is under that delivery.
part_dir() { echo "$1/${PART_AT[$2]:-$2}"; }
IDENTITY="GRT Release <release@goldenrivertek.com>"
# Where a mirror with no release before it starts.  A date, not a count:
# garnet has 10851 commits reaching back to a clone of chromium in 2014, and
# what MTK receive is this project's history rather than the repository's.
#
# A count was what this used to be, and for the default part it was wrong --
# `rev-list HEAD | sed -n '11p'` counts the whole repository, so the start
# landed wherever the last eleven commits happened to fall and bore no
# relation to what the half delivers.  One release left grt_ddk with twelve
# commits out of the 374 its files actually have.
#
# Policy: everything before it is already on MTK's side, imported whole, so
# a mirror that reaches further back only carries history nobody deploys.
# A date or a garnet revision; see delivery.json.
MIRROR_SINCE=$(python3 -c "import json, sys
print(json.load(open(sys.argv[1])).get('mirror_since') or '2025-01-01')" "$POLICY")

cd "$NEBULA"

# The prebuilt clang in the tree is not self-contained: ld.lld is linked
# against libxml2.so.2, and a host whose libxml2 has moved on to another
# soname cannot start it -- so nothing here builds at all.  The exported
# toolchain carries its own copies; this is the same three libraries, for the
# in-tree one that the product build uses.
#
# Three files, not the sysroot directory: that also holds the glibc this
# sysroot was built against, and putting it on the loader's path next to the
# running loader segfaults every tool immediately.
SHIM=${TMPDIR:-/tmp}/ddk-shim-$$
HOSTLIBS=$SHIM/lib
mkdir -p "$HOSTLIBS" "$SHIM/bin"
trap 'rm -rf "$SHIM"; rm -f "${DDK_RELEASE_SNAPSHOT:-}"' EXIT
for f in usr/lib/x86_64-linux-gnu/libxml2.so.2          lib/x86_64-linux-gnu/libz.so.1          lib/x86_64-linux-gnu/liblzma.so.5; do
    cp -L "$SYSROOT/$f" "$HOSTLIBS/$(basename "$f")" 2>/dev/null
done
# NOT exported.  These are a 2013 sysroot's libraries, and putting them on
# every process's loader path drags the host's own tools down with them: xz
# wants XZ_5.4 symbols that this liblzma has never heard of, so anything the
# release runs through xz or tar -- the mirror's git archive pipe among them
# -- dies.  Only the commands that run the in-tree clang get them; see
# `pinned` below.
pinned() { LD_LIBRARY_PATH="$HOSTLIBS${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$@"; }

# `python` has to be python 2.  GN reaches build/zircon/list_source_files.py
# through `#!/usr/bin/env python`, and that file is python 2 -- on a host
# where `python` is python 3 it dies inside gn gen, which reports only that a
# script returned non-zero.  Found rather than assumed: a host where `python`
# is already python 2 needs nothing.
if ! python -c 'import sys; sys.exit(sys.version_info[0] != 2)' 2>/dev/null; then
    PYTHON2=$(command -v python2 || true)
    if [ -z "$PYTHON2" ]; then
        echo "python is $(python -V 2>&1) and there is no python2: the zircon" >&2
        echo "helpers gn runs are python 2 and gn gen would fail." >&2
        exit 2
    fi
    ln -sf "$PYTHON2" "$SHIM/bin/python"
    export PATH="$SHIM/bin:$PATH"
fi
if ! pinned zircon/prebuilt/downloads/clang/bin/ld.lld --version >/dev/null 2>&1; then
    echo "the pinned linker will not run here, so nothing below would build:" >&2
    pinned zircon/prebuilt/downloads/clang/bin/ld.lld --version >&2
    exit 2
fi

# Nothing below reads the terminal.  A command that does by accident -- cat
# given no files -- waits there silently, and the release looks hung.
exec </dev/null

fail=0
step() { echo; echo "########## $* ##########"; }
chk()  { if [ "$1" = 0 ]; then echo "  [OK] $2"; else echo "  [FAIL] $2"; fail=$((fail+1)); fi; }

# Everything under out/ is made again by this run except the last release.
# output/ holds the repositories this one continues: step 7 seeds the
# mirrors from them and step 20 makes the patches against them.  Deleting it
# would give every release a new root commit, and nothing MTK have would
# take a patch from anything we send.  output.last is the one before that,
# kept so a release can be compared with its predecessor after the fact.
rm -rf "$WORK"
if [ -d "$OUT" ]; then
    find "$OUT" -mindepth 1 -maxdepth 1 \
        ! -name output ! -name output.last -exec rm -rf {} +
fi
mkdir -p "$OUT" "$WORK" "$LOGS"

step "1  unit tests, every suite, both pythons"
total=0
# Found, not listed.  The list was written out by hand and a suite added to
# the directory was a suite the release never ran -- green, for the one reason
# nobody checks.  An empty directory is a failure rather than zero of zero.
suites=$(cd build/ddk/tests && ls *_test.py 2>/dev/null | sed 's/\.py$//' | sort)
[ -n "$suites" ] || { echo "  no test suites in build/ddk/tests"; fail=$((fail+1)); }
for t in $suites; do
    three=$(python3 build/ddk/tests/$t.py 2>&1 | tail -3)
    two=$(python2 build/ddk/tests/$t.py 2>&1 | tail -3)
    n=$(echo "$three" | grep -oE 'Ran [0-9]+' | grep -oE '[0-9]+'); n=${n:-0}
    total=$((total+n))
    # A whole line, not a substring: a suite that dies on import prints
    # neither word, and matching loosely reported that as a pass.
    echo "$three" | grep -q '^OK$' && a=OK || a=BAD
    echo "$two"   | grep -q '^OK$' && b=OK || b=BAD
    printf '  %-24s %4s  py3:%-4s py2:%s\n' "$t" "$n" "$a" "$b"
    [ "$a$b" = OKOK ] || fail=$((fail+1))
done
echo "  $total tests"

step "2  build the product, read how it builds, derive the DDK"
# Nothing is declared for this.  The product is built as it is, GN says what
# it concluded and Ninja what every object read, and the delivery follows
# from that, the policy and the lock.  See tools/build_map.py and
# tools/derive_ddk.py.
rm -rf "$O/ddk" "$DERIVED"
mkdir -p "$DERIVED"
#
# The product's own args.gn, untouched.  This used to add an argument to it
# for a GN template the derived delivery no longer uses, and rewriting it with
# grep broke once gn had folded a long path onto a second line: the dangling
# half failed gn gen, into /dev/null, and the map was read from the last run's
# project.json.  So the old graph is removed first, and a failed gen stops.
rm -f "$O/project.json"
( cd "$O" && pinned "$GN" --root=../../.. -q gen . --ide=json ) \
    > "$LOGS/gn-gen.log" 2>&1
r=$?
# --ide=json also writes itself into the rule that regenerates build.ninja.
# The graph is unchanged either way; this puts the rule back, so the product
# directory is what a plain gn gen leaves.
( cd "$O" && pinned "$GN" --root=../../.. -q gen . ) >/dev/null 2>&1
if [ $r != 0 ]; then
    head -20 "$LOGS/gn-gen.log" | sed 's/^/  /'
    chk 1 "gn gen, with the graph exported"
    exit 1
fi
chk 0 "gn gen, with the graph exported"
( cd "$O" && pinned "$NJ" -k0 ) > "$LOGS/prod-first.log" 2>&1
echo "  product built: $(grep -c '^FAILED' "$LOGS/prod-first.log") failure(s), checked at step 10"

python3 build/ddk/tools/build_map.py map --root "$NEBULA" --gn "$GN" --ninja "$NJ" \
    --gn-out "$O" --build-dir "$O" --out "$WORK/build_map.json" 2>&1 | sed 's/^/  /'
chk ${PIPESTATUS[0]} "build map"
python3 build/ddk/tools/build_map.py select --map "$WORK/build_map.json" \
    --policy "$POLICY" --lock "$LOCK" --symbols "$LOCK_SYMBOLS" \
    --build-dir "$O" --nm "$LLVM/llvm-nm" --out "$WORK/selection.json" 2>&1 \
    | sed 's/^/  /'
chk ${PIPESTATUS[0]} "selection"
python3 build/ddk/tools/build_map.py modules --map "$WORK/build_map.json" \
    --rules "$RULES" --makefile build/ddk/selftest.mk \
    --selection "$WORK/selection.json" --lock "$LOCK" \
    --out "$DERIVED/modules" 2>&1 | sed 's/^/  /'
chk ${PIPESTATUS[0]} "module descriptions"
python3 build/ddk/tools/export_abi.py \
    --abigen zircon/system/public/zircon/syscalls.abigen \
    --ordinals "$ZIRCON_GEN/global/include/zircon/zx-syscall-numbers.h" \
    --out "$DERIVED/abi" --stamp "$DERIVED/abi.stamp" 2>&1 \
    | sed 's/^/  /'
chk ${PIPESTATUS[0]} "kernel ABI record"
# Two components out of one plan.  libddk carries what MTK's half needs and
# travels to everyone; what only GRT's half needs travels inside that half,
# so a customer who receives mtk_ddk and libddk never gets GRT's headers or
# archives at all.  Which is which is measured, not listed: the half that
# reads a header or links a library is the half that gets it.
python3 build/ddk/tools/derive_ddk.py component --map "$WORK/build_map.json" \
    --selection "$WORK/selection.json" --rules "$RULES" --root "$NEBULA" \
    --build-dir "$O" --lock "$LOCK" --modules "$DERIVED/modules" \
    --trim-policy build/ddk/policy/trim.json \
    --for shared --shared-half "$SHARED_HALF" \
    --plan "$WORK/component.plan.json" \
    --manifest "$DERIVED/libddk.portable.sdk" \
    --layout "$DERIVED/libddk.layout.json" \
    --stage "$DERIVED/archives" --ar "$LLVM/llvm-ar" --abi "$DERIVED/abi" 2>&1 \
    | sed 's/^/  /'
chk ${PIPESTATUS[0]} "libddk derived"
python3 build/ddk/tools/derive_ddk.py component --map "$WORK/build_map.json" \
    --selection "$WORK/selection.json" --rules "$RULES" --root "$NEBULA" \
    --build-dir "$O" --lock "$LOCK" --modules "$DERIVED/modules" \
    --trim-policy build/ddk/policy/trim.json \
    --for private --shared-half "$SHARED_HALF" \
    --plan "$WORK/private.plan.json" \
    --manifest "$DERIVED/private.portable.sdk" \
    --layout "$DERIVED/private.layout.json" \
    --stage "$DERIVED/archives" --ar "$LLVM/llvm-ar" 2>&1 \
    | sed 's/^/  /'
chk ${PIPESTATUS[0]} "$PRIVATE_HALF's own component derived"
python3 build/ddk/tools/derive_ddk.py sources --map "$WORK/build_map.json" \
    --selection "$WORK/selection.json" --rules "$RULES" --root "$NEBULA" \
    --build-dir "$O" --lock "$LOCK" --modules "$DERIVED/modules" \
    --plan "$WORK/sources.plan.json" \
    --manifest "$DERIVED/sources.portable.sdk" \
    --layout "$DERIVED/sources.layout.json" 2>&1 | sed 's/^/  /'
chk ${PIPESTATUS[0]} "source set derived"
python3 build/ddk/tools/materialize_ddk.py \
    --manifest "$DERIVED/sources.portable.sdk" \
    --layout "$DERIVED/sources.layout.json" \
    --out "$O/ddk/selftest_src" --out-root "$O" --source-root "$NEBULA" 2>&1 \
    | sed 's/^/  /'
chk ${PIPESTATUS[0]} "source set materialised"

step "3  materialise libddk into an empty directory"
python3 build/ddk/tools/materialize_ddk.py \
    --manifest "$DERIVED/libddk.portable.sdk" \
    --layout "$DERIVED/libddk.layout.json" \
    --licenses build/ddk/policy/licences.json \
    --out "$WORK/libddk-export" --out-root "$O" --source-root "$NEBULA" 2>&1 | sed 's/^/  /'
chk ${PIPESTATUS[0]} "export"
python3 build/ddk/tools/materialize_ddk.py \
    --manifest "$DERIVED/private.portable.sdk" \
    --layout "$DERIVED/private.layout.json" \
    --out "$WORK/private-export" --out-root "$O" --source-root "$NEBULA" 2>&1 \
    | sed 's/^/  /'
chk ${PIPESTATUS[0]} "$PRIVATE_HALF's own component"

step "4  export the compiler, and rebuild the DDK with it"
bash build/ddk/scripts/run_export_toolchain.sh \
    --prebuilt zircon/prebuilt/downloads/clang --out "$WORK/toolchain" \
    --target aarch64-fuchsia --strip "$OBJCOPY" \
    --host-libs "$SYSROOT/usr/lib/x86_64-linux-gnu" \
    --host-libs "$SYSROOT/lib/x86_64-linux-gnu" \
    --source-root "$NEBULA" --out-root "$O" --libddk "$WORK/libddk-export" \
    --sources "$O/ddk/selftest_src" --sandbox "$WORK/tc-selftest" \
    --extra "$(basename "$O/ddk/selftest_src")=$WORK/private-export" \
    --rename-rules "$RULES" --jobs "$JOBS" > "$LOGS/toolchain.log" 2>&1
r=$?
grep -E 'kept |bundled |stripped |clang 7|toolchain: [0-9]|sealed|passed:' \
    "$LOGS/toolchain.log" | sed 's/^/  /'
chk $r "toolchain"

step "5  sealed self-test over the whole source set"
bash build/ddk/scripts/run_selftest.sh --libddk "$WORK/libddk-export" \
    --sources "$O/ddk/selftest_src" --toolchain "$WORK/toolchain" \
    --rename-rules "$RULES" --makefile build/ddk/selftest.mk \
    --sandbox "$WORK/selftest" --source-root "$NEBULA" --out-root "$O" \
    --extra "$(basename "$O/ddk/selftest_src")=$WORK/private-export" \
    --closure-out "$OUT/whole.closure.json" --stamp "$OUT/whole.sealed" \
    --jobs "$JOBS" > "$LOGS/selftest.log" 2>&1
r=$?; grep -E 'sealed:' "$LOGS/selftest.log" | sed 's/^/  /'; chk $r "whole tree"

step "6  split: each half alone, combined, merged back"
bash build/ddk/scripts/run_split_selftest.sh --libddk "$WORK/libddk-export" \
    --sources "$O/ddk/selftest_src" --toolchain "$WORK/toolchain" \
    --rules "$RULES" --prefix garnet --sandbox "$WORK/split" \
    --extra "$PRIVATE_HALF=$WORK/private-export" \
    --source-root "$NEBULA/" --out-root "$O" > "$LOGS/split.log" 2>&1
r=$?
grep -E 'sealed|artifacts|union|reproduces|passed:' "$LOGS/split.log" | sed 's/^/  /'
chk $r "split delivery"

# Each half gets its own licence set, here rather than at assembly time: the
# mirror below turns this directory into MTK's repository, so anything that is
# not in it now is not in what they receive.  Adding it afterwards would have
# it deleted by the next sync as a file the delivery does not declare, and
# re-added by the next release -- churn in their history, every time, for a
# file neither side edited.
#
# The build the half ships with goes in first, for the same reason: it is one
# of the half's files, so it has to be there when the licences are collected
# and when the mirror is made.  Installed again at assembly, where a delivery
# says what it contains; the bytes are the same and the licence row stands.
#
# The half that carries a component of its own gets it here too, and before
# the licences for the same reason.  It is scanned under two declarations:
# its sources under the source one, and the component inside it under
# libddk's -- those files are libddk's, and the root entry of the source
# declaration would otherwise claim boringssl and musl as GRT's own.
#
# It is stripped first, and that is the point of doing it here.  The
# component as built is 79 MB of archives carrying DWARF and the paths they
# were compiled from; what ships is the stripped and redacted one.  The
# mirror below commits whatever this directory holds, so a mirror made from
# the larger one leaves every release's DWARF in the history MTK clone --
# mended in the working tree at assembly, never out of the objects.  Nothing
# is cut: the component is already the closure of the half that receives it,
# which is what --keep-all says, and the file set is unchanged either way --
# so the licence scan below reads the same as it did before.
python3 build/ddk/tools/trim_ddk.py --component "$WORK/private-export" \
    --keep-all --out "$WORK/private-release" --strip "$OBJCOPY" \
    --report "$OUT/trim-$PRIVATE_HALF.json" 2>&1 | tail -2 | sed 's/^/  /'
chk ${PIPESTATUS[0]} "$PRIVATE_HALF's own component stripped"

r=0
for half in grt_ddk mtk_ddk; do
    cp build/ddk/selftest.mk "$WORK/split/parts/$half/Makefile"
    declarations=(--declaration build/ddk/policy/source_licences.json)
    if [ "$half" = "$PRIVATE_HALF" ]; then
        cp -a "$WORK/private-release" "$WORK/split/parts/$half/libddk"
        declarations+=(--declaration "libddk=build/ddk/policy/licences.json")
    fi
    python3 build/ddk/tools/collect_licenses.py \
        --component "$WORK/split/parts/$half" "${declarations[@]}" \
        --source-root "$NEBULA" 2>&1 \
        | sed "s|^|  $half |;s|licences installed -> .*/\([^/]*\)/LICENSES.*|licences installed|"
    [ "${PIPESTATUS[0]}" = 0 ] || r=1
done
chk $r "each half names its licences"

step "7  mirror both halves, with our commits in them"
# Before the delivery is assembled, not after: a mirror *is* the half that
# ships, so its history has to exist before anything copies it.
#
# Both halves carry history now.  MTK work in mtk_ddk and their customers
# read it; grt_ddk goes to MTK alone, who are downstream of us and read our
# commits in it.  Neither is a fresh tree each release -- what makes a patch
# possible is that this release's repository continues the last one's.
#
# Which is why the seed is the last release's own repository, out of
# output/.  That directory is still the previous release here: step 13 moves
# it aside, and does it after this.  Mirroring from scratch would give every
# release a new root commit and no patch would apply to what anybody has.
r=0
declare -A MIRROR_OF=()
for part in grt_ddk mtk_ddk; do
    mirror=$WORK/mirror-$part
    MIRROR_OF[$part]=$mirror
    rm -rf "$mirror"
    from=()
    if [ -d "$(part_dir "$OUT/output" "$part")/.git" ]; then
        cp -a "$(part_dir "$OUT/output" "$part")" "$mirror"
        git -C "$mirror" reset -q --hard
    else
        # No release before this one, so the mirror starts at MIRROR_SINCE.
        # Both halves take the same bound: sync_mirror replays only what
        # touches the part, and for the default part -- which owns no prefix
        # to select by -- it decides that by reading each commit's files and
        # asking the rules who owns them, the same question the split answers
        # later.  So the bound is a floor on how far back to look, not a
        # guess at what belongs.
        if git -C garnet cat-file -e "$MIRROR_SINCE^{commit}" 2>/dev/null; then
            start=$(git -C garnet rev-parse "$MIRROR_SINCE^{commit}")
        else
            start=$(git -C garnet rev-list -1 --before="$MIRROR_SINCE" HEAD)
        fi
        [ -n "$start" ] && from=(--from "$start")
        echo "  $part first mirror, from ${start:-the root commit} (since $MIRROR_SINCE)"
    fi
    python3 build/ddk/tools/sync_mirror.py --repo "$NEBULA/garnet" \
        --rules "$RULES" --part "$part" \
        --delivery "$WORK/split/parts/$part" --mirror "$mirror" \
        "${from[@]}" --to HEAD --identity "$IDENTITY" 2>&1 \
        | tail -1 | sed "s/^/  $part: /"
    [ "${PIPESTATUS[0]}" = 0 ] || r=1
    echo "  $part history: $(git -C "$mirror" log --oneline | wc -l) commit(s)"
done
chk $r "mirror"

step "8  trim, strip, redact, install the compiler, licence"
bash build/ddk/scripts/run_trim.sh --component "$WORK/libddk-export" \
    --sources "$O/ddk/selftest_src" --toolchain "$WORK/toolchain" \
    --policy build/ddk/policy/trim.json \
    --licenses build/ddk/policy/licences.json \
    --strip "$OBJCOPY" --install-toolchain "$WORK/toolchain" \
    --rename-rules "$RULES" --sandbox "$WORK/trim" --out "$WORK/libddk-release" \
    --source-root "$NEBULA/" --out-root "$O" --report "$OUT/trim.json" \
    --extra "$(basename "$O/ddk/selftest_src")=$WORK/private-export" \
    > "$LOGS/trim.log" 2>&1
r=$?
grep -E 'component [0-9]|stripped |redacted |measured [0-9]|installed [0-9]|passed:' \
    "$LOGS/trim.log" | sed 's/^/  /'
chk $r "trim"
# The half's own component is stripped in step 6, where it has to be: the
# mirror is made out of that directory, so anything done to it afterwards
# reaches the working tree and not the history.

step "9  partition round trip over garnet"
python3 build/ddk/tools/partition.py roundtrip --tree garnet --rules "$RULES" 2>&1 \
    | tail -1 | sed 's/^/  /'
chk ${PIPESTATUS[0]} "merge(split(garnet)) == garnet"

step "10  the product build, unbroken"
( cd "$O" && pinned "$NJ" -k0 ) > "$LOGS/prod.log" 2>&1
n=$(grep -c '^FAILED' "$LOGS/prod.log")
grep '^FAILED' "$LOGS/prod.log" | sed 's/^/  /'
# syzkaller fails on hosts whose toolchain it predates, and builds elsewhere.
{ [ "$n" = 0 ] || { [ "$n" = 1 ] && grep -q '^FAILED.*syzkaller' "$LOGS/prod.log"; }; }
chk $? "nothing failed but the pre-existing syzkaller ($n)"

step "11  audit the components"
# Both of them: what only GRT's half needs is delivered too, inside that
# half, and a component nobody audits is a component nobody answers for.
python3 - "$NEBULA" "$ALLOW_DIRTY" "$WORK/libddk-release" \
        "$WORK/private-release" <<'PY'
import json, os, sys
sys.path.insert(0, os.path.join(sys.argv[1], 'build/ddk/tools'))
import collect_licenses as cl, trim_ddk
root, allow_dirty, components = sys.argv[1], sys.argv[2] == '1', sys.argv[3:]
bad = []
def check(name, found, fatal=True):
    """A finding fails the release unless it is one this run was told to
    tolerate, which is still printed -- a delivery built from a tree nobody
    can check out should not look like one that was."""
    if found and fatal:
        bad.append('%s: %s' % (component, name))
    label = 'OK' if not found else ('FAIL' if fatal else 'WARN')
    print('  [%s] %s: %s' % (label, name, found or 'none'))
for component in components:
    print('  ---- %s' % os.path.basename(component))
    check('debug information', trim_ddk.check_stripped(component))
    check('source paths', trim_ddk.check_redacted(component))
    declaration = cl.read_declaration(
        os.path.join(root, 'build/ddk/policy/licences.json'), root)
    coverage = cl.scan(component, declaration, cl.BOOKKEEPING)
    check('spdx contradictions', cl.contradictions(component, coverage))
    check('attribution gaps', cl.attribution_gaps(component, coverage))
    check('files we answer for with no notice',
          cl.undeclared(component, coverage))
    manifest = json.load(open(os.path.join(component, 'SDK_CONTENTS.json')))
    disk = set()
    for current, _, names in os.walk(component):
        for name in names:
            disk.add(os.path.relpath(os.path.join(current, name), component)
                     .replace(os.sep, '/'))
    check('manifest disagrees with the tree',
          sorted((disk - {'SDK_CONTENTS.json'})
                 ^ {e['path'] for e in manifest['files']}))
    info = json.load(open(os.path.join(component, 'BUILD_INFO.json')))
    check('dirty repositories', info['dirty'], fatal=not allow_dirty)
    check('toolchain unrecorded',
          [] if (info.get('toolchain') or {}).get('clang_stamp')
          else ['missing'])
    print('  %d component(s), %d file(s)' % (len(coverage), len(disk)))
sys.exit(1 if bad else 0)
PY
chk $? "component audits"

step "12  audit the compiler inside it"
python3 - "$WORK/libddk-release/toolchain" "$NEBULA" <<'PY'
import json, os, sys
sys.path.insert(0, os.path.join(sys.argv[2], 'build/ddk/tools'))
import collect_licenses as cl, export_toolchain as et
toolchain, root = sys.argv[1], sys.argv[2]
bad = []
def check(name, found):
    if found:
        bad.append(name)
    print('  [%s] %s: %s' % ('OK' if not found else 'FAIL', name,
                             found or 'none'))
check('debug information', et.check_stripped(toolchain))
check('source paths outside the allow list', et.check_redacted(toolchain))
record = cl.check_provenance(
    os.path.join(root, 'build/ddk/policy/licenses'))
print('  [OK] licence texts: %d verified' % len(record['files']))
info = json.load(open(os.path.join(toolchain, 'TOOLCHAIN_INFO.json')))
check('cipd stamp unrecorded', [] if info['prebuilt_stamp'] else ['missing'])
check('host path in the record',
      [info['source']] if info['source'].startswith('/') else [])
print('  clang %s / %s / %s' % (info['clang_release'], info['target'],
                                info['prebuilt_stamp'][:12]))
print('  bundled: %s' % ', '.join(info['bundled_libraries']))
print('  still needed from the host: %s' % ', '.join(
    n for n in info['host_requirements'] if not n.startswith(
        ('libc.', 'libm.', 'libdl.', 'librt.', 'libpthread.', 'libgcc_s.',
         'ld-linux'))) or '  (glibc only)')
sys.exit(1 if bad else 0)
PY
chk $? "toolchain audit"

step "13  assemble output/"
# The last release is moved aside, not deleted.  It is what the patches are
# made against and what step 7 seeds the mirrors from, so exactly one is
# kept: output.last, replaced each time.
rm -rf "$OUT/output.last"
if [ -d "$OUT/output" ]; then
    mv "$OUT/output" "$OUT/output.last"
    echo "  last release kept as output.last ($(cat "$(part_dir "$OUT/output.last" libddk)/RELEASE" 2>/dev/null || echo unknown))"
fi
# libddk is built output: there are no commits to replay into it, so its
# history is one commit per release.  The seed is the last release's own
# repository.  Where the last release predates libddk having one -- the
# first run after this change -- that release is replayed as the first
# commit, so the history starts where the deliveries started rather than
# here, and the patches have something to be made against.
#
# It goes in after step 8, never before: run_trim.sh removes this directory
# and builds it again.
IDNAME=${IDENTITY%% <*}
IDMAIL=${IDENTITY#*<}; IDMAIL=${IDMAIL%>}
rm -rf "$WORK/libddk-release/.git"
LAST_LIBDDK=$(part_dir "$OUT/output.last" libddk)
if [ -d "$LAST_LIBDDK/.git" ]; then
    cp -a "$LAST_LIBDDK/.git" "$WORK/libddk-release/.git"
    echo "  libddk history continues from output.last"
elif [ -d "$LAST_LIBDDK" ]; then
    SEED=$WORK/libddk-seed
    rm -rf "$SEED"
    cp -a "$LAST_LIBDDK" "$SEED"
    rm -rf "$SEED/.git"
    was=$(cat "$LAST_LIBDDK/RELEASE" 2>/dev/null || echo unknown)
    git -C "$SEED" init -q
    git -C "$SEED" add -A
    python3 build/ddk/tools/publish_delivery.py --release-log "$SEED" \
        > "$WORK/libddk-seed.msg" 2>/dev/null \
        || echo "Release $was" > "$WORK/libddk-seed.msg"
    git -C "$SEED" -c user.name="$IDNAME" -c user.email="$IDMAIL" \
        commit -q -F "$WORK/libddk-seed.msg"
    git -C "$SEED" tag "release/$was" >/dev/null 2>&1 || true
    cp -a "$SEED/.git" "$WORK/libddk-release/.git"
    echo "  libddk history seeded from output.last ($was)"
else
    # No history to inherit is not the same as wanting none: the component
    # still ships as a repository, so this release becomes its first commit.
    # Reached on the very first release, and again whenever output.last is
    # there but empty -- which is what a failed run leaves behind.
    git -C "$WORK/libddk-release" init -q
    echo "  libddk history starts at this release"
fi
# Both halves come from their mirror rather than from the split: the mirror
# is the same tree with .git beside it, and that history is what ships.
python3 build/ddk/tools/make_delivery.py --libddk "$WORK/libddk-release" \
    --half "grt_ddk=${MIRROR_OF[grt_ddk]}" --half "mtk_ddk=${MIRROR_OF[mtk_ddk]}" \
    --half-component "$PRIVATE_HALF=$WORK/private-release" \
    --makefile build/ddk/selftest.mk --out "$OUT/output" --layout "$LAYOUT" \
    2>&1 | sed 's/^/  /'
chk ${PIPESTATUS[0]} "output/"
r=0
for part in libddk grt_ddk mtk_ddk; do
    at=$(part_dir "$OUT/output" "$part")
    python3 build/ddk/tools/collect_licenses.py --component "$at" \
        --verify 2>&1 | sed "s|^|  $part: |;s|$at|it|"
    [ "${PIPESTATUS[0]}" = 0 ] || r=1
done
chk $r "every file names a licence"
# A part that is a git repository carries the release as its tag, not as a
# file -- a `Release <id>` commit is one MTK's Commit-Check refuses.  Both
# halves are repositories now, so libddk is the only part left with a
# RELEASE file and comparing the files to each other would be one file
# agreeing with itself.  What ties the three together is checked after they
# are tagged, in step 16; here it is only that the stamp was written.
stamps=()
for part in libddk grt_ddk mtk_ddk; do
    f=$(part_dir "$OUT/output" "$part")/RELEASE
    [ -f "$f" ] && stamps+=("$f")
done
stamped=${#stamps[@]}
same=$(cat "${stamps[@]}" 2>/dev/null | sort -u | wc -l)
{ [ "$stamped" -ge 1 ] && [ "$same" = 1 ] && [ -f "$(part_dir "$OUT/output" libddk)/RELEASE" ]; }
chk $? "one release across the $stamped part(s) that carry the file"
# Asked of the directory, not of git: `git -C` walks up until it finds a
# repository, so a part that is not one would report the history of the
# checkout this release was built from.
for part in grt_ddk mtk_ddk; do
    if [ -d "$(part_dir "$OUT/output" "$part")/.git" ]; then
        echo "  $part history: $(git -C "$(part_dir "$OUT/output" "$part")" \
            log --oneline | wc -l) commit(s)"
    else
        echo "  $part carries no history"
    fi
done

step "14  hold libddk to the last release"
# Everything above derived the delivery from the build; this is what stops a
# derivation that moved from shipping.  A header gone, a symbol no longer
# exported, a module renamed or moved, a different kernel ABI: the release
# stops here, before anything is tagged or packed.  If the change is meant,
#   python3 build/ddk/tools/ddk_lock.py accept --delivery $OUT/output \
#       --lock build/ddk/lock/libddk.lock.json \
#       --symbols build/ddk/lock/libddk.symbols --nm <llvm-nm>
# and the lock's diff goes to review with it.
pinned python3 build/ddk/tools/ddk_lock.py check --delivery "$OUT/output" \
    --layout "$LAYOUT" \
    --lock "$LOCK" --symbols "$LOCK_SYMBOLS" --nm "$LLVM/llvm-nm" \
    --report "$OUT/lock-check.json" 2>&1 | sed 's/^/  /'
r=${PIPESTATUS[0]}
chk $r "nothing the last release promised is gone"
# The lock this release would make.  Not written over the one in the tree --
# a release that edits its own checkout is a release whose audit reports it
# dirty -- but beside the delivery, so moving the baseline to this release is
# one copy and a commit that review sees.
#
# Written whether or not the check passed, and that is the point: a release
# that stops is the one whose lock somebody has to read.  Splitting libddk
# removed 313 files on purpose, and the candidate lock was the thing needed
# to move the baseline -- and the only run that would not write it was the
# run that stopped.
if [ -d "$OUT/output" ]; then
    mkdir -p "$OUT/lock"
    pinned python3 build/ddk/tools/ddk_lock.py accept --delivery "$OUT/output" \
        --layout "$LAYOUT" \
        --lock "$OUT/lock/libddk.lock.json" \
        --symbols "$OUT/lock/libddk.symbols" --nm "$LLVM/llvm-nm" >/dev/null
    if [ "$r" = 0 ]; then
        echo "  this release's lock: $OUT/lock/ -- copy to build/ddk/lock/ and"
        echo "  commit to hold the next release to this one"
    else
        echo "  the lock this release would make is in $OUT/lock/. Read its"
        echo "  diff against build/ddk/lock/: it is what stopping was about."
    fi
fi

step "15  build from a copy of output/, in an empty environment"
C=$WORK/verify; rm -rf "$C"
r=0
if [ -d "$OUT/output" ]; then
    # Flat, the way the recipe fetches the parts into WORKDIR: a half finds
    # the component as ../libddk, whatever the layout put them under.
    mkdir -p "$C"
    for part in libddk grt_ddk mtk_ddk; do
        [ -d "$(part_dir "$OUT/output" "$part")" ] && \
            cp -a "$(part_dir "$OUT/output" "$part")" "$C/$part"
    done
    : > "$LOGS/output-build.log"
    for half in "$C"/*/; do
        [ -f "$half/Makefile" ] || continue
        ( cd "$half" && env -i PATH=/usr/bin:/bin make -j"$JOBS" ) \
            >> "$LOGS/output-build.log" 2>&1 || r=1
    done
else
    echo "  no output/ to build"; r=1
fi
built=$(ls -d "$C"/*/build/*/ 2>/dev/null | grep -vcE '/(lib|bin|drivers)/$')
outside=$(find "$C" -name '*.d' -exec cat {} + 2>/dev/null \
    | tr ' \\' '\n\n' | grep '^/' | grep -vc "^$C")
echo "  $built module(s), $outside dependency(ies) outside the sandbox"
{ [ $r = 0 ] && [ "$outside" = 0 ]; }; chk $? "output/ builds"
( cd "$C/mtk_ddk" && env -i PATH=/usr/bin:/bin make -j"$JOBS" BUILD=build_mtk ) \
    > "$LOGS/output-mtk-alone.log" 2>&1
chk $? "one half alone builds"
rm -rf "$WORK/stage"
for half in "$C"/*/; do
    [ -f "$half/Makefile" ] || continue
    ( cd "$half" && env -i PATH=/usr/bin:/bin make -s install \
        DESTDIR="$WORK/stage" ) >/dev/null 2>&1
done
n=$(find "$WORK/stage" -type f 2>/dev/null | wc -l)
echo "  install wrote $n file(s)"
[ "$n" = "$built" ]; chk $? "install"

step "16  publish"
rm -rf "$OUT/publish"
# All three parts are repositories now, and each is tagged where it stands.
# Tagging a copy would leave the one that ships untagged, which is the one
# anybody would look at.
python3 build/ddk/tools/publish_delivery.py --delivery "$OUT/output" \
    --layout "$LAYOUT" --out "$OUT/publish" --identity "$IDENTITY" 2>&1 \
    | sed 's/^/  /'
chk ${PIPESTATUS[0]} "repositories and note"

# What used to be one repository is now three, and nothing in git ties them
# together.  The tie is the release id: libddk carries it as a file, and
# each repository carries it as a tag.  This is where that is checked --
# step 13 could not, because the tags are made here.  Without it a half
# could ship against a component it was never built with.
want=$(cat "$(part_dir "$OUT/output" libddk)/RELEASE")
r=0
for part in libddk grt_ddk mtk_ddk; do
    at=$(part_dir "$OUT/output" "$part")
    if [ ! -d "$at/.git" ]; then
        echo "  $part is not a repository, so it carries no release tag"
        r=1
    elif [ -z "$(git -C "$at" tag -l "release/$want")" ]; then
        echo "  $part is not tagged release/$want"
        r=1
    fi
done
chk $r "all three parts are the same release ($want)"

step "17  restore the way MTK do"
WS=$WORK/workspace; rm -rf "$WS"; mkdir -p "$WS"
# Three repositories and nothing else to fetch: what `repo sync` leaves.
# Beside each other, whatever the layout: that is where the recipe's fetch
# puts them, and where ../libddk resolves.
for part in libddk grt_ddk mtk_ddk; do
    git clone -q "$(part_dir "$OUT/output" "$part")" "$WS/$part"
done
echo "  workspace: $(ls "$WS" | tr '\n' ' ')"
echo "  mtk_ddk history survived the clone: \
$(git -C "$WS/mtk_ddk" log --oneline | wc -l) commit(s), \
tag $(git -C "$WS/mtk_ddk" tag -l | tr '\n' ' ')"
r=0
: > "$LOGS/restore-build.log"
for half in "$WS"/*/; do
    [ -f "$half/Makefile" ] || continue
    ( cd "$half" && env -i PATH=/usr/bin:/bin make -j"$JOBS" ) \
        >> "$LOGS/restore-build.log" 2>&1 || r=1
done
echo "  $(ls -d "$WS"/*/build/*/ 2>/dev/null | grep -vcE '/(lib|bin|drivers)/$') module(s)"
chk $r "clone + make, the way MTK sync it"

step "19  the delivery and the tree agree"
python3 build/ddk/tools/ddk_version.py describe --component "$WORK/libddk-release" \
    --out "$OUT/v-release.json" >/dev/null 2>&1
python3 build/ddk/tools/ddk_version.py describe --tree "$NEBULA" \
    --abigen zircon/system/public/zircon/syscalls.abigen \
    --ordinals "$NEBULA/out/build-zircon/build-venus-hee/gen/global/include/zircon/zx-syscall-numbers.h" \
    --like "$WORK/libddk-release" --out "$OUT/v-tree.json" >/dev/null 2>&1
python3 build/ddk/tools/ddk_version.py compare "$OUT/v-release.json" \
    "$OUT/v-tree.json" 2>&1 | head -4 | sed 's/^/  /'
chk ${PIPESTATUS[0]} "no difference"

step "20  pack what MTK receive"
# The delivery is three directories; what MTK apply is the difference from
# the release they already have.  Both halves are repositories continuing
# the last ones, so their difference is a patch series.  libddk is built
# artifacts -- a diff over a .a reads as nothing -- so it travels whole with
# a list of which files moved.
python3 build/ddk/tools/pack_patches.py --delivery "$OUT/output" \
    --layout "$LAYOUT" --previous "$OUT/output.last" --out "$OUT/patches" \
    --identity "$IDENTITY" 2>&1 | sed 's/^/  /'
chk ${PIPESTATUS[0]} "patches"

echo
echo "########## result ##########"
echo "failed steps: $fail"
if [ "$ALLOW_DIRTY" = 1 ]; then
    echo "  NOT A RELEASE: --dirty was given, so the audit tolerated"
    echo "  uncommitted changes. The id below names a tree that exists"
    echo "  only on this machine; do not hand this delivery to anyone."
fi
du -sh "$OUT/output" 2>/dev/null | sed 's/^/  /'
echo "  logs:    $LOGS"
echo "  work:    $WORK (kept for inspection; the next release removes it)"
echo "  output/: $(for part in libddk grt_ddk mtk_ddk; do
    [ -d "$(part_dir "$OUT/output" "$part")" ] && printf '%s ' "${PART_AT[$part]:-$part}"; done)"
echo "  release: $(cat "$(part_dir "$OUT/output" libddk)/RELEASE")"
echo "  patches: $(ls "$OUT/patches"/ddk-*.tar.gz 2>/dev/null | tail -1)"
[ -d "$OUT/output.last" ] && \
    echo "  previous: output.last ($(cat "$(part_dir "$OUT/output.last" libddk)/RELEASE" \
        2>/dev/null || echo unknown))"
exit $((fail > 0))

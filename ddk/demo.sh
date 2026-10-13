#!/bin/bash
# Walk somebody through a finished DDK delivery, in six acts.
#
# This does not build the delivery -- run_release.sh does that.  This opens
# one that has already been made and shows what it is, in the order somebody
# receiving it would want to be told:  what did I get, can I put it in my
# version control, can I build it, what am I allowed to do with it, and how
# do I know none of that has been quietly tampered with.
#
#   ./demo.sh [output-dir]        one act at a time, Enter to move on
#                                 (default: out/ddk/output in the checkout)
#   DEMO_AUTO=1 ./demo.sh         straight through, no pauses
#   ./demo.sh -l                  list the acts and stop
#   ./demo.sh 3 5                 only acts 3 and 5
#
# Nothing here writes to the delivery.  Act 3 builds in a scratch copy and
# act 6 breaks one, both under a temporary directory that is removed on the
# way out.

set -u

NEBULA=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
OUT=${DDK_DEMO_OUTPUT:-$NEBULA/out/ddk/output}
ACTS=()
for a in "$@"; do
    case $a in
        -l|--list) LIST=1 ;;
        [1-9]) ACTS+=("$a") ;;
        *) OUT=$a ;;
    esac
done

if [ ! -d "$OUT/libddk" ]; then
    echo "no delivery at $OUT" >&2
    echo "pass the output directory, or set DDK_DEMO_OUTPUT" >&2
    exit 1
fi
OUT=$(cd -- "$OUT" && pwd -P)
SCRATCH=$(mktemp -d "${TMPDIR:-/tmp}/ddk-demo-XXXXXX")
trap 'rm -rf "$SCRATCH"' EXIT

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; DIM=$'\033[2m'; G=$'\033[32m'; R=$'\033[31m'; Z=$'\033[0m'
else
    B=; DIM=; G=; R=; Z=
fi

act() {                                  # act <n> <title>
    [ -n "${LIST:-}" ] && { printf '  %s. %s\n' "$1" "$2"; return 1; }
    if [ ${#ACTS[@]} -gt 0 ]; then
        local want= a
        for a in "${ACTS[@]}"; do [ "$a" = "$1" ] && want=1; done
        [ -n "$want" ] || return 1
    fi
    [ -n "${SHOWN:-}" ] && [ -z "${DEMO_AUTO:-}" ] && {
        printf '\n%s-- Enter --%s' "$DIM" "$Z"; read -r _ || true
    }
    SHOWN=1
    printf '\n%s========== %s. %s ==========%s\n\n' "$B" "$1" "$2" "$Z"
}

run() {                                  # echo the command, then run it
    printf '%s$ %s%s\n' "$DIM" "$*" "$Z"
    "$@" 2>&1 | sed 's/^/  /'
    return "${PIPESTATUS[0]}"
}

note() { printf '\n%s\n' "$*"; }

# The licence column of a part's FILES map, counted.  Column offsets are
# sized to the longest path in that part, so they differ between the three;
# the header line says where they start.
licence_census() {
    awk -v col="${2:-LICENCE}" \
        'NR==9 { at = index($0, col); next }
         NR>10 && at { line = substr($0, at); sub(/ +$/, "", line)
                       gsub(/  +/, "  ", line); print line }' \
        "$1/LICENSES/FILES" | sort | uniq -c | sort -rn
}
good() { printf '  %s%s%s\n' "$G" "$*" "$Z"; }
bad()  { printf '  %s%s%s\n' "$R" "$*" "$Z"; }

[ -n "${LIST:-}" ] && echo "acts:"

RELEASE=$(cat "$OUT/libddk/RELEASE")
NEBULA_DECL=$NEBULA/build/ddk/policy/licences.json

# --------------------------------------------------------------------------
if act 1 "What they receive"; then
    run ls -A "$OUT"
    note "Three directories, one repo project each.  ${B}libddk${Z} is ours and
is binary: headers, .so/.a, the sysroot, and the compiler that built them.
${B}grt_ddk${Z} and ${B}mtk_ddk${Z} are source, split along ownership -- MTK edit
theirs, we edit ours, and neither half needs the other's history.  Each half
carries the Makefile that builds it, because a half is what repo manages."
    echo
    for d in libddk grt_ddk mtk_ddk; do
        printf '  %-9s %6s  %5d files\n' "$d" \
            "$(du -sh "$OUT/$d" | cut -f1)" "$(find "$OUT/$d" -type f | wc -l)"
    done
    note "Every part carries the same release id, stamped when it was made:"
    for d in libddk grt_ddk mtk_ddk; do
        printf '  %-9s %s\n' "$d" "$(cat "$OUT/$d/RELEASE")"
    done
    note "The point of libddk being binary is that none of Zircon ships:"
    n=$(find "$OUT/libddk" \( -name '*.c' -o -name '*.cc' -o -name '*.cpp' \) | wc -l)
    if [ "$n" -eq 0 ]; then
        good "0 source files under libddk/ -- headers and objects only"
    else
        bad "$n source files under libddk/"
    fi
    note "The three travel separately, one repository each, so each carries
its own licences rather than pointing at the others:"
    for d in libddk grt_ddk mtk_ddk; do
        printf '  %-9s %2s licence text(s), %5s file(s) in %s/%s\n' "$d" \
            "$(ls "$OUT/$d/LICENSES"/*.txt 2>/dev/null | wc -l)" \
            "$(sed -n '11,$p' "$OUT/$d/LICENSES/FILES" 2>/dev/null | wc -l)" \
            LICENSES FILES
    done
fi

# --------------------------------------------------------------------------
if act 2 "MTK's half is a git repository"; then
    note "MTK asked to manage their half with repo, which means it has to
arrive as a real repository, not a directory that becomes one on their side."
    run git -C "$OUT/mtk_ddk" log --oneline
    echo
    run git -C "$OUT/mtk_ddk" tag
    note "Those commits are the ones we replayed out of garnet -- author,
date and message preserved, so a change they sent us comes back to them
recognisable.  It survives being cloned, which is what repo will do:"
    run git clone --quiet "$OUT/mtk_ddk" "$SCRATCH/cloned"
    printf '  cloned: %s commits, tag %s\n' \
        "$(git -C "$SCRATCH/cloned" rev-list --count HEAD)" \
        "$(git -C "$SCRATCH/cloned" tag | tr '\n' ' ')"
    note "Our people's names come across as themselves, which is what MTK
asked for -- they work with us closely enough to want to know who wrote what:"
    run git -C "$SCRATCH/cloned" log --format='%an <%ae>' | sort -u
    note "What does ${B}not${Z} come across is our review plumbing.  A Change-Id is
an index into a Gerrit they cannot reach, so it is stripped on the way out,
and the message says where the change came from instead:"
    run git -C "$SCRATCH/cloned" log -1 --format=%B 84c7094 2>/dev/null || \
        run git -C "$SCRATCH/cloned" log --format=%B --skip=2 -1
    echo
    for pat in 'Change-Id' 'Co-Authored-By' 'Claude'; do
        n=$(git -C "$SCRATCH/cloned" log --all --format=%B | grep -c "$pat")
        if [ "$n" -eq 0 ]; then good "$pat: 0 lines in the whole history"
        else bad "$pat: $n line(s) -- should be none"; fi
    done
fi

# --------------------------------------------------------------------------
if act 3 "It builds with nothing from this machine"; then
    note "The delivery goes into a Yocto build, so it cannot depend on
whatever happens to be installed here.  Copy it somewhere else and build it
with an empty environment -- no PATH beyond /usr/bin, nothing inherited, and
no compiler of ours anywhere on it."
    cp -a "$OUT" "$SCRATCH/copy"
    printf '%s$ env -i PATH=/usr/bin:/bin make -j%s%s\n\n' "$DIM" "$(nproc)" "$Z"
    t0=$SECONDS
    env -i PATH=/usr/bin:/bin HOME="$SCRATCH" \
        make -C "$SCRATCH/copy" -j"$(nproc)" > "$SCRATCH/build.log" 2>&1
    st=$?
    took=$((SECONDS - t0))
    sed -nE 's|.* -o (build/[^ ]+).*|  \1|p' "$SCRATCH/build.log" | tail -6
    printf '  %s\n' "..."
    echo
    if [ "$st" -eq 0 ]; then
        good "$(grep -c ' -o ' "$SCRATCH/build.log") compile and link steps, ${took}s, exit 0"
        printf '  %s driver module(s), %s librar(y/ies), %s executable(s)\n' \
            "$(find "$SCRATCH/copy/build" -name module.so | wc -l)" \
            "$(find "$SCRATCH/copy/build/lib" -type f 2>/dev/null | wc -l)" \
            "$(find "$SCRATCH/copy/build/bin" -type f 2>/dev/null | wc -l)"
    else
        bad "build failed ($st)"
        tail -15 "$SCRATCH/build.log" | sed 's/^/  /'
    fi
    note "Every one of those was compiled by the clang inside the delivery.
That is not a convenience -- this libc++ is built with ABI namespace
${B}std::__2${Z} instead of the usual __1, so a stock clang would link against
libddk and then not interoperate with it:"
    printf '%s$ libddk/toolchain/bin/clang --version%s\n' "$DIM" "$Z"
    "$OUT/libddk/toolchain/bin/clang" --version 2>&1 | head -1 | sed 's/^/  /'
    printf '  every invocation carried: %s\n' \
        "$(grep -m1 -oE -e '--target=[^ ]+ --sysroot=[^ ]+' "$SCRATCH/build.log")"
    note "And it installs where a bitbake recipe would put it:"
    env -i PATH=/usr/bin:/bin make -C "$SCRATCH/copy" \
        install DESTDIR="$SCRATCH/image" PREFIX=/ddk > "$SCRATCH/install.log" 2>&1 \
        && good "make install DESTDIR=\$D PREFIX=/ddk" || bad "install failed"
    echo
    find "$SCRATCH/image" -mindepth 1 -maxdepth 3 2>/dev/null \
        | sed "s|$SCRATCH/image|\$DESTDIR|" | sort | head -8 | sed 's/^/  /'
    printf '  %s file(s) installed\n' "$(find "$SCRATCH/image" -type f | wc -l)"
fi

# --------------------------------------------------------------------------
if act 4 "Any file, its licence"; then
    note "Legal asked what terms each file ships under.  Rather than a
summary they have to trust, every file in the delivery is listed with the
component that owns it, and the component's full text sits next to it.
This is libddk's; each source half has one of its own, over its own files."
    run sed -n '9,14p' "$OUT/libddk/LICENSES/FILES"
    printf '\n  %s rows, %s licence texts\n' \
        "$(sed -n '11,$p' "$OUT/libddk/LICENSES/FILES" | wc -l)" \
        "$(ls "$OUT/libddk/LICENSES"/*.txt | wc -l)"
    note "What the delivery is made of, by licence:"
    licence_census "$OUT/libddk" | sed 's/^/  /'
    note "The interesting line is the one naming the GPL.  ${B}libfdt is dual
licensed${Z}, GPL or BSD, and the choice belongs to whoever takes the code --
so the GPL text travels inside the header and applies to nobody who elected
otherwise.
Electing is a decision, so it is written down rather than assumed:"
    run grep -m1 -A3 '"name": "libfdt"' \
        "$NEBULA_DECL"
    note "Three other components ship a licence file that talks about the GPL
for parts we do not take -- acrn, xz, trusty.  Each carries a note saying
which part, and the check in act 5 is what stops those notes from drifting."
    note "The source halves get the same treatment, against a declaration of
their own -- a source tree is laid out by where code lives, not by what a
driver includes, so the two are not the same question:"
    for d in grt_ddk mtk_ddk; do
        printf '  %s\n' "$d"
        licence_census "$OUT/$d" COMPONENT | sed 's/^/    /'
    done
    note "That is where the vendored code surfaces: Lua and LuaBridge under
MIT, the UTF-8 routines fxl took from ICU, littlefs.  None of it was named
anywhere before -- it went out as source with no licence beside it."
fi

# --------------------------------------------------------------------------
if act 5 "The delivery checks itself"; then
    note "None of that is worth much if it was only true the day we shipped.
The licence map is re-derivable from the delivery alone:"
    for d in libddk grt_ddk mtk_ddk; do
        printf '%s$ collect_licenses.py --component %s --verify%s\n' "$DIM" "$d" "$Z"
        python3 "$NEBULA/build/ddk/tools/collect_licenses.py" \
            --component "$OUT/$d" --verify 2>&1 | sed "s|$OUT/$d|$d|;s|^|  |"
    done
    note "It re-walks every file, works out which component owns it, and
compares against the shipped map.  It reads no declaration to do that, so it
cannot agree with us by construction.

The other half of the check runs on our side, and it is the one that would
have caught a real mistake.  Take libfdt's election away -- the exact slip
of pasting a GPL-or-BSD file in and not saying which arm we took:"
    python3 - "$NEBULA_DECL" "$SCRATCH/doctored.json" <<'EOF' | sed 's/^/  /'
import json, sys
d = json.load(open(sys.argv[1]))
n = 0
for e in d['components']:
    if e.get('name') == 'libfdt' and 'elected_spdx' in e:
        del e['elected_spdx']
        n += 1
json.dump(d, open(sys.argv[2], 'w'), indent=1)
print('removed elected_spdx from %d libfdt entr(y/ies)' % n)
EOF
    echo
    run python3 "$NEBULA/build/ddk/tools/collect_licenses.py" \
        --component "$OUT/libddk" --declaration "$SCRATCH/doctored.json" \
        --source-root "$NEBULA" --check-only
    st=$?
    echo
    if [ "$st" -ne 0 ]; then
        good "the release cannot be built with an unelected copyleft component"
    else
        bad "it passed -- the copyleft gate is not working"
    fi
fi

# --------------------------------------------------------------------------
if act 6 "Mismatched parts are refused, not merged"; then
    note "The failure we most want to make impossible is somebody syncing
a new libddk against an old grt_ddk, getting a build that links, and shipping
an ABI mismatch.  Break exactly that and see what happens:"
    cp -a "$OUT" "$SCRATCH/mixed" 2>/dev/null
    echo "20250101-deadbeefcafe" > "$SCRATCH/mixed/grt_ddk/RELEASE"
    printf '  libddk  RELEASE = %s\n' "$(cat "$SCRATCH/mixed/libddk/RELEASE")"
    printf '  grt_ddk RELEASE = %s   %s<- synced from an older release%s\n' \
        "$(cat "$SCRATCH/mixed/grt_ddk/RELEASE")" "$DIM" "$Z"
    echo
    run env -i PATH=/usr/bin:/bin make -C "$SCRATCH/mixed/grt_ddk" 2>&1 | tail -6
    st=${PIPESTATUS[0]}
    echo
    if [ "$st" -ne 0 ]; then
        good "refused before compiling anything"
    else
        bad "it built -- the gate is not working"
    fi
    note "It stops at parse time, so there is no half-built tree to clean up
and no linked artefact that might have escaped."
fi

# --------------------------------------------------------------------------
if [ -z "${LIST:-}" ]; then
    printf '\n%s========== %s ==========%s\n' "$B" "release $RELEASE" "$Z"
    printf '  %s\n' "$OUT"
    [ -f "$(dirname "$OUT")/publish/RELEASE.txt" ] && \
        sed 's/^/  /' "$(dirname "$OUT")/publish/RELEASE.txt"
    echo
fi

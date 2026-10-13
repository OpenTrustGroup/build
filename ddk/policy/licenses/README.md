# Licence texts for things whose upstream package ships none

Every other component in the delivery points its `license_file` at a text that
travels with the code it covers.  The prebuilt clang cannot: the CIPD package
under `zircon/prebuilt/downloads/clang` contains **no LICENSE file at all**,
in any directory, while the headers it ships say

    This file is distributed under the University of Illinois Open Source
    License. See LICENSE.TXT for details.

— naming a file that is not in the package.  Redistributing those binaries
without reproducing the notice is the compliance gap we inherit from that
package, so the texts live here instead.

The compiler ships as `libddk/toolchain/`, inside the component, so these are
declared in `ddk_licenses.json` under `toolchain/` prefixes alongside
everything else -- one delivery, one `NOTICE`.

## What is here

| file | covers | upstream | copyright line |
| --- | --- | --- | --- |
| `LLVM.LICENSE.TXT` | `bin/llvm-ar`, `bin/llvm-ranlib` | `llvm/LICENSE.TXT` | 2003-2018 Illinois |
| `CLANG.LICENSE.TXT` | `bin/clang` and its two aliases; the NCSA half of the builtin headers | `clang/LICENSE.TXT` | 2007-2018 Illinois |
| `LLD.LICENSE.TXT` | `bin/ld.lld`, `bin/lld` | `lld/LICENSE.TXT` | 2011-2018 contributors |
| `LIBCXX.LICENSE.TXT` | `include/c++/v1`, `lib/<target>/lib` | `libcxx/LICENSE.TXT` | 2009-2017 contributors |
| `COMPILER_RT.LICENSE.TXT` | `lib/clang/<version>/lib/fuchsia` | `compiler-rt/LICENSE.TXT` | 2009-2018 contributors |

All five are verbatim from `llvmorg-7.0.0`, the release matching the pinned
`clang.sha1`.  They are *not* interchangeable — the NCSA body is shared
between all five and the copyright line is not, which is the whole reason
there are five files here and not one.  A notice carrying the wrong holder is
worse than a missing one.  Fetched once, by hand, and kept here so a release
never needs the network.

`PROVENANCE.json` records the tag, the URL and the SHA-256 of each, and
`export_toolchain.py` verifies all five on every export — an edited text
fails with the URL to fetch the right one back.  Re-check them
against that file rather than re-downloading:

    cd garnet/public/ddk_sdk/licenses
    python -c "import json;print('\n'.join('%s  %s' % (e['sha256'], n) \
        for n, e in sorted(json.load(open('PROVENANCE.json'))['files'].items())))" \
        | sha256sum -c

## The one thing not covered by a text here

107 of the 108 headers under `lib/clang/<version>/include` are MIT with an
individual author's copyright — Chris Lattner, Eli Friedman, Howard Hinnant,
Chandler Carruth — and only `opencl-c.h` is NCSA, which is why that component
is declared `NCSA AND MIT`.  `CLANG.LICENSE.TXT` carries the NCSA half; the
MIT half is carried by each header's own inline permission notice, which
states the "above copyright notice ... shall be included in all copies"
clause itself.  They are declared as `embedded_holders` in
`ddk_licenses.json` rather than added to a text they did not write.

One file under `toolchain/` is not LLVM's: `TOOLCHAIN_INFO.json`, which the
export writes.  It has its own entry so the catch-all beside it does not ship
GRT's own record under NCSA with the LLVM Team named as its holder.

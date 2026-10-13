# The DDK delivery

libddk and the two source halves, grt_ddk and mtk_ddk, are derived from the
product build as it is. Nothing is declared for them in garnet, zircon or
third_party: `tools/build_map.py` reads what GN and Ninja concluded, `policy/`
decides what the build cannot know, and `lock/` holds libddk still from one
release to the next. `run_release.sh` does all of it.

## Why

The first export path discovered the component's contents by scraping the
product Ninja graph and then trimming the result with a real MTK build. That
gives a precise *build closure* for the drivers that exist today, but it is not
an SDK: the sysroot gets trimmed to the libc headers those drivers happen to
include, so a customer cannot compile new code; and the customer-visible
directory layout is derived from producer paths, so a product build-directory
reorganisation is a silent breaking change.

The second declared it instead: every delivered target in garnet restated in
its BUILD.gn what ships, through GN templates kept here, with a layout file
per component. That made the contents a decision rather than an accident, and
it was forty-odd patches to garnet, zircon and third_party that every branch
the DDK is ported to would have had to carry first.

Here the build is read:

* **read** what the product builds -- GN's graph, which config put each flag
  where, and which headers every object opened;
* **decide** only what the build cannot know, in `policy/` -- what ships, who
  owns it, what it is licensed under;
* **hold** it still with `lock/` -- a release that would remove, rename or
  move anything the last one shipped stops for review;
* **measure** the consumer closure separately -- a *check* and a trimming
  input, never the definition.

## Where to start

A release is one command, from the root of the checkout:

    bash build/ddk/run_release.sh [--out <dir>] [--work <dir>] [--jobs N]

What it produces lands in `out/ddk` in the checkout:

    out/ddk/
      output/     the delivery: libddk, grt_ddk, mtk_ddk -- directories, one
                  repo project each; each half carries its own Makefile, and
                  grt_ddk carries the part of the component only it needs
      publish/    RELEASE.txt
      logs/       one per long step, and the three builds done under env -i
      *.json      the closure, the trim report, the version records

What it builds on the way -- the sandboxes, the exported compiler, MTK's
mirror, the staged component, about 2.5 GB -- goes to `--work`, by default
`~/.cache/ddk-release`. That has to be **outside** the checkout: the sealed
sandboxes are built there, and a sandbox the product tree can reach through a
relative path proves nothing, so the script refuses a work directory inside
it before doing anything else. Nor may it be inside any other git checkout,
the repo tool's top directory included. The product's `args.gn` is left as it
is.

**Both are deleted at the start** -- a release begins from nothing, including
from the last one -- so the script refuses a directory that is `/`, `$HOME`,
contains the checkout, or overlaps the other one.

It builds the delivery from nothing and proves each part of it before going
on to the next, in 20 steps: the tools' own tests on both pythons, the
product build and the delivery derived from it, libddk materialised into an
empty directory, the compiler export, the sealed self-test, the split, MTK's
mirror, the trim and licences, the round trip over garnet, the product build
again, two audits, assembly, the lock, a build of the result under `env -i`,
publishing, a restore done the way MTK do it, and the ABI record. It ends with
`failed steps: N` and the path to `output/` and `publish/`.

The order is the procedure, not a convenience. Every check in it exists
because something got through without it, and running the pieces by hand in
a different order is how a delivery goes out that builds here and nowhere
else. The individual scripts below are what it drives; reach for them to
debug one step, not to make a release.

Once one exists, `demo.sh` walks through it in six acts -- what
is in it, MTK's repository, a build with nothing from the host, a licence per
file, the delivery checking itself, and mismatched parts being refused. It
reads `out/ddk/output` unless given another, and writes nothing to it.

### What is decided where

Nothing about the delivery is decided in garnet. The build is read as it
is, and what the build cannot say is decided here, in `policy/`, and held
still by `lock/`:

| File | Decides |
| --- | --- |
| `policy/delivery.json` | what is delivered, what is excluded, what ships compiled |
| `policy/partition.json` | who owns what: the prefixes that are MTK's |
| `policy/licences.json` | what libddk ships under |
| `policy/source_licences.json` | what the source halves ship under |
| `policy/licenses/` | licence texts upstream does not ship, with `PROVENANCE.json` |
| `policy/trim.json` | what the trim keeps that nothing measures |
| `lock/` | what the last release promised; written by a release, committed by review |

garnet, zircon and third_party carry nothing for the delivery. A copy of the
checkout with every build file in them taken back to the base branch
releases 20 of 20 steps and the same files; see "The component and the
source set, derived".

## Pieces

```
build/ddk/
  README.md
  run_release.sh   a release, end to end -- start here
  demo.sh          walk somebody through a finished delivery
  selftest.mk      the standalone build; ships inside each half as its
                   Makefile
  scripts/         the multi-step stages a release drives
  tools/           the Python that does the work
  tests/           one suite per tool
```

What stays at the top is what something outside this directory names:
`run_release.sh` and `demo.sh` are what a person runs, and `selftest.mk` is
copied into each delivered half as its `Makefile`.

**`scripts/`** -- each one a stage: several tools in order, with a check at
the end that means something.

| File | Role |
| --- | --- |
| `run_selftest.sh` | assemble the sealed sandbox, build, scan the dependencies |
| `run_split_selftest.sh` | split, build each half alone, combine, merge, verify |
| `run_trim.sh` | measure, trim, rebuild, and licence the result |
| `run_export_toolchain.sh` | export the compiler, then build the DDK with it |

**`tools/`** -- importable by each other, and run by `run_release.sh` and the
scripts above.

| File | Role |
| --- | --- |
| `materialize_ddk.py` | manifest + layout → component tree |
| `build_info.py` | record which build a component came out of |
| `export_abi.py` | record the kernel ABI the component was built against |
| `ddk_version.py` | describe a delivery or the tree, and compare two |
| `assemble_selftest.py` | lay the sandbox out outside the product tree |
| `check_hermetic.py` | prove the build read nothing outside the sandbox |
| `partition.py` | split a tree into ownership halves, and merge them back |
| `combine_builds.py` | export a built half as binaries; combine halves |
| `trim_ddk.py` | cut a component down to what a delivery needs |
| `collect_licenses.py` | cover a part with licence texts; refuse copyleft |
| `export_toolchain.py` | cut the prebuilt clang down to what a DDK build runs |
| `make_delivery.py` | lay the finished delivery out, and refuse anything else |
| `layout.py` | where each part sits in a delivery: the name of its repo project |
| `sync_mirror.py` | replay our commits into the repository MTK work in |
| `import_from_mirror.py` | turn their commits back into ours |
| `publish_delivery.py` | the repositories and the note |
| `build_map.py` | read how the product builds, and derive modules from it |
| `derive_ddk.py` | plan libddk and the source set from the map |
| `ddk_lock.py` | hold libddk to what the last release promised |
| `deploy_delivery.py` | put a delivery up for review on the Gerrit MTK build from |

**`tests/`** -- `<tool>_test.py` for each tool that has its own suite;
`selftest_test.py` covers `assemble_selftest`, `check_hermetic` and
`selftest.mk`, and `sync_mirror_test.py` covers `import_from_mirror`. Each
runs on its own, from any directory, on python 2 or 3:

    python build/ddk/tests/partition_test.py

Step 1 of a release runs every `*_test.py` it finds in `tests/` on both, so a
new suite needs nothing more than to be put there.

`derive_ddk.py` writes what it plans in the SDK machinery's own form: a
portable manifest whose every source is an `out://` or `source://` locator,
and a layout. `materialize_ddk.py` installs the result.

## The layout contract

The layout `derive_ddk.py` writes names every atom, where it installs, and
why. `materialize_ddk.py` holds a manifest and its layout to three rules, all
hard errors:

1. **Coverage** — an atom that reaches the manifest without a layout entry
   fails, naming the atom. A new dependency cannot silently widen what the
   customer receives.
2. **No stale entries** — a layout entry no atom provides fails.
3. **Root policy** — every destination must start with one of the component's
   `roots` (`include`, `lib`, `sysroot`). Producer paths such as
   `out/build-zircon/build-venus-hee/...` cannot reach the customer.

Two atoms claiming the same destination is an error unless they resolve to the
same source file. The layout is generated now, so these guard the planner
rather than a hand-kept file.

A `source_set` that ships compiled -- Zircon's `zx`, `fbl`, `trace` and
`zxcpp` -- has no archive in the product build, which compiles its sources
straight into each consumer. A standalone consumer cannot link that, so
`derive_ddk.py` archives the product's own objects with `llvm-ar`: libddk
carries what the product compiled, not a second build of it.

MTK drivers spell the bus API as `#include <garnet/public/lib/grt-i2c.h>`,
so the product source path is part of the customer API. libddk publishes it
at that spelling because that is how the code includes it; renaming it is a
customer-visible change and needs MTK.

## The sealed self-test

`scripts/run_selftest.sh` rebuilds the delivered modules from their own
sources against libddk alone, in a sandbox outside the product tree:

```
<sandbox>/
  libddk/          the component under test, with the compiler inside it
  <source root>/   the delivered sources, and nothing else -- one
                   directory per root named on the command line, laid out
                   as the tree is: garnet/..., third_party/..., with
                   selftest.mk as its Makefile and its own build/ under it
```

There is no `toolchain/` link. The compiler ships inside the component, and a
sandbox built before it is put there is told where it is on make's command
line: a symlink would be a path back to the machine that built it, and would
appear in a layout nothing a consumer receives has.

The sandbox is built under `--work`, which must be outside the product tree;
`assemble_selftest.py` refuses anything else.

Isolation is physical rather than advisory. The product source root and build
directory are simply not reachable, so a driver cannot satisfy an include from
`//` and still report a green build. `check_hermetic.py` then scans the
compiler's own `.d` files and fails on any dependency outside the sandbox or
the pinned toolchain — that is what catches a host header the export machine
has and the customer does not.

### The source set is not a copy of Garnet

This matters more than it looks. `mt8678_ufs` depends on
`//garnet/public/lib/blk_crypto`, a GRT-private static library that MTK never
receives. With a full Garnet copy in the sandbox, `blk_crypto.c` would compile
locally and the self-test would pass for a component the customer cannot
actually build against. Only the delivered sources go in, so the gap fails
the build and has to be resolved: either libddk ships the archive, or the
driver stops needing it.

That one was resolved the second way and then the first. The driver-facing
declarations moved to `<zircon/device/blk_crypto.h>`, which leaves
`struct blk_crypto_profile` opaque, and libddk ships `libblk_crypto.a` behind
it. The implementation's own header stayed private.

### The module description is generated, and travels with its code

`build_map.py modules` writes each module description from what the product
compiles for that target -- its sources, the flags its own configs add, the
libraries it links -- so the standalone build cannot drift from the product
one. It installs as `<module>.ddk.mk` beside the sources it describes, not in
a directory of its own: it is derived from that `BUILD.gn`, so it belongs to
the same owner as the code, and a partition by path then carries it to the
right half without a rule of its own. The checked-in `garnet/drivers/block/ufs/mt8678/rules.mk` shows why
that matters: it lists seven sources where `BUILD.gn` lists thirteen, and
`check_product_garnet_sync.sh` excludes both file kinds from comparison, so
nothing reports the divergence.

### What it proves, and what it does not

* **Sufficiency** — nothing the drivers need is missing. This is its value.
* Not **minimality** — that needs the measured closure, which
  `check_hermetic.py` emits as `--closure-out` for a later
  `measured ⊆ shipped` check.
* Not **ownership** — whether a GRT-private file reached the customer is a
  separate question.
* Not **the halves in isolation** — that is what `run_split_selftest.sh`
  adds; see below.

## The split delivery

The delivery is two source halves, one ours and one MTK's, each built against
libddk. `policy/partition.json` declares who owns what, by longest-prefix path
rules: six prefixes are MTK's, everything else is ours.

There are two delivery tiers, and they differ in what is source:

| | source | binary |
| --- | --- | --- |
| to MTK | `grt_ddk` + `mtk_ddk` | libddk |
| to a client | `mtk_ddk` | libddk + `grt_ddk` |

So a half has to be deliverable as binaries alone. `combine_builds.py export`
turns a built half into a bundle — its artifacts and a manifest of them, with
no source in it — and `combine` unions bundles into one delivery tree. What a
half is expected to produce is read from the module descriptions it was built
from, not from whatever is in its output directory, so a half that quietly
stopped building one of its drivers is caught rather than shipped short.

Combining is a union, not a link. That holds only while no module in one half
links a module in the other, which `combine_builds.py check` verifies before
the union is attempted.

`run_split_selftest.sh` runs the split test in one command -- it is step 6 of
a release, and the thing to run on its own when the question is only whether
the partition holds:

1. check that no dependency crosses the halves
2. split the DDK source component into its halves
3. build each half **on its own** against libddk, sealed
4. export each half as binaries, and combine them into one tree
5. merge the halves' sources back
6. prove the merge reproduces what was split, byte for byte

Step 3 is what the split is worth. A half that only compiles because the other
half is sitting next to it is not a half. It is how `machina/virtio_rpmb.h`
was found reaching into `../../drivers/block/ufs/vrpmb/` for the vRPMB wire
protocol — a header the two halves genuinely share, which now travels through
libddk instead.

Step 6 is what the split is for. MTK returns a changed half, the halves merge
into a whole again, and diffing that against ours is exactly what MTK changed,
with nothing else in the way. Files keep their source-tree path throughout, so
that diff applies to garnet directly, and a file MTK adds lands where they put
it.

### The sandbox builds with relative paths

`__FILE__` carries whatever path the compiler was handed, so an absolute one
bakes the sandbox's location into every object that asserts or logs. Two
builds of the same source in two directories then differ, and a half MTK or a
client returns cannot be compared against ours. The pinned Clang is 7.0, which
has `-fdebug-prefix-map` but not `-fmacro-prefix-map`, so the fix is to hand
the compiler relative paths rather than to rewrite them afterwards: make is
always invoked in the sandbox, and everything but the toolchain is named
relative to it.

With that, a client's build of `mtk_ddk` and ours are byte-identical, and so
is the whole combined delivery tree.

`check_hermetic.py` has to resolve those relative paths against the directory
the compiler ran in, not against the dependency file's own directory. The
latter puts them under `build/`, which is a zone, so nothing escapes and
nothing is measured — the check passes while proving nothing.

Three properties make that work, and `partition.py` enforces each rather than
hoping for it:

* **total** — every file lands in some half. By construction: a default part
  answers wherever no rule does.
* **disjoint** — no file lands in two. Also by construction, and a merge
  refuses a path claimed twice rather than resolving it.
* **path-preserving** — merging is a union.

Ownership is longest-prefix, not first-match, so the answer does not depend on
the order the rules are written in and each line can be read on its own. A rule
matching nothing is an error: a stale rule is how a partition silently stops
being what its author last read. Each half carries a digest of the ownership
decision, so halves split under different rules cannot be merged.

`merge(split(garnet))` is garnet, every entry and exact -- 5997 of them when
this was written. Step 9 of the release checks it on every run, so the number
is whatever the tree holds that day and the word that matters is *exact*.

### What comes back

A half returns with what MTK did to it, which is the reason any of this
exists. `merge` therefore reads the half's tree, not its manifest: the
manifest says what we sent, and merging that dropped a file they had added
without a word — a whole driver, reported as `merged 645 entries` and no
complaint. It now says what arrived and what left:

    merged 647 entries
      the sender added 2 and removed 0

Adding a **file** to a module they already have needs nothing from us: the
Makefile finds modules by their descriptions, and a half builds its own, so
`make -C mtk_ddk` picks it up untouched.

Adding a **module** does need something from us, and the merge says so:

    1 module description(s) came back that this build does not generate:
      garnet/drivers/i2c/mt8678_i2c2/mt8678_i2c2.ddk.mk
    Each needs a product target here that policy/delivery.json delivers,
    so the next release derives it.

A description is derived from a product target the policy ships. Committing
one on its own leaves a file the next release overwrites, and the module then leaves the
delivery with nothing said — which is why this is printed where somebody can
still act on it rather than left to be discovered.

### The delivered half keeps garnet's name

A delivered half is laid out the way the tree is: `grt_ddk/garnet/...` and
`mtk_ddk/garnet/...`, and libddk publishes the headers a half includes by path
under `include/garnet/...`. That is the tree's own spelling, so nothing has
to name it: libddk publishes a header at the path the delivered code includes
it by.

For a while it was not. The split renamed `garnet` to `src` on the way out,
on the argument that `garnet` on the top line of a directory listing says
which upstream this derives from, and where every file sat in it, to anybody
who has never opened the code. MTK asked for the name back. They read our
tree against theirs, and a different name at the top means every path in a
bug report, a patch or a review comment has to be translated before it can be
looked up. That cost is paid every day by the people using the delivery; the
other is paid once by a reader who was going to work it out anyway.

`partition.py` still supports renaming, and the rules file records why it is
off. Turning it back on is a `rename` block in `policy/partition.json`; the
split self-test compiles the delivered sources against the component on every
run, so a header libddk still publishes under the old name fails on the first
include. What follows is what anybody doing that needs to know,
because none of it is obvious and all of it was learned the hard way.

**The rewriting is structural, not textual.** A top-level name also occurs
in prose -- `clear its src/dst registers` in the MT8678 SPI driver, a comment
in `virtio_i2c.cc` naming a path on MTK's side -- so a rewrite that touched
every occurrence could not be undone without corrupting comments. Only three
positions are rewritten: the path inside an `#include`, the value of a make
field known to hold paths (`MODULE_SRCS`, `MODULE_INCLUDE_DIRS`), and a JSON
string. A bare word is never a path: a rules file renaming to `src` says
`"to": "src"` in its own text, and a rewriter that swapped every string equal
to the name would undo itself into `"to": "garnet"`.

**Every file is checked by undoing the rewrite and comparing the bytes**,
every time, before it is written. Reasoning about whether a regex is
one-to-one is how a round trip quietly stops being one. The tree had
`#include <src/lauxlib.h>` in `guest_config.cc`, which no leading-component
rule can tell from a path that was renamed; the check caught it, and the
include is now spelled the way every other one is. A policy file whose
prefixes are already written in delivered space -- the source licences were,
briefly -- trips the same check and has to be kept out of the rewrite.

**The manifest inside a renamed part records `"renamed": true` and not the
mapping** -- writing `"from": "garnet"` in it would put the name back on the
one line the rename exists to clear. `merge` therefore needs `--rules`, and
the release passes them whether a rename is declared or not, so that turning
one on is not also a hunt through the scripts.

## Reading the build instead of declaring it

The delivery used to start from declarations in garnet: each delivered
driver's BUILD.gn said it ships and restated its flags, libraries and headers
for the standalone build. That was forty-odd patches to garnet, and porting
the DDK to another branch meant porting them first. `tools/build_map.py`
replaced them, and it is what the release reads.

GN has evaluated every template, config and conditional by the time it
writes its Ninja files, and says what it concluded: `gn gen --ide=json` for
the graph, `gn desc --blame` for which config put each flag on each target.
Ninja's dependency log says which headers every object read. `build_map.py
map` reads all three, from the product's own build directory, into one
document: every compiling target on every toolchain,
with its sources, the headers it read, its dependencies, the libraries it
links, and each define, flag and include directory together with where it
came from. It adds nothing to the tree it reads.

`build_map.py modules` derives module descriptions from that, given which
targets ship and under what name. The rules are not obvious, and each came
from a module that derived wrongly:

* **Where a value came from decides whether it is the module's.** A target's
  own configs are; the toolchain's defaults are not, since the Makefile
  carries them for every module; Zircon's are libddk's. A dependency's public
  defines are the module's, because they change how its headers compile; its
  public warnings are not, because they change no object.
* **A source set compiles into what depends on it.** liblua has no sources of
  its own. A source set belongs to one library module, and anything else that
  reaches it links that module -- fxl_lib is fxl's, not fsl_io's too. Two
  drivers sharing one each compile it, since neither links the other.
* **A module is a module on every toolchain.** A driver on the shared
  toolchain depends on the shared variant of a library.
* **Below an archive libddk provides, only libraries are collected.**
  vm_config needs protobuf_full beside it. blk_crypto's BUILD.gn lists fxl,
  its archive references none of fxl's symbols, and following the edge made
  MTK's UFS driver depend on GRT's source.
* **A library nothing can provide is left out and reported.** The product
  links librng.a beside libcrypto.so; libddk's libcrypto.so already holds it.
* **Names are policy.** Five GN libraries are called `cpp`.

Before the declarations went, `build_map.py compare` checked the derivation
against the delivery they produced. All 46 declared modules were found; every
file their objects read was either in the half or provided by libddk, with
none from neither; and the 46 derived descriptions, swapped into the declared
source set, **passed the sealed self-test and the split self-test** -- each
half built alone and the merge reproduced the source set exactly.

The textual differences were where the declarations and the product
disagreed, and in each case checked, the product was right:
`vmlog_srv.cc` has `#if VMLOG_SINKING_USE_NOTIFY_MODE`, which guest's
declaration never defined; liblua is built without `LUA_USE_POSIX`, which the
declaration added; mt8678's board driver links `mt8678-eint-hw.c`, which the
declaration left out. The rest are dependencies the declarations listed and
nothing uses -- fidl_cpp on fxl, whose headers none of its objects read.

### Holding libddk still

Derived faithfully, every move the build makes reaches libddk: a target
renamed, a header split, a dependency added. A consumer who built against the
last release should not find any of that out at link time. `lock/` records
what the last release promised, and `tools/ddk_lock.py check` holds a new
delivery to it:

| change | |
| --- | --- |
| a file, library, symbol or module added | reported |
| a header edited, a library exporting more | reported |
| a file removed, or a symbol a library no longer exports | **stops** |
| a module removed or renamed, or moved to the other half | **stops** |
| a different kernel ABI fingerprint | **stops** |

A release that stops is waiting for a person, not broken: `ddk_lock.py
accept` rewrites the lock from the delivery, and the lock's diff in review
is both the decision and its record. `lock/libddk.lock.json` holds every
surface file's hash, every module's name, half and type, the fingerprint and
the compiler; `lock/libddk.symbols` holds one `library<TAB>symbol` per line,
so an accepted change reviews as the list of what went. The encrypted
ordinals are the one surface file not compared by hash, since they differ on
every build.

The first lock was taken from release 20260910-d44e456d8270, so the module
names that release shipped -- `fsl_io`, `app_cpp`, `littlefs_upstream` -- are
the names from here on, and only a module that is new gets one by rule.

### What ships is policy; the rest is read

`policy/delivery.json` is the part the build cannot know -- what MTK are given and
what stays closed -- in patterns rather than lists:

| key | |
| --- | --- |
| `deliver` | drivers and programs, by directory or subtree; a library only when named outright |
| `exclude` | what those patterns match and does not ship: the QEMU board driver, a demo program |
| `binary` | shipped compiled: Zircon, third-party code, GRT's private block libraries |
| `source` | exceptions to `binary`: littlefs |

The more specific pattern wins. Tests need no pattern -- a target GN marks
`testonly` never ships -- and neither does generated code, which always ships
compiled.

`build_map.py select` applies it. Everything the roots reach is a libddk
library if binary, a module if it is a library in its own right, and folded
into what uses it otherwise; the rules for which are in its docstring, and
each came from a module that selected wrongly. Names come from the lock: a
module the last release shipped keeps its name and its type, so fxl stays an
archive though the product links it shared. A binary library is named after
the released library that exports the same symbols, which is how
`nbl_fwk` is found to be `libnbl_fwk_hee.a` and `app/fidl` to be
`libfidl_component.a` with nothing written down. Only something new is named
by rule.

On this tree, from the policy and the lock and nothing else, selection
chooses the 46 modules the lock holds, by the names it holds; the module
descriptions derived from it pass the sealed and split self-tests against
the released, trimmed libddk. What the product links and that libddk does
not ship -- pretty, trace, trace-engine, ddktl, rng -- is left out and
listed, and the links prove the trim was right to drop it.

### The component and the source set, derived

`tools/derive_ddk.py` plans what `ddk_sdk()` and `ddk_driver_sources()` used
to declare, and writes it as the portable manifest and layout
`materialize_ddk.py` already reads, so materialising, sealing, licensing and
trimming are unchanged. What keeps the published structure still is the lock
now, not a hand-kept layout. Its docstring has the rules; each placed a real
file wrongly first:

* libddk's headers are what the delivered objects read, **at every spelling
  the delivered code uses** -- rapidjson is `<rapidjson/document.h>` in six
  places and `"third_party/rapidjson/rapidjson/document.h"` in eleven -- and
  what those include, a quoted include landing beside its includer. Includes
  are followed only where the build read the result or the last release
  shipped it, so protobuf's atomicops for other architectures stay and
  rapidjson's MSVC-only msinttypes do not come in. An include in a comment is
  not one: rapidjson's reader.h shows usage in a doc comment.
* One header spelled two ways is one file. The product reaches `ulib/zx`'s
  headers through both `-I .../include` and `-I .../include/lib`, so
  `<zx/time.h>` and `<lib/zx/time.h>` name one file there; published at both
  paths they would be two, each `#pragma once`'d on its own, and a unit
  spelling it both ways -- `callback/scoped_task_runner.cc` on mt8668 -- would
  define `zx::duration` twice. The file goes under `lib/` only, and the
  Makefile searches `include/lib` as well, so the short spelling resolves as
  it does in the product.
* Taking library include directories whole was tried and refused by the
  licence gate at export: it carried headers nobody delivered reads, under
  licences the declaration never covered.
* A header one half reads from the other is published by libddk, at the
  other half's spelling only.
* A module's source is every directory its sources and listed headers are
  in, less headers only tests list; what the last release shipped in a half
  and is still read stays there, and is not published by libddk as well.
* The sysroot is the product's, whole, less what is neither a header nor a
  binary. A source set shipped as a library is archived from the product's
  own objects.
* A header the last release shipped under `include/` stays while the tree
  still has it, however the build now reaches it. Without the declarations,
  guest_sos reads `<zircon/nebula/platform_abi.h>` through the sysroot
  instead of `zircon/system/public`, so nothing read it at `include/`; a
  consumer of the last release still includes it from there, and the lock
  stopped the release until this rule existed.

On this tree the plan is exactly the release's: all 309 headers at the same
paths with the same bytes, all 38 libraries, all 319 sysroot files, and the
599-file source set; what else it carries is what the release's trim dropped.
Wired into the release, all 20 steps pass and the lock calls the delivery
compatible -- fifteen libraries rebuilt from the product's objects with the
same symbols, and mt8678's board driver with the source its declaration
left out.

Then the declarations went. The policy files moved here from garnet, and a
copy of the checkout with every `BUILD.gn`, `.gni` and `rules.mk` in garnet,
zircon, build and third_party taken back to the base branch released 20 of 20
steps: the same files as the declared tree, the source halves byte for byte,
and libddk compatible with the lock. It found one real bug the declarations
had been hiding -- fxl never compiled `logging_rate_limiter.cc`, so the
unoptimised link of guest failed -- and that is fixed in fxl itself.

## Trimming

The component the build produces is what libddk *may* contain. What ships is
narrower. A header nobody compiles against still names our functions, our
structures and our subsystems: `ddk/protocol/usb.h`, `sdhci.h`, `bt-hci.h` and
`intel-gpu-core.h` shipped in a delivery that contains no USB stack, no SD
card driver, no Bluetooth and no display framework, and each one is a free
hint to anybody reading it. Trimming is how what may ship and what does ship
stop being the same thing.

`trim_ddk.py` builds the keep set from four rules, and the report records
which one saved each file:

| rule | what it keeps | why it is not enough on its own |
| --- | --- | --- |
| `measured` | what the compiler read and the linker opened | only the paths this configuration took |
| `reachable` | what a kept header includes, to a fixed point | read textually, so a guarded include counts |
| `declared` | libraries the module descriptions name | `-lvirtio` fails on a missing file, not an unused one |
| `policy` | files kept by declaration, with a reason | entry points nothing includes yet |

The last three are each there because the first one alone was wrong:

* `--trace` reports the archives a linker took something from, not the ones it
  was pointed at. Trimming to it removed `libasync.a` and `libvirtio.a`, and
  the next link failed with *unable to find library*.
* A header reached only through an `#ifdef` this build did not take would be
  dropped, and the customer whose configuration differs finds out first.
* `sysroot/` is upstream musl and Fuchsia's own ABI. Removing the parts this
  build did not touch hides nothing — the same headers are public — while a
  consumer who writes `#include <stdio.h>` breaks. All cost, no benefit, so
  the policy keeps it whole.

### Every binary is stripped, and it is checked rather than assumed

The archives are built with `-g`, so each one carries DWARF: function names,
parameter names, structure layouts, line numbers and the source paths they
came from. `libzx.a` named eighteen Zircon sources and `libnbl_fwk_hee.a`
thirteen of ours. That is a far larger surface than the headers a trim
removes, and nothing needs it to link, so `--strip` is required rather than
optional — the whole component goes from 71 MB to 16.

The tool is the pinned `zircon/prebuilt/downloads/gcc/bin/aarch64-elf-objcopy`.
It reads an archive directly and it is in the tree, so a release machine needs
nothing installed and the CI image needs no new package. The alternatives all
fail: the pinned clang is LLVM 7, whose `llvm-objcopy` cannot open an archive
at all and, stripping members one at a time, writes objects that `llvm-nm`
reads happily and the linker then rejects with *invalid section index*; a
host GNU `objcopy` is built for x86-64 and does not recognise aarch64, but
**exits zero** while saying so, leaving the file untouched. So any output at
all from the stripper is treated as a failure.

None of that is trusted. `trim_ddk.py` reads the ELF section headers itself,
in Python, and refuses to produce a component in which any shipped binary
still has a `.debug_*` section — whether or not `--strip` was given, and
whichever tool ran. A check that asks the same tool it is checking would have
passed all three of the failures above. The scanner agrees with
`llvm-readelf` on all 53 binaries in the component.

What this costs: nobody can step into libddk with a debugger any more.

### And the paths stripping cannot reach

Stripping removes DWARF. It does not touch the strings the compiler put in
`.rodata` for `assert()`, and 473 of those survive: `libfs.a` alone names
`system/ulib/fs/connection.cpp` and twenty-four more. Fifty-three of them are
ours; the rest are boringssl's and protobuf's, which reveal nothing anyone
could not read off the headers.

The archives are rewritten in place instead of compiled differently, because
half of them come out of Zircon's own make build and changing how that
compiles would change the product. Each path is cut down to its basename and
the rest of the buffer filled with NUL, so the string only ever gets shorter
where it already sits: no relocation moves, no section changes size, and an
assert still says which file it fired in.

The pattern is anchored on a NUL to the right and on a non-printable byte to
the left, so it matches a whole string and not the tail of a longer one —
`cannot open dir/file.c` is a message, not a path, and a bare `config.h` names
nothing worth hiding. Anchoring on a *preceding* NUL was not enough: a string
laid down straight after code is preceded by the last instruction, and
fourteen protobuf paths reached the linked binary that way while the archive
looked clean.

A second gate refuses a component in which any binary still names a directory.
With it, nothing from our source tree reaches what a consumer builds: their
`guest_sos`, `machina` and the MTK drivers carry zero paths from `zircon/`,
`hee-trusty/`, `boringssl/` or `protobuf/`.

Trimming is deliberately **not** part of the component's build. What may ship
is declared; what does ship is derived from a measurement, and a measurement
is only as good as the build that produced it. `run_trim.sh` therefore builds
the source set twice — once against the full component to measure it, once
against the trimmed one to prove the delivery still works from what is left.

## Licences

Two things go wrong with the licensing of a delivery, and they fail
differently.

The loud one is contamination: something copyleft is linked in and the
obligation reaches everything it was linked into. `policy/licences.json` declares
what each part of the component is, by longest-prefix rule, and
`collect_licenses.py` refuses a copyleft component that names no permissive
alternative. libddk has exactly one: **libfdt**, which is
`GPL-2.0-or-later OR BSD-2-Clause` — dual licensed, the choice the recipient's,
and we elect BSD-2-Clause, as U-Boot, Zephyr and the Linux tooling do. The
election is declared, so it is a decision on the record rather than an
assumption. Nothing else in the component is copyleft; BoringSSL's OpenSSL and
SSLeay licences are permissive, BSD-style.

The declaration is not trusted on its own. A prefix rule with a catch-all root
will happily label a stranger that lands under `include/` as ours, so every
shipped text file is also read, and two things stop the export:

* an SPDX tag the declared expression does not account for. Seventeen files
  were labelled `BSD-3-Clause` while calling themselves MIT, Apache-2.0 or
  BSD-2-Clause — the LK and Trusty headers, linenoise, and three of Zircon's
  own. All permissive, none contaminating, and all of them wrong in a NOTICE
  the customer reads.
* copyleft text in a file whose component declares none. That is the only
  thing that catches an untagged file, which is what ACRN's ioctl header is.

The declaration follows provenance rather than destination. The export
manifest records where every file came from, and `zircon/system` accounts for
258 of them against garnet's 38; labelling all of them `nebula` because they
install under `include/` was the mistake behind most of the seventeen.

`zircon/LICENSE` is not used as a licence text. It is a pointer file — "copies
of which may be found in system/LICENSE" — and shipping it as the notice for
398 BSD-3-Clause files would hand the customer directions instead of a
licence.

A third check compares the copyright holders a component's files name against
the ones its licence text names. That is where the largest failure was: 304
files carried GoldenRiver's copyright under a text that named only the Fuchsia
Authors, because they are files GRT has modified and `zircon/system/LICENSE`
had never been told. Not a breach — each file keeps its own notice — but the
delivery was telling a reader that somebody else wrote what we did.

A holder the text does not name must either be added to it or declared as an
`embedded_holders` entry **with a reason**: boringssl keeps Sun's, Nokia's and
Amazon's notices in the files rather than in its LICENSE, libfdt keeps Kim
Phillips's, and Linux's `virtio_ring.h` keeps Rusty Russell's and IBM's under
the explicit BSD exception those headers carry. Silence is not an option; the
check names all nine and refuses until each is answered.

It is a presence test, not an equality one. It catches a holder nobody wrote
down. It does not catch a text that names the right holder and says the wrong
thing — `LICENSES/acrn.txt` described a file that was not in the component and
carried a two-clause BSD for three-clause files, and only reading it found
that.

The quiet failure is under-attribution, and it is the one that had actually
happened. BSD and MIT ask only that the notice travel with the code, and a
component assembled out of headers has no natural place to put one: **257 of
libddk's files carried no inline notice at all**, 212 of them the C library's,
and the component shipped with no `LICENSE`, `NOTICE` or `COPYING` file
anywhere in it. `collect_licenses.py` installs `LICENSES/<part>.txt` and a
`NOTICE` naming every part, its licence, any election, and how many files it
covers — and fails if a single file is claimed by no entry, because that is how
something arrives in a delivery with nobody having decided what it is.

This runs as part of the export, not after it. `materialize_ddk.py
--licenses` covers the staged tree before it writes the manifest, and the licence files are listed in `SDK_CONTENTS.json` like
anything else — an export that forgot them would otherwise look complete. A
component meant to ship should always set it.

Doing it at export rather than at trim is what found the second copyleft file.
`include/acrn/hsm_ioctl_defs.h` is Intel's ACRN ioctl definitions, dual
BSD/GPLv2 — "you may do so under either license" — and it carries no SPDX tag,
unlike the two files beside it. The trim had already dropped it, so scanning
the trimmed component alone never saw it. It is declared and elected
BSD-3-Clause now.

### The source halves have licences of their own

For a long time only the component did. The halves went out with no NOTICE,
no LICENSE and no line anywhere saying what their files are used under, while
four upstreams ship inside them:

| | licence | where |
| --- | --- | --- |
| Lua 5.3.3 | MIT | `garnet/public/lib/lua/src` |
| LuaBridge 2.9 | MIT | `garnet/public/lib/lua/LuaBridge` |
| ICU, the UTF routines fxl copied | ICU | `garnet/public/lib/fxl/third_party/icu` |
| littlefs 2.11 | BSD-3-Clause | `third_party/littlefs` |

Each half now carries `LICENSES/`, `LICENSES/FILES` and a `NOTICE`, generated
from `policy/source_licences.json` -- a declaration of its own, because a source
tree is laid out by where code lives and libddk by what a driver includes, and
one file answering both would be read as neither. One declaration serves both
halves: which files land in which half moves with the partition, and a
per-half declaration would need an entry moved with every driver that does.

Neither Lua nor LuaBridge ships a licence file. Lua's terms are at the end of
`lua.h`, and the text here is byte-identical to upstream's own at v5.3.3;
LuaBridge's are in its README, reproduced verbatim with the MIT text its SPDX
tags name. Both are in `PROVENANCE.json` with the checksum of the upstream
file as well as their own.

They are generated **before the mirror**, not at assembly. The mirror is the
half that ships, so anything not in it then is not in what MTK receive -- and
added afterwards, the next sync deletes it as a file the delivery does not
declare and the release after puts it back, churn in their history every time
for a file neither side edited.

`make_delivery.py` refuses a part without `LICENSES/FILES` and a `NOTICE`,
because the three travel separately and a half cannot count on the
component's being synced beside it. The merge leaves them out, since they
are generated from garnet and do not belong back in it -- except that garnet
has a `NOTICE` of its own at its root, which lands exactly where a generated
one would. Skipping the name unconditionally dropped it from the round trip,
silently; `LICENSES/FILES` is written by the same pass and nothing else, so
its presence is what tells the two apart.

Reading MTK's sources turned up one thing worth recording: twenty files under
`garnet/drivers/block/ufs` tag themselves `BSD-3-Clause` and then reproduce the
MIT permission notice below it. Both are permissive, and shipping the source
verbatim satisfies either, but the tag and the text disagree, and that is
MediaTek's to correct.

## The kernel ABI

libddk is released to MTK every so often; Zircon keeps moving in between. A
syscall inserted or reordered renumbers everything after it, a driver built
against the old numbers calls the wrong kernel entry, and the headers the
customer holds look exactly as they did. Nothing in the delivery said which
ABI it was built for, so nothing could tell that apart from a bug.

`export_abi.py` puts two files under `abi/`, and they are deliberately not one:

| file | answers |
| --- | --- |
| `ABI.fingerprint` | did the ABI move at all? |
| `SYSCALLS.json` | which syscall moved? |

Both are plaintext because withholding them would protect nothing. The names
and the full prototypes ship anyway — 440 of them in
`sysroot/include/zircon/syscalls/definitions.h`, 230 more as dynamic symbols
in `libzircon.so` — because a driver cannot call what it cannot declare. A
digest of what is already published hides nothing.

The fingerprint covers the ordinals as well as the declarations. Swapping two
syscalls' numbers without touching either declaration is exactly the change
that breaks a prebuilt library, and it leaves every per-syscall digest
identical — there is a test for it.

The numbers themselves are not in the delivery. `BUILD_INFO.json` names the
zircon revision, and `zircon/system/public/zircon/syscalls.abigen` at that
revision *is* the numbering, so "what did its number become" is a diff of
that file between two revisions. There used to be a third file,
`ORDINALS.enc` — the numbers and the abigen source, encrypted to a
certificate — with a private key kept off the release machine. The release
made a throwaway pair when given no certificate, two trees released through
one container shared the cache the pair lived in, and the second run
overwrote the first's: the first mt8678 delivery's record can no longer be
opened by anyone. A record the tree can answer without, sealed to a key that
has to be kept for ten years by a team that does not run a CI, was the wrong
trade, so it went, and `openssl` with it — nothing in the pipeline needs a
package the tree does not build.

## Whose work this is

Several parts of the component are forks, and their files carry both
copyrights, so a reader with only the licence texts cannot tell ours from
upstream's. A component may declare an `owner`, and the NOTICE then says
plainly which parts GRT holds copyright in — *alongside* the upstream authors,
not instead of them.

Within the delivery the copyright line is `The GoldenRiverTek Authors`: 315 of
the 322 files that carry one already used it, and the ten that did not have it
now do. The older lines are left where they are; a copyright notice is added
to, not rewritten.

The wider tree is not consistent — six spellings across four repositories,
including both `GoldenRiver Technology Co., Ltd.` and `GoldenRiver
Technologies Co., Ltd.`, which are different legal entities. Settling that is
a separate change and needs the entity named first.

## Which build this came out of

A customer reports something against "the libddk you sent in March". Without a
record, answering means guessing which build that was.

`BUILD_INFO.json` carries three things, because a timestamp on its own
identifies nothing:

* `revisions` — the commit each repository was at. This is the identity: it
  says what went in, and it can be checked out again. Only the repositories
  that contributed a file are listed; this tree holds a hundred checkouts and
  recording the Linux trees and vendor drops would bury the eight that matter.
* `source_date` — the newest of those commits, so two builds of the same
  source agree on it.
* `built_at` — when the export ran.
* `toolchain` — the CIPD stamp and release of the clang that produced the
  binaries. Not derivable from the revisions: the compiler is downloaded, so
  moving the pin moves nothing in git, and the delivered archives carry that
  clang's libc++ ABI namespace. Without this field, "the March libddk stopped
  linking" cannot be told apart from an ABI change.

`dirty` matters more than any of them. A component built from a tree with
uncommitted changes cannot be traced to anything, and finding that out while
chasing a customer's bug is late.

### The manifest is sealed last

`SDK_CONTENTS.json` has to describe the component that is actually there, and
in a pipeline it does not: the trim writes the manifest, then writes its own
report, and the licence step then rewrites `LICENSES/` for what is left. A
manifest written in the middle of that claimed two licence texts whose files
the trim had removed, and missed the report entirely.

`materialize_ddk.py --seal` runs last and rewrites the manifest from the tree,
then fails if the two still disagree in either direction. It was found by an
end-to-end run rather than by any of the checks: each of them was asking
whether the thing it owned was right, and none owned the boundary between two
steps.

`built_at` is the one field that moves between two builds of identical source,
so the file says where it came from: set `SOURCE_DATE_EPOCH` and it is pinned
and the export is byte-reproducible again; leave it and it is the clock, and
`built_at_source` says so.

### Diagnosing a report from the field

A customer reports a driver misbehaving against a libddk released months ago.
The first question is always whether the kernel ABI moved under it.

    python3 build/ddk/tools/ddk_version.py describe \
        --component /path/to/their/libddk --out theirs.json
    python3 build/ddk/tools/ddk_version.py describe \
        --tree . --abigen zircon/system/public/zircon/syscalls.abigen \
        --ordinals out/build-zircon/build-$PROJECT/gen/global/include/zircon/zx-syscall-numbers.h \
        --like /path/to/their/libddk --out ours.json
    python3 build/ddk/tools/ddk_version.py compare theirs.json ours.json

From the root of the checkout, which `--tree .` assumes.

Three answers, in the order somebody under time pressure wants them:

* **identical** — a driver built against one runs against the other. Look
  elsewhere.
* **moved, and here is the syscall** — a declaration changed, and the report
  names it. Usually the end of the search.
* **moved, and no declaration changed** — the numbers were reshuffled, which
  is the case that breaks a prebuilt library while leaving every header
  looking untouched. This is the one time the private key is needed, and the
  report says so and gives the command.

`--like` makes the tree record the same repositories the delivery does. Without
it a description that names one repository compares against one that names
eight, and seven non-differences bury the real one. `compare` only calls a
revision moved when both sides name it; the rest are reported as unrecorded,
because that is what they are.

None of this needs the key. That is why the digests and the fingerprint ship
in the clear.

### Which licence covers this one file

`NOTICE` answers what is in the delivery and under what. It does not answer
the question a customer actually arrives with, which is about one path out of
a thousand — and counting files per component leaves them to re-derive the
prefix rules from outside.

`LICENSES/FILES` is one line per delivered file, sorted by path, so `grep`
answers it:

    include/ddk/driver.h             zircon    BSD-3-Clause
    lib/libfdt.a                     libfdt    GPL-2.0-or-later OR BSD-2-Clause (elected BSD-2-Clause)
    toolchain/lib/host/libxml2.so.2  libxml2   MIT
    sysroot/include/stdio.h          musl      MIT

The election is shown beside the expression: somebody asking about `libfdt.a`
has to see that the permissive half was taken, not only that the file is dual
licensed.

`python3 build/ddk/tools/collect_licenses.py --component <part> --verify`
checks one part of a finished delivery -- libddk, grt_ddk or mtk_ddk, each
carries its own table -- against that table and **reads no declaration**. The declaration is what
produced the delivery; running the same input through the same rules would
agree with itself whatever it did. This uses only what is inside the artefact,
which is what an auditor holds and what a customer can be handed to repeat.

Three ways it fails, and the first is why it exists: a file shipped that no
row names, a row naming a file the delivery does not have, and a component
named against files whose licence text is not beside them.

### The report stays here

The trim writes what it kept and what it dropped, and that report does **not**
go into the component. `dropped_files` names all 337 — the ACRN header held
back for its licence, every `ddk/protocol/` header the delivery does not
include, five archives — which is a map of what is there and was withheld,
published by the step whose whole purpose is to withhold it.

`--report` is required and points outside the component. A trim nobody can
review is a set of deletions nobody agreed to; the record belongs where the
reviewing happens, which is here and not at the customer.

## What ships

Everything above produces a piece. `make_delivery.py` is where they become one
directory, and the only place that says what a delivery contains:

    output/
      libddk/aries_v1/    what both halves need, the compiler inside it
      grt_ddk/garnet/     one source half, the Makefile that builds it, and
                          libddk/ -- the part of the component only this
                          half needs
      mtk_ddk/aries_v1/   the other, the same way but without a component
                          of its own

and each of the three carries `RELEASE`, `NOTICE` and `LICENSES/` of its own,
since each travels without the other two.

Each part sits one level down, at the name of its repo project on MTK's side
-- `yocto/src/hypervisor/libddk/aries_v1` and so on -- so that `output/` is
`src/hypervisor/` as their manifest checks it out, directory for directory.
Those names are nothing the build knows, so they are policy: the `layout`
entry of `delivery.json`, read once by `run_release.sh` and handed to every
tool that reads or writes a delivery as `--layout`. `layout.py` is the one
place that turns a part's name into its directory. Without a layout a part
sits at its own name, which is what the tests build.

The halves still build beside each other. The recipe fetches each part flat
into its WORKDIR and a half finds the component as `../libddk`, so the two
release steps that build a delivery -- from a copy of `output/`, and from
clones of its repositories -- lay the parts out flat the same way first.

libddk is split because the two halves do not need the same thing and do not
go to the same people. MTK's half reads 22 headers and links 5 libraries out
of the 309 and 38 the component used to carry; GRT's half reads the rest.
What MTK's half needs is what libddk ships, and a customer receives that and
mtk_ddk and nothing else -- so GRT's headers and archives are not in their
hands at all. What only GRT's half needs travels inside grt_ddk, as a
libddk/ of its own, and the Makefile there adds it to the include and link
paths when it is present.

Which side a file falls on is measured, not listed: the half that reads a
header or links a library is the half that gets it, read from the same
module descriptions the delivery ships. A file neither half was measured
needing stays out of the shared component, because the smaller it is the
less leaves here.

Three parts, all of them directories, and nothing else at the top or in the
directory the layout puts a part under -- checked rather than intended. MTK
keep every part under `repo`, which manages projects and not loose files: the
Makefile that used to sit beside the parts belonged to no project, so it ships
inside each half instead. The entry most likely to turn up wrongly is a
`build/` left by testing the very layout this writes. A customer's delivery is
the same minus one half, which arrives as the binaries in `libddk/lib`.

A half is built from inside itself, which is also how a customer with one half
builds:

    make -C mtk_ddk           # that half, against ../libddk
    make -C grt_ddk           # the other, against ../libddk and ./libddk

`make list` names the half beside each module, so output from two halves is
never read as one list.

Symlinks are allowed only where they stay inside the delivery. The compiler
ships four on purpose — `clang++` and `clang-7.0` beside `clang`, `lld` beside
`ld.lld` — and copying them would add 234 MB of the same bytes. One that is
absolute, or that resolves outside once the tree has moved, is refused: that
is a path back to the machine that built the delivery, which is what the
sandbox's old `toolchain/` link was and why it is gone.

### Three parts, one release

MTK manage every part under `repo`, so the three parts of a delivery travel
separately and nothing in git ties them together. `repo sync` can bring the
sources forward and leave the component where it was; the build then links
against the previous ABI and fails somewhere that has nothing to do with it.
That is what a single repository used to do for free.

`make_delivery.py` stamps the same `RELEASE` into all three — a digest over
the contents, dated — and the Makefile checks them before compiling anything.
One file different anywhere gives a different id, so parts from two
deliveries cannot pass for one.

It does **not** promise two builds of the same tree the same id, and it was
written down here as though it did. `BUILD_INFO.json` is part of the digest
and carries the build's date. (For a while `abi/ORDINALS.enc` was too, and it
was encrypted with a fresh content key every time, so no two builds could
match even without the date.) The id tells apart parts that were not
delivered together; to recognise a rebuild, compare the files.

All of them or none of them. A sandbox assembled here carries no `RELEASE`
and is fine; a delivery where one part lost its is not. Treating a missing
file as "nothing to compare" is how a mismatched part gets through: `$(sort)`
drops the empty string and the two that are left agree with each other.

### Building it somewhere else

`make install DESTDIR=... [PREFIX=/ddk]` installs to the same layout
`combine_builds.py` gives a binary bundle, so a half delivered as sources and
a half delivered as binaries land in the same places and a consumer's
packaging does not have to know which it got. `make list-install` prints
those paths without writing anything, which is what a recipe's `FILES` line
needs.

`CFLAGS`, `CXXFLAGS` and `LDFLAGS` are `override`, not `?=`. They carry the
ABI — the target triple, the sysroot, `-fno-rtti` — and they are the
delivery's, not the caller's. This is not hypothetical: bitbake exports
`CFLAGS` and `LDFLAGS`, so under Yocto `?=` silently dropped
`--target=aarch64-fuchsia` and compiled for the host, and the failure then
arrived as a mangling mismatch at link time pointing at nothing.
`EXTRA_CFLAGS` and friends are the way in — you can add to the line, you
cannot replace it.

The halves compile at the product's `-Og` and link with `--strip-all`, so
what a half installs is the size of what the product ships. Neither was
there at first: a module description carries a target's own flags and not
the toolchain's, so the halves built at `-O0` with their symbol tables in,
and came out 1.8× the product's binaries — on mt8668, 23 MB more bootfs in a
128 MB hypervisor, and the system ran out of memory bringing the guest up.
`make STRIP_FLAGS=` keeps the symbols; `EXTRA_CFLAGS=-O0` wins over `-Og`.

### Handing it over

MTK keep every part under `repo`, so a delivery leaves here as three
repositories and a note:

    libddk                 a git repository: the component, compiler included
    grt_ddk                a git repository, one commit per release, tagged
    mtk_ddk                a git repository, our commits replayed into it
    RELEASE.txt            the id and each part's revision

The component was a tar for a while, unpacked over the workspace root, because
185 MB of binaries in a `repo sync` is a real cost. MTK asked for a directory
instead: one way to fetch a delivery, one tool, and a component that arrives
with the halves rather than beside them.

`publish_delivery.py` makes each repository **equal** to the part that
shipped, commits what changed, and tags it. Equal and not merged: a
repository that is nearly the delivery is a repository nobody checked. The
same tag on different content is refused — a release is made once, and
publishing it again over something else is how two things end up with one
name. A part of the delivery that would not be published at all is refused
too: one left behind is one they cannot fetch.

The note is the part that has to be right. Nothing in git ties three
repositories together, so the id and every revision are written down in one
place, and every part carries the id as well.

### Deploying it

Where MTK actually build from is the Gerrit the yocto layers live on, one
project per part -- `yocto/src/hypervisor/libddk/aries_v1` and the two halves
beside it, on the branch and under the topic `delivery.json`'s `deploy` entry
names. `deploy_delivery.py` brings each project up to the delivery, and
`gr-nebula.py ddk-deploy` runs it on the last release:

    gr-nebula.py ddk-deploy         # works out what is new; patches in out/ddk/deploy/
    gr-nebula.py ddk-deploy --push  # and pushes it for review, printing the change URLs

It reads nothing local. Each project is fetched from Gerrit into `--work`,
and the base a delivery lands on is the tip of what is already up for review
under the topic -- a release goes on top of the last one whether or not that
has merged. What the base already holds is read from git, never from tags:

- The release it is at, from its `RELEASE` -- every part carries one now, the
  halves included, because a change pushed for review carries no tag. A
  release that differs from it in nothing but the stamp, the build date and
  the checksums that follow -- the same sources built again -- is the same
  delivery, and reported as up to date.
- The RD commits it holds. A replayed commit names its garnet source in a
  `Mirrored-From` trailer, so the newest source the base names is the
  boundary and everything the delivery's repository has after it is new. A
  base imported whole names no sources, but libddk's build record at the same
  release names the garnet revision it was built from, and the garnet
  checkout can say which RD commits came after that. Neither known, the tree
  goes up whole as one `ddk: import` change, the way the first deliveries
  did.

What goes up is only the RD's work and one change for the release:

- The RD commits the base does not hold, as they are: the mirror replayed them
  with their CR, ONES id and trailers intact, so nothing is written by hand.
  One change each, the RD as author and as reviewer.
- Then one change, `ddk: release <id>`, for what the build generated on top
  -- module descriptions, the manifest, licence records, the stamp -- under
  the ids of the newest RD commit in the series, which is the work it was
  made for. libddk, being built output with no commits of anybody's, is that
  one change alone, under the newest RD work in the halves.
- Every change gets a Change-Id derived from the project and the source
  commit, so deploying the same release twice updates the changes rather than
  opening more.

Nothing depends on the delivery repository's continuity either: a repository
rebuilt from scratch replays the same sources with the same trailers, and the
deployment reads the same.

Either way the tree that results is compared with the delivery byte for byte
before anything is pushed; a difference stops it. A push Gerrit refuses --
the branch moved under an open change, a check it will not pass -- is not
retried and never forced: the refusal is printed with the RD's address and the
patches stay in `out/ddk/deploy/<part>/`, for the RD to take up. Verified is
still a person's to give.

The mirror's own starting point follows from this. `delivery.json`'s
`mirror_since` is where a half's mirror begins when there is no last release
to continue from: everything before it is already deployed, imported whole,
so a mirror reaching further back would only carry history nobody pushes. It
was a fixed date in the script -- 2025-01-01, 330 commits -- and is policy
now, at the date of the first import.

## The compiler

libddk ships headers and binaries. It does not ship the thing that turns one
into the other, and for a long time that was a gap nobody had written down.

It is not a convenience. The delivered archives were produced by one specific
clang, and their C++ symbols carry that clang's libc++ ABI namespace:

| | |
| --- | --- |
| symbols mangled `std::__2::` in the delivery | 4113 |
| of those, needing the consumer's own libc++ to resolve | 46 |
| namespace an upstream clang uses instead | `std::__1::` |

Fuchsia's clang 7 builds libc++ with `_LIBCPP_ABI_VERSION 2`; upstream is 1.
A consumer building with a different compiler gets undefined references
across the whole delivery — loudly, at link time, which is the good case.

It ships as `libddk/toolchain/`, inside the component rather than beside it:
one thing to sync, one NOTICE, one manifest, and no way to end up with a
component and a compiler out of step. `selftest.mk` looks there first and
falls back to a separately supplied one, which is what our own pipeline uses —
it has to build the delivery before it can put the compiler in it.

`export_toolchain.py` cuts the 1.4 GB prebuilt down to what a DDK build
actually runs:

| | prebuilt | export |
| --- | --- | --- |
| size | 1.4 GB | 158 MB |
| files | 840 | 490 |

Most of that is not trimming, it is deduplication: the package carries five
*copies* of the 78 MB clang driver and three of lld, where the export keeps
one of each and symlinks the names the driver answers to. The rest is
`clang-tidy`, `clangd`, `opt`, `llc`, `wasm-ld` and two dozen other tools no
rule in `selftest.mk` invokes, plus an x86_64 runtime nothing here targets.

### Declared, then proved

The keep set is declared in the script rather than measured from a build, and
that is the one place this differs from the trim. A compiler decides what to
run from `argv[0]` and from its own installation layout, so "what did this
build execute" does not answer "what will the next one execute": clang forks
`ld.lld` by looking beside itself, and a driver that took the integrated
assembler path today may fork one tomorrow.

So `run_export_toolchain.sh` rebuilds the *entire* DDK against the export,
sealed, and that is what turns the declaration into evidence. The sanitizer
runtimes are the one place measurement does decide: they are two thirds of the
target runtime by size and no link trace in the sealed build touches them, so
they are left out unless `--with-sanitizers` asks for them.

### Where it gets installed, and why only there

`run_trim.sh --install-toolchain` copies it in **after** the trim and its
rebuild, and **before** the licences. Both halves of that are load-bearing.

Earlier and the trim destroys it twice over: `plan()` drops whatever no build
measured, and no build measures the compiler it ran, so 490 files would be
deleted as unreachable; and `redact_source_paths` rewrites strings in place
inside every `.a` and `.so` it finds, five of which are `libc++.so`,
`libc++abi.so`, `libunwind.so`, `libc++experimental.a` and
`libclang_rt.builtins-aarch64.a`. A compiler with rewritten strings is one
nobody can debug and possibly one that does not run.

Later and the licence step sees 490 unclaimed files — or worse, does not, and
they fall through to the root entry, which says GoldenRiver owns them.

### Two things the gates had to learn

**Host binaries are verified, not stripped.** Nineteen of the twenty-two ELF
files in the export carry DWARF, and all nineteen are aarch64, which the
pinned `aarch64-elf-objcopy` reads. The other three are the host tools — and
that objcopy cannot open an x86-64 ELF at all. They ship stripped already, so
there is nothing to do; `check_stripped` still reads every one of them, which
is the only thing that would notice if a future prebuilt does not.

**The source-path gate has an allow list.** `bin/clang` carries fourteen
strings the redaction regex matches — `sys/types.h`, `arpa/inet.h`,
`objc/runtime.h` — and every one is the compiler knowing what a system header
is called, not a build path leaking. The list is exact names only: a path
merely *ending* in `sys/types.h` still fails.

### It carries its own libraries

`ld.lld` is dynamically linked against `libxml2.so.2` — for COFF manifests,
which no link here touches — and on a host whose libxml2 has moved on to
another soname it cannot start at all. Recording that in
`TOOLCHAIN_INFO.json` was not enough: nobody reads a JSON file before typing
`make`, and the user who hit it had two hundred files compile and then a
loader error naming a library nobody asked for.

So the export carries them. `libxml2.so.2`, `libz.so.1` and `liblzma.so.5`
land in `toolchain/lib/host`, 1.6 MB in all, and the Makefile prepends that
directory to `LD_LIBRARY_PATH`.

They come from `--host-libs`, which names pinned directories in the checkout —
the sysroot `buildtools` ships — and never from the machine doing the export.
A delivery assembled out of whatever happened to be installed is one that
works here and nowhere else, and there would be nothing to say which version
went in. A library the pinned tree does not have is an error, not a fallback.

What is left is glibc and `libgcc_s`: they have to match the loader already
running, and shipping them would be shipping a second C library.
`host_requirements` is now that list and nothing more. The floor is
glibc 2.15.

### Licences

The prebuilt contains **no LICENSE file at all**, in any directory, while its
headers say

    This file is distributed under the University of Illinois Open Source
    License. See LICENSE.TXT for details.

— naming a file the package does not have. Redistributing those binaries
without reproducing the notice is a compliance gap inherited from that
package, so the five upstream texts were fetched once from `llvmorg-7.0.0`
and live in `policy/licenses/`. Five and not one: the NCSA
body is shared between LLVM, clang, lld, libc++ and compiler-rt, and the
copyright line is not — 2003-2018 Illinois, 2007-2018 Illinois, and three
different spans of "the contributors listed in CREDITS.TXT". `PROVENANCE.json`
records the tag, URL and SHA-256 of each, and every export verifies them, so a
release needs neither the network nor a manual fetch and an edited text fails
with the URL to get the right one back.

They are declared in `policy/licences.json` under `toolchain/` prefixes, so the
same four gates cover the compiler and the delivery in one pass and the
component has one `NOTICE`. The `toolchain` prefix is a catch-all on purpose:
nothing under it may reach the root entry, which says GoldenRiver owns what it
claims. The version and the triple are written out rather than templated — a
toolchain bump *should* make the unclaimed-file gate fire, because that is the
moment somebody has to read these licences again.

Declaring it correctly took two attempts. 107 of the 108 headers under
`lib/clang/<version>/include` are MIT with an individual author's copyright —
Chris Lattner, Eli Friedman, Howard Hinnant, Chandler Carruth — and only
`opencl-c.h` is NCSA. The attribution gate is what said so. Each of those
headers states its full permission notice inline, which is why they are
declared as `embedded_holders` rather than added to a licence text they did
not write.

`read_declaration` verifies `PROVENANCE.json` for every licence directory a
declaration points at, before anything is written.

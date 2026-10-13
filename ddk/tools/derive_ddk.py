#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Derives the DDK component and the delivered source set from the build map.

What `ddk_sdk()` and `ddk_driver_sources()` used to declare in garnet --
which headers libddk publishes and where, which archives it carries, which
files make up the source set -- follows from three things already on disk:
the build map, the selection `build_map.py select` made from the policy, and
the product build directory holding the objects.

  component  plan libddk: headers, libraries, sysroot
  sources    plan the source set the halves are split from

Each writes a portable SDK manifest and a layout, in the form
materialize_ddk.py already reads, so everything after it -- materialising,
sealing, licensing, trimming -- is unchanged.  The layout is generated rather
than written: what used to be its job, keeping the published structure from
moving, belongs to the lock now.

The rules, each from a file that landed wrongly:

* **libddk's headers are what the delivery reads, and what those include.**
  Taking library include directories whole carried headers nobody delivered
  reads under licences the declaration was never asked about, and the licence
  gate at export refused them.  The includes are followed as a compiler
  would, a quoted one beside the file doing it, and only where the build read
  the result or the last release shipped it: protobuf's atomicops for other
  architectures were shipped and stay, rapidjson's MSVC-only msinttypes were
  not and are not dragged in.  Includes inside comments are not includes.
* **A header is installed at every spelling the delivered code uses.**
  rapidjson is <rapidjson/document.h> in six places and
  "third_party/rapidjson/rapidjson/document.h" in eleven.
* **A header one half reads from the other half is published by libddk**, at
  its path in the tree: MTK's I2C driver reads GRT's grt-i2c.h, and a half
  that has to be built alone cannot reach into the other for it.
* **What the last release shipped in a half stays in it**, and libddk does
  not publish it again unless the other half reads it.
* **The sysroot is the product's, whole**, less what is neither a header nor
  a binary -- a Clang module map, a Rust binding.
* **A library is the product's own output where it has one, and the shared
  toolchain's where it builds on both**, so that a driver can link it too.
  A source set has no archive, and gets one made from its objects.
"""

from __future__ import print_function

import argparse
import io
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_map  # noqa: E402
import partition  # noqa: E402

FORMAT_VERSION = 1

PUBLISHED_HEADER_SUFFIXES = ('.h', '.hh', '.hpp', '.hxx', '.inc', '.inl',
                             '.ipp', '.def')
BINARY_SUFFIXES = ('.a', '.so', '.o')
INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)

# The C headers libc++ wraps and then falls back to with #include_next.
#
# <cwchar> is the toolchain's, and its first line reaches past itself for the
# sysroot's wchar.h.  Nothing in any source or sysroot header spells that, so
# the walk and the closure below cannot see it: a half that compiles C++ at
# all needs these, whoever was measured reading them.  They are upstream musl
# and travel to both halves.
LIBCXX_C_HEADERS = (
    'complex.h', 'ctype.h', 'errno.h', 'fenv.h', 'float.h', 'inttypes.h',
    'limits.h', 'locale.h', 'math.h', 'setjmp.h', 'stdbool.h', 'stddef.h',
    'stdint.h', 'stdio.h', 'stdlib.h', 'string.h', 'tgmath.h', 'uchar.h',
    'wchar.h', 'wctype.h',
)

SHARED_HINT = '-shared'


class DeriveError(Exception):
    """A component or source set that cannot be planned."""


def _fail(message):
    raise DeriveError(message)


def _read_json(path):
    with io.open(path, encoding='utf-8') as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# where things are


class Tree(object):
    """The checkout and the build directory, and how the map spells both."""

    def __init__(self, root, build_dir):
        self.root = os.path.abspath(root)
        self.build_dir = os.path.abspath(build_dir)
        self.build_rel = os.path.relpath(self.build_dir,
                                         self.root).replace(os.sep, '/')

    def spelled(self, path):
        """A map path -- root-relative, `$build/...`, or absolute -- as a
        root-relative one, or an absolute one outside the root."""
        if path.startswith(build_map.BUILD_PLACEHOLDER):
            rest = path[len(build_map.BUILD_PLACEHOLDER):].lstrip('/')
            path = self.build_rel + ('/' + rest if rest else '')
        if os.path.isabs(path):
            relative = os.path.relpath(path, self.root).replace(os.sep, '/')
            return path if relative.startswith('..') else relative
        return path.rstrip('/')

    def absolute(self, path):
        spelled = self.spelled(path)
        return spelled if os.path.isabs(spelled) else \
            os.path.join(self.root, spelled)

    def locator(self, path):
        """The portable manifest's spelling for a file."""
        spelled = self.spelled(path)
        if os.path.isabs(spelled):
            _fail('%s is outside the checkout; a component cannot carry it'
                  % spelled)
        if spelled == self.build_rel or spelled.startswith(self.build_rel +
                                                           '/'):
            return 'out://' + spelled[len(self.build_rel):].lstrip('/')
        return 'source://' + spelled


# ---------------------------------------------------------------------------
# what the delivery is made of


def members(the_map, selection):
    """module name -> every variant of the module's target and what it folds.

    A module is one target per toolchain, and whatever source sets and
    libraries selection folded into it.
    """
    targets = the_map['targets']
    folded = set(selection.get('folded', []))
    by_plain = {}
    for label in targets:
        if not build_map.is_host(label, the_map.get('default_toolchain', '')):
            by_plain.setdefault(build_map.strip_toolchain(label), []).append(
                label)
    owners = {}
    for label, name in selection['modules'].items():
        owners[build_map.strip_toolchain(label)] = name
    result = dict((name, set()) for name in set(owners.values()))
    for plain, name in owners.items():
        result[name].update(by_plain.get(plain, []))
    # A folded target belongs to whichever module reaches it; drivers that
    # share one both carry it.  So does a source set beside the module with
    # nothing to compile: luabridge is lua's headers, listed as a source set
    # of its own, and selection never saw it because there was nothing in it
    # to build.
    for name, labels in result.items():
        home = set(targets[l]['dir'] for l in labels if l in targets)
        stack = list(labels)
        seen = set(labels)
        while stack:
            label = stack.pop()
            for dependency in targets.get(label, {}).get('deps', []):
                plain = build_map.strip_toolchain(dependency)
                if dependency in seen or plain in owners:
                    continue
                entry = targets.get(dependency)
                header_only = entry is not None and \
                    entry['type'] == 'source_set' and \
                    not build_map.compiles(entry) and entry['dir'] in home
                if plain in folded or header_only:
                    seen.add(dependency)
                    labels.add(dependency)
                    stack.append(dependency)
    return result


def module_half(name, labels, the_map, rules, scope='garnet'):
    directory = the_map['targets'][sorted(labels)[0]]['dir']
    return path_half(directory, rules, scope)


def path_half(path, rules, scope='garnet'):
    if path == scope or path.startswith(scope + '/'):
        part, _ = partition.owner_of(path[len(scope):].lstrip('/'), rules)
        return part
    return rules['default_part']


def include_dirs_of(entry, tree):
    """Every include directory a target compiles with, root-relative."""
    found = []
    for group in entry.get('include_dirs', []):
        for value in group['values']:
            spelled = tree.spelled(value)
            if spelled not in found:
                found.append(spelled)
    return found


# ---------------------------------------------------------------------------
# the component


BLOCK_COMMENT = re.compile(r'/\*.*?\*/', re.S)
LINE_COMMENT = re.compile(r'//[^\n]*')


def _includes_of(path, tree):
    """The #include spellings a file really has -- not the ones in its
    comments.  rapidjson's reader.h shows how to use it inside a doc comment,
    #include "rapidjson/reader.h" and all, and taking that for an include
    published rapidjson a second time at a path nothing compiles."""
    try:
        with io.open(tree.absolute(path), encoding='utf-8',
                     errors='replace') as handle:
            text = handle.read()
    except (IOError, OSError):
        return []
    text = BLOCK_COMMENT.sub(' ', text)
    text = LINE_COMMENT.sub('', text)
    return INCLUDE.findall(text)


def read_module_libs(directory):
    """half -> the libraries its module descriptions name.

    Read from the .ddk.mk files build_map.py has already written rather than
    worked out again here.  They are what the delivery ships and what the
    standalone build links, so a half is given exactly the libraries its own
    build asks for: pretty and zx are in the link closure and in neither
    description, and neither half should receive them.
    """
    wanted = {}
    if not directory or not os.path.isdir(directory):
        return wanted
    for current, dirs, names in os.walk(directory):
        dirs.sort()
        for name in sorted(names):
            if not name.endswith('.ddk.mk'):
                continue
            owner, libs = None, []
            with io.open(os.path.join(current, name),
                         encoding='utf-8') as handle:
                for line in handle:
                    if line.startswith('MODULE_OWNER'):
                        owner = line.split(':=', 1)[1].strip()
                    elif line.startswith('MODULE_LIBS'):
                        libs = line.split(':=', 1)[1].split()
            if owner:
                wanted.setdefault(owner, set()).update(libs)
    return wanted


class _Context(object):
    """What every phase reads and no phase changes."""

    def __init__(self, the_map, selection, rules, tree, in_halves,
                 keep_headers, released_files, module_libs):
        self.the_map = the_map
        self.targets = the_map['targets']
        self.selection = selection
        self.rules = rules
        self.tree = tree
        self.in_halves = set(in_halves)
        self.keep_headers = keep_headers
        self.released_files = released_files
        self.module_libs = module_libs
        self.modules = members(the_map, selection)
        self.half_of_module = dict(
            (name, module_half(name, labels, the_map, rules))
            for name, labels in self.modules.items())
        # The parts the rules declare, which is what "both halves" means here.
        self.both = frozenset([rules['default_part']]
                              + [r['part'] for r in rules.get('rules', [])])
        self.binary = selection.get('archives', {})
        self.every_label = sorted(set(l for ls in self.modules.values()
                                      for l in ls))
        self.sysroot = _sysroot_of(the_map, tree)


def _sysroot_of(the_map, tree):
    """The sysroot the product compiled against, as the checkout spells it."""
    sysroot = None
    for entry in the_map['targets'].values():
        for group in entry.get('cflags', []):
            for flag in group['values']:
                if flag.startswith('--sysroot='):
                    sysroot = tree.spelled(flag[len('--sysroot='):])
        if sysroot:
            break
    if sysroot is None:
        _fail('no --sysroot in any target: nothing says which C library the '
              'delivery was built against')
    return sysroot


class _Plan(object):
    """What the component carries, and why each file is in it."""

    def __init__(self):
        self.files = {}
        self.why_of = {}
        self.wants = {}
        self.evidence = set()
        self.reasons = {}

    def put(self, destination, source, why, read=False, halves=()):
        """Claim a destination.

        Two include directories can both hold trusty_unittest.h.  A compiler
        takes the one earlier in its search path, and so does this: the
        first directory claiming a name keeps it -- unless a module actually
        read the other, which is better evidence than any order.
        """
        destination = os.path.normpath(destination).replace(os.sep, '/')
        if destination in self.files and self.files[destination] != source:
            if destination in self.evidence or not read:
                self.reasons.setdefault('shadowed', 0)
                self.reasons['shadowed'] += 1
                return
        self.files[destination] = source
        self.why_of[destination] = why
        if halves:
            self.wants.setdefault(destination, set()).update(halves)
        if read:
            self.evidence.add(destination)
        self.reasons.setdefault(why, 0)
        self.reasons[why] += 1

    def want(self, destination, halves):
        if halves:
            self.wants.setdefault(destination, set()).update(halves)


def _carry_sysroot(ctx, plan):
    """The sysroot: its libraries whole, its headers as they are read.

    Taking the headers whole shipped 312 of them where 119 are read, and
    what rode along was not only dead weight: zircon/nebula/platform_abi.h
    and the grt-*.h device headers are GRT's own ABI, and the sysroot
    handed them to both halves however carefully the rest of the delivery
    kept them apart.  So a header travels because a half read it, like
    every other header here, and the closure below follows what it
    includes.  The libraries stay whole: a consumer's link line may name
    -lm or -lrt though ours did not, and Scrt1.o is how anything links at
    all.

    Returns every file the sysroot has, which is what says whether it
    already answers a spelling.
    """
    base = ctx.tree.absolute(ctx.sysroot)
    found = {}
    for current, dirs, names in os.walk(base):
        dirs.sort()
        for name in sorted(names):
            relative = os.path.relpath(os.path.join(current, name),
                                       base).replace(os.sep, '/')
            if name.endswith(PUBLISHED_HEADER_SUFFIXES + BINARY_SUFFIXES) or \
                    (relative.startswith('lib/') and '.' not in name):
                found['sysroot/' + relative] = ctx.sysroot + '/' + relative
                if relative.startswith('lib/'):
                    plan.put('sysroot/' + relative,
                             ctx.sysroot + '/' + relative,
                             'sysroot', halves=ctx.both)
    return found


def _library_include_dirs(ctx):
    """Where a binary library's own headers are, in search order."""
    closed_prefixes = build_map.CLOSED_PREFIXES
    seen = {}
    order = []
    for entry in [ctx.targets[l] for n in sorted(ctx.modules)
                  for l in sorted(ctx.modules[n])]:
        for group in entry.get('include_dirs', []):
            origin = group.get('from') or ''
            origin_dir = build_map.label_dir(origin) if origin else ''
            belongs = origin.startswith(closed_prefixes) or any(
                build_map.label_dir(b) == origin_dir for b in ctx.binary)
            if not belongs:
                continue
            for value in group['values']:
                spelled = ctx.tree.spelled(value)
                if spelled and not spelled.startswith(ctx.tree.build_rel) \
                        and spelled not in seen:
                    seen[spelled] = origin
                    order.append(spelled)
    return order


def _carry_trim_policy_keeps(ctx, plan, order):
    """The trim policy's entry points, which nothing in the delivery reads.

    Kept as the places a published name can be found, and not taken whole.
    A whole include directory carried headers nobody delivered reads, under
    licences the declaration was never asked about, and the licence gate at
    export was right to refuse them.
    """
    kept = []
    for relative in ctx.keep_headers:
        for directory in order:
            candidate = directory + '/' + relative
            if os.path.isfile(ctx.tree.absolute(candidate)):
                plan.put('include/' + relative, candidate,
                         'kept by the trim policy', True, halves=ctx.both)
                kept.append(candidate)
                break
    return kept


def _readers_of(ctx):
    """header -> the (label, module) pairs that read it."""
    readers = {}
    for name, labels in ctx.modules.items():
        for label in labels:
            for header in ctx.targets[label]['headers']:
                spelled = ctx.tree.spelled(header)
                readers.setdefault(spelled, set()).add((label, name))
    return readers


def _spellings_of(ctx):
    """How the delivered code spells each header it reads.

    That, and not the include directory, is where a header has to be:
    rapidjson is included as <rapidjson/document.h> in six places and as
    "third_party/rapidjson/rapidjson/document.h" in eleven, and a consumer
    compiling either needs a file at that path.  A quoted include relative
    to the file doing it needs nothing from libddk.
    """
    spellings = {}
    spelled_by = {}
    quoted = set()
    for name, labels in ctx.modules.items():
        for label in labels:
            entry = ctx.targets[label]
            directories = include_dirs_of(entry, ctx.tree)
            read = set(ctx.tree.spelled(h) for h in entry['headers'])
            texts = [ctx.tree.spelled(x) for x in entry['sources']] \
                + sorted(read)
            for text in texts:
                if build_map.classify(text) == 'toolchain':
                    continue
                for spelling in _includes_of(text, ctx.tree):
                    spelling = os.path.normpath(spelling).replace(os.sep, '/')
                    for header in read:
                        if header != spelling and \
                                not header.endswith('/' + spelling):
                            continue
                        prefix = header[:len(header) - len(spelling)].rstrip(
                            '/')
                        if prefix == os.path.dirname(text) and \
                                prefix not in directories:
                            quoted.add(header)
                            continue
                        if prefix in directories or prefix == '':
                            spellings.setdefault(header, set()).add(spelling)
                            spelled_by.setdefault(header, {}).setdefault(
                                name, set()).add(spelling)
    return spellings, spelled_by, quoted


def _install_path(ctx, header, labels):
    """Where a header with no spelling of its own goes: under the longest
    include directory that has it."""
    best = None
    for label in labels:
        for directory in include_dirs_of(ctx.targets[label], ctx.tree):
            prefix = directory + '/' if directory else ''
            if header.startswith(prefix) and (
                    best is None or len(directory) > len(best)):
                best = directory
    if best is None:
        return None
    return header[len(best) + 1:] if best else header


def _carry_read_headers(ctx, plan, readers, spellings, spelled_by, quoted,
                        sysroot_all):
    """What modules read that neither half has, where the code spells it."""
    pending = []
    for header, who in sorted(readers.items()):
        # In a half, and read only from that half: the half has it.  Read
        # from the other half as well, and libddk publishes it besides --
        # virtio_rpmb_protocol.h is MTK's source and GRT's machina includes
        # it.
        if header in ctx.in_halves and all(
                ctx.half_of_module[n] == path_half(header, ctx.rules)
                for _, n in who):
            continue
        if os.path.isabs(header) or header.startswith(
                build_map.TOOLCHAIN_PREFIXES):
            continue
        if ctx.sysroot and header.startswith(ctx.sysroot + '/'):
            # Read from the sysroot: it travels with the halves that read it.
            relative = header[len(ctx.sysroot) + 1:]
            if relative.endswith(PUBLISHED_HEADER_SUFFIXES):
                plan.put('sysroot/' + relative, header, 'sysroot', True,
                         halves=set(ctx.half_of_module[n] for _, n in who))
            continue
        if header.startswith('out/') and not header.startswith(
                ctx.tree.build_rel + '/'):
            # Zircon's own build directory: its generated headers reach the
            # delivery through the sysroot.
            continue
        # Whether it is a half's is the source set's decision, made once:
        # the UFS driver's territory is every directory its sources are in,
        # ufs/vrpmb included, not just ufs/mt8678 where it is declared.
        if header in ctx.in_halves and not header.startswith(
                ctx.tree.build_rel):
            owner_half = path_half(header, ctx.rules)
            if all(ctx.half_of_module[n] == owner_half for _, n in who):
                continue
            # Read across the halves: published where the other half's code
            # looks for it -- not where its own half does.  MTK's UFS driver
            # includes "virtio_rpmb_protocol.h" beside it; GRT's machina
            # includes it by its path in the tree, and only that is libddk's.
            foreign = set()
            for module_name, used in spelled_by.get(header, {}).items():
                if ctx.half_of_module[module_name] != owner_half:
                    foreign.update(used)
            # Wanted by the half that reaches across, not by the one whose
            # source it is: that half has the file already.
            reaching = set(ctx.half_of_module[n] for _, n in who
                           if ctx.half_of_module[n] != owner_half)
            for spelling in sorted(foreign or [header]):
                plan.put('include/' + spelling, header,
                         'read across the halves', True, halves=reaching)
            continue
        placed = sorted(spellings.get(header, []))
        if not placed:
            # Only ever included beside the file that includes it: it goes
            # wherever that file goes, which the closure below decides.
            # Placing it by its include directory as well published
            # rapidjson's allocators.h a second time, at a path no code uses.
            if header in quoted:
                continue
            relative = _install_path(ctx, header, [l for l, _ in who])
            placed = [relative] if relative is not None else []
        # The sysroot already answers <zircon/nbl_trace/ktrace.h>; a second
        # copy under include/ would only shadow it.
        placed = [r for r in placed
                  if 'sysroot/include/' + r not in sysroot_all or
                  'include/' + r in ctx.released_files]
        # One header, two spellings, one file.  The product reaches
        # ulib/zx's headers through both -I .../include and
        # -I .../include/lib, so <zx/time.h> and <lib/zx/time.h> are the
        # same file there; published at both paths they are two files, and
        # `#pragma once` lets a translation unit that spells it both ways
        # define zx::duration twice.  The delivery keeps the one under
        # lib/ and answers the short spelling the way the product does,
        # with -I include/lib in the Makefile.
        placed = [r for r in placed if 'lib/' + r not in placed]
        for relative in placed:
            destination = 'include/' + relative
            if plan.files.get(destination) == header:
                plan.evidence.add(destination)
                continue
            plan.put(destination, header, 'read, where the code spells it',
                     True,
                     halves=set(ctx.half_of_module[n] for _, n in who))
        if placed:
            pending.append((header, [l for l, _ in who],
                            frozenset(ctx.half_of_module[n] for _, n in who)))
    return pending


class _Walk(object):
    """What a carried header includes in turn, resolved the way a compiler
    would: a quoted include beside the file doing it first, so it lands
    beside wherever that file was installed -- rapidjson's document.h is at
    include/third_party/rapidjson/rapidjson/, and its "internal/meta.h" has
    to be under that too -- and then the readers' include directories, where
    the spelling is the path.
    """

    def __init__(self, plan, pending, readers, sysroot_all):
        self.pending = list(pending)
        # What the build was measured reading, and what the sysroot has:
        # the two things _take consults to decide whether to follow an
        # include.  Passed in rather than set afterwards, so a Walk is
        # usable the moment it exists.
        self.readers = readers
        self.sysroot_all = sysroot_all
        self.installed = {}
        for destination, source in plan.files.items():
            self.installed.setdefault(source, set()).add(destination)
        # A header is walked once per place it is installed, not once:
        # reached again under a second destination, what it includes beside
        # itself has to follow it there too.
        self.walked = set()

    def follow(self, ctx, plan):
        while self.pending:
            header, who, halves = self.pending.pop()
            fresh = sorted(d for d in self.installed.get(header, [])
                           if (header, d) not in self.walked)
            if not fresh:
                continue
            self.walked.update((header, d) for d in fresh)
            for spelling in _includes_of(header, ctx.tree):
                beside = os.path.normpath(os.path.join(
                    os.path.dirname(header), spelling)).replace(os.sep, '/')
                targets_found = []
                if os.path.isfile(ctx.tree.absolute(beside)):
                    for destination in fresh:
                        targets_found.append((beside, os.path.normpath(
                            os.path.join(os.path.dirname(destination),
                                         spelling)).replace(os.sep, '/')))
                else:
                    for label in who:
                        for directory in include_dirs_of(ctx.targets[label],
                                                         ctx.tree):
                            prefix = directory + '/' if directory else ''
                            candidate = prefix + spelling
                            if os.path.isfile(ctx.tree.absolute(candidate)):
                                targets_found.append((candidate,
                                                      'include/' + spelling))
                                break
                        if targets_found:
                            break
                for candidate, destination in targets_found:
                    self._take(ctx, plan, candidate, destination, who, halves)

    def _take(self, ctx, plan, candidate, destination, who, halves):
        if candidate in ctx.in_halves or not \
                destination.startswith('include/') or (
                    ctx.sysroot and
                    candidate.startswith(ctx.sysroot + '/')):
            return
        # Followed only where the build read it or the last release shipped
        # it.  protobuf's atomicops for other architectures were shipped,
        # and are kept; rapidjson's msinttypes are included only under
        # _MSC_VER, were never shipped, and are not dragged in by an
        # #include this compiler never takes.
        if candidate not in self.readers and \
                destination not in ctx.released_files:
            return
        if 'sysroot/' + destination in self.sysroot_all and \
                destination not in ctx.released_files:
            return
        if plan.files.get(destination) != candidate:
            plan.put(destination, candidate, 'included by a published header',
                     halves=halves)
        else:
            plan.want(destination, halves)
        self.installed.setdefault(candidate, set()).add(destination)
        self.pending.append((candidate, who, halves))


def _seed_sysroot_from_half_sources(ctx, plan):
    """What a half's own sources include from the sysroot.

    `readers` is what the product build recorded reading, and a half
    compiles more than the product did: guest/top.cc includes
    <zircon/syscalls/exception.h>, which no sysroot header includes and the
    product build never read, so nothing above carried it and GRT's half
    would not build.  A source that spells a sysroot header needs it,
    whoever else read it.
    """
    for source in sorted(ctx.in_halves):
        half = path_half(source, ctx.rules)
        try:
            with io.open(ctx.tree.absolute(source), encoding='utf-8',
                         errors='ignore') as handle:
                text = handle.read()
        except (IOError, OSError):
            continue
        for spelling in INCLUDE.findall(text):
            candidate = (ctx.sysroot + '/include/' + spelling).replace(
                os.sep, '/')
            if not os.path.isfile(ctx.tree.absolute(candidate)):
                continue
            destination = 'sysroot/include/' + spelling
            if destination in plan.files:
                plan.want(destination, set([half]))
                continue
            plan.put(destination, candidate, 'sysroot', halves=set([half]))


def _seed_sysroot_for_libcxx(ctx, plan):
    """What libc++ reaches for, whoever read it."""
    for spelling in LIBCXX_C_HEADERS:
        candidate = (ctx.sysroot + '/include/' + spelling).replace(os.sep, '/')
        if not os.path.isfile(ctx.tree.absolute(candidate)):
            continue
        destination = 'sysroot/include/' + spelling
        if destination in plan.files:
            plan.want(destination, ctx.both)
            continue
        plan.put(destination, candidate, 'sysroot', halves=ctx.both)


def _close_over_sysroot(ctx, plan):
    """What a carried sysroot header includes, by every branch it spells.

    Every branch, not the one this compiler takes: bits/stat.h reads
    "x86_64/stat.h" under __x86_64__ and "aarch64/stat.h" otherwise, and
    bits/io.h falls back to "generic/io.h".  Following only the first
    #include kept x86_64's copies and dropped the ones aarch64 actually
    compiles, which left both halves unable to build.  Cheap to be wrong
    the other way: a handful of small headers ride along.
    """
    sysroot_include = ctx.sysroot + '/include/'
    pending = [d for d in plan.files if d.startswith('sysroot/include/')]
    while pending:
        destination = pending.pop()
        source = plan.files.get(destination)
        if not source:
            continue
        try:
            with io.open(ctx.tree.absolute(source), encoding='utf-8',
                         errors='ignore') as handle:
                text = handle.read()
        except (IOError, OSError):
            continue
        here = os.path.dirname(source)
        for spelling in INCLUDE.findall(text):
            for candidate in (sysroot_include + spelling,
                              os.path.normpath(
                                  os.path.join(here, spelling))):
                candidate = candidate.replace(os.sep, '/')
                if not candidate.startswith(sysroot_include):
                    continue
                if not os.path.isfile(ctx.tree.absolute(candidate)):
                    continue
                target = 'sysroot/' + candidate[len(ctx.sysroot) + 1:]
                if target in plan.files:
                    if plan.wants.get(destination):
                        plan.want(target, plan.wants[destination])
                    break
                plan.put(target, candidate, 'included by a sysroot header',
                         halves=plan.wants.get(destination, set()))
                pending.append(target)
                break


def _carry_released_headers(ctx, plan, walk, order, sysroot_all):
    """What the last release shipped under include/ that nothing above
    placed, while the tree still has it.

    A build can reach one header by another road: guest_sos read
    <zircon/nebula/platform_abi.h> from zircon/system/public while that
    directory was among its include directories, and from the sysroot once
    it was not.  Same bytes, same spelling, and a consumer of the last
    release still includes it from include/ -- so it stays.  Gone from the
    tree, it is gone, and the lock says so.
    """
    for destination in sorted(ctx.released_files):
        if not destination.startswith('include/') or \
                destination in plan.files:
            continue
        relative = destination[len('include/'):]
        found = sysroot_all.get('sysroot/' + destination)
        for directory in [] if found else order:
            candidate = directory + '/' + relative
            if candidate not in ctx.in_halves and \
                    os.path.isfile(ctx.tree.absolute(candidate)):
                found = candidate
                break
        if found:
            # No half was measured needing it; it stays where the release
            # that shipped it is, and the smaller component does not grow a
            # file nobody asked for.
            plan.put(destination, found,
                     'shipped last release, still in the tree')
            walk.installed.setdefault(found, set()).add(destination)
            walk.pending.append((found, ctx.every_label, frozenset()))


def _archive_wants(ctx):
    """library name -> the halves whose modules link it.

    Read from the same walk the module description is written from, not
    from one target's `libs`: a driver reaches blk_crypto through a group,
    and its own libs list says nothing about it.
    """
    wanted = {}
    label_to_module = ctx.selection.get('modules', {})
    provided = sorted(set(ctx.binary.values()))
    # What the delivery can actually hand a half: the libraries the last
    # release shipped under lib/, and the modules the other half would link.
    # The walk's closure names more than that -- pretty, trace, zx, which the
    # delivery leaves out -- and derive_module drops them for the same
    # reason, so a half is never given a library its own build cannot link.
    provided_names = set(label_to_module.values())
    for released in ctx.released_files:
        if released.startswith('lib/lib') and released.endswith(
                ('.a', '.so')):
            provided_names.add(
                released[len('lib/lib'):released.rindex('.')])
    for owner, libraries in (ctx.module_libs or {}).items():
        # MODULE_OWNER is the half without its _ddk suffix.
        half = owner if owner in ctx.both else owner + '_ddk'
        for library in libraries:
            wanted.setdefault(library, set()).add(half)
    for name, labels in ctx.modules.items():
        if ctx.module_libs:
            break
        for label in labels:
            try:
                _, _, libraries, _ = build_map.walk(
                    ctx.the_map, label, label_to_module, provided)
            except Exception:      # a variant the map cannot walk
                continue
            # Only what libddk actually provides: the walk's closure names
            # libraries the delivery leaves out -- pretty, trace, zx -- and
            # the module description drops them for the same reason.  A
            # half is not given a library its own build never links.
            for library in libraries:
                if library in provided_names:
                    wanted.setdefault(library, set()).add(
                        ctx.half_of_module[name])
    return wanted


def _carry_libraries(ctx, plan):
    """The archives and shared libraries the delivery ships."""
    linked = {}
    for name, labels in ctx.modules.items():
        for label in labels:
            for spelling in ctx.targets[label].get('libs', []):
                linked.setdefault(spelling, set()).add(
                    ctx.half_of_module[name])
    wanted = _archive_wants(ctx)
    for plain, archive in sorted(ctx.binary.items()):
        variants = [l for l in ctx.targets
                    if build_map.strip_toolchain(l) == plain]
        variants.sort(key=lambda l: (SHARED_HINT not in l, l))
        entry = ctx.targets[variants[0]]
        outputs = [o for o in entry.get('outputs', [])
                   if o.endswith(('.a', '.so'))]
        unstripped = [o for o in outputs if 'lib.unstripped' in o]
        if outputs:
            source = (unstripped or outputs)[0]
            suffix = '.so' if source.endswith('.so') else '.a'
            plan.put('lib/lib%s%s' % (archive, suffix),
                     ctx.tree.spelled(source), 'binary library',
                     halves=wanted.get(archive, ()))
        elif entry['type'] == 'source_set':
            plan.put('lib/lib%s.a' % archive, 'archive:' + variants[0],
                     'binary library, archived from its objects',
                     halves=wanted.get(archive, ()))
    for spelling in sorted(linked):
        spelled = ctx.tree.spelled(spelling)
        if not spelled.endswith(('.a', '.so')) or os.path.isabs(spelled):
            continue
        name = build_map.library_name(spelled)
        destination = 'lib/lib%s%s' % (name, os.path.splitext(spelled)[1])
        # With descriptions to read, they decide: the raw link closure names
        # libraries no half's build asks for, and pretty is one of them.
        by_half = wanted.get(name) if ctx.module_libs else linked[spelling]
        if destination not in plan.files and \
                os.path.isfile(ctx.tree.absolute(spelled)):
            plan.put(destination, spelled, 'linked by a module',
                     halves=by_half or ())
        elif destination in plan.files and by_half:
            plan.want(destination, by_half)


def plan_component(the_map, selection, rules, tree, in_halves=(),
                   keep_headers=(), released_files=(), module_libs=None):
    """destination -> root-relative source, for everything libddk carries
    except the compiler, the ABI record and the licences.

    The phases run in the order their evidence becomes available: what the
    sysroot has, where a library's headers live, what the policy keeps, what
    the build was measured reading, what those include in turn, what a half
    spells for itself, and finally the libraries.
    """
    ctx = _Context(the_map, selection, rules, tree, in_halves, keep_headers,
                   released_files, module_libs)
    plan = _Plan()

    sysroot_all = _carry_sysroot(ctx, plan)
    order = _library_include_dirs(ctx)
    kept = _carry_trim_policy_keeps(ctx, plan, order)
    readers = _readers_of(ctx)
    spellings, spelled_by, quoted = _spellings_of(ctx)
    pending = _carry_read_headers(ctx, plan, readers, spellings, spelled_by,
                                  quoted, sysroot_all)

    # The cross-half headers are published too, and include things.
    for destination, source in list(plan.files.items()):
        if destination.startswith('include/'):
            pending.append((source,
                            [l for l, _ in readers.get(source, [])],
                            frozenset(plan.wants.get(destination, ()))))
    for candidate in kept:
        pending.append((candidate, ctx.every_label, ctx.both))

    walk = _Walk(plan, pending, readers, sysroot_all)
    walk.follow(ctx, plan)

    _seed_sysroot_from_half_sources(ctx, plan)
    _seed_sysroot_for_libcxx(ctx, plan)
    _close_over_sysroot(ctx, plan)

    _carry_released_headers(ctx, plan, walk, order, sysroot_all)
    walk.follow(ctx, plan)

    _carry_libraries(ctx, plan)

    reasons = plan.reasons
    reasons['_why'] = plan.why_of
    reasons['_wants'] = dict((d, sorted(h)) for d, h in plan.wants.items())
    return plan.files, reasons


def plan_sources(the_map, selection, rules, tree, released_halves=None):
    """destination -> root-relative source, for the source set the halves are
    split from.

    A module's territory is every directory one of its sources is in, not
    only the directory of its target: MTK's UFS driver is declared in
    ufs/mt8678 and compiles ufs/core/*.c, whose headers are in ufs/core/inc.
    Inside that territory it ships what it lists, what it reads, and the
    headers sitting directly beside its sources -- guest_view.h beside
    guest's sources is part of guest's source whether or not this build
    includes it.
    """
    targets = the_map['targets']
    modules = members(the_map, selection)
    # Headers only tests list -- machina's virtio_queue_fake.h -- are not the
    # module's source.
    listed_by_product, listed_by_tests = set(), set()
    for entry in targets.values():
        spelled = set(tree.spelled(x) for x in entry['sources'])
        (listed_by_tests if entry.get('testonly') else
         listed_by_product).update(spelled)
    test_only = listed_by_tests - listed_by_product

    source_dirs = set()
    for labels in modules.values():
        for label in labels:
            entry = targets[label]
            source_dirs.add(entry['dir'])
            for path in entry['sources']:
                spelled = tree.spelled(path)
                # A listed header's directory is territory too: LuaBridge
                # lists headers in LuaBridge/ and compiles nothing there.
                if build_map.classify(spelled) == 'source':
                    source_dirs.add(os.path.dirname(spelled))

    def closed(path):
        return any(path.startswith(p[2:]) for p in build_map.CLOSED_PREFIXES)

    def in_territory(path):
        return any(path.startswith(d + '/') for d in source_dirs)

    plan = {}
    for name in sorted(modules):
        for label in sorted(modules[name]):
            entry = targets[label]
            for path in [tree.spelled(x) for x in entry['sources']]:
                if build_map.classify(path) == 'source' and not closed(path):
                    plan[path] = path
            for path in [tree.spelled(x) for x in entry['headers']]:
                if build_map.classify(path) == 'source' and \
                        not closed(path) and in_territory(path):
                    plan[path] = path
    for directory in sorted(source_dirs):
        if closed(directory):
            continue
        absolute = tree.absolute(directory)
        if not os.path.isdir(absolute):
            continue
        for name in sorted(os.listdir(absolute)):
            path = (directory + '/' + name) if directory else name
            if name.endswith(PUBLISHED_HEADER_SUFFIXES) and \
                    path not in test_only and \
                    os.path.isfile(tree.absolute(path)):
                plan[path] = path

    # What the last release shipped in a half and the delivery still reads
    # stays in it.
    if released_halves:
        read = set()
        for labels in modules.values():
            for label in labels:
                read.update(tree.spelled(x) for x in targets[label]['headers'])
        for files in released_halves.values():
            for path in files:
                if path in read and path not in plan and \
                        os.path.isfile(tree.absolute(path)):
                    plan[path] = path
    return plan


def write_inputs(plan, tree, component, stage, manifest_path, layout_path,
                 extra_files=(), ar=None):
    """The portable manifest and layout materialize_ddk.py reads.

    One atom per top-level directory, installing at the path planned.  A
    source set planned as a library is archived here first, from the objects
    the product build left, into the stage directory under the build
    directory, so the manifest can name it like any other build output.
    """
    atoms = {}
    for destination, source in sorted(list(plan.items()) + list(extra_files)):
        if source.startswith('archive:'):
            if ar is None:
                _fail('%s is archived from objects and no --ar was given'
                      % destination)
            source = archive_objects(source[len('archive:'):], destination,
                                     tree, stage, ar)
        top = destination.split('/')[0] if '/' in destination else 'files'
        atoms.setdefault(top, []).append({
            'destination': destination, 'packaged': False,
            'source': tree.locator(source)})
    manifest = {
        'atoms': [{'id': {'domain': 'cpp', 'name': 'derived_' + top},
                   'files': files, 'gn-label': '(derived from the build map)',
                   'deps': [], 'package-deps': [], 'output-category': 'hee',
                   'tags': {'arch': 'target', 'domain': 'cpp',
                            'type': 'derived'}}
                  for top, files in sorted(atoms.items())],
        'ids': ['cpp/derived_' + top for top in sorted(atoms)],
        'meta': {'host-arch': 'x64', 'source-locator-schema': '1',
                 'target-arch': 'arm64'},
    }
    roots = sorted(atoms)
    contract = COMPONENT_CONTRACTS[component]
    layout = {
        'format_version': 1,
        'component': component,
        'closed_source_roots': ['zircon'],
        'roots': roots,
        'content': dict((r, contract.get(r, ['source'])) for r in roots),
        'atoms': dict(('cpp/derived_' + top,
                       {'prefix': '', 'reason': 'Derived from the build map: '
                        'everything %s carries under %s/.' % (component, top)})
                      for top in roots),
    }
    for path, document in ((manifest_path, manifest), (layout_path, layout)):
        with io.open(path, 'w', encoding='utf-8') as handle:
            text = json.dumps(document, indent=1, sort_keys=True) + '\n'
            handle.write(text if isinstance(text, type(u''))
                         else text.decode('utf-8'))
    return sum(len(f) for f in atoms.values())


# What each root of a component may hold: an interface and the binaries
# implementing it, and for the source set, source.  The same contract the
# hand-written layouts stated.
COMPONENT_CONTRACTS = {
    'libddk': {'include': ['header'], 'lib': ['binary'],
               'sysroot': ['header', 'binary'], 'abi': ['source']},
    # What only one half needs, travelling inside that half: headers,
    # archives, and the sysroot headers no other half reads.  The compiler
    # stays in the component beside them, and so do the sysroot's libraries:
    # both halves link those.
    'half_libddk': {'include': ['header'], 'lib': ['binary'],
                    'sysroot': ['header', 'binary']},
    'ddk_selftest_sources': {},
}


def archive_objects(label, destination, tree, stage, ar):
    """An archive from a source set's objects, in the stage directory."""
    the_map = archive_objects.the_map
    entry = the_map['targets'][label]
    objects = [os.path.join(tree.build_dir, o) for o in entry['objects']]
    missing = [o for o in objects if not os.path.isfile(o)]
    if missing:
        _fail('%s has %d object(s) the product build did not produce, e.g. '
              '%s' % (label, len(missing), missing[0]))
    out = os.path.join(stage, destination)
    if not os.path.isdir(os.path.dirname(out)):
        os.makedirs(os.path.dirname(out))
    if os.path.exists(out):
        os.remove(out)
    process = subprocess.Popen([ar, 'rcsD', out] + sorted(objects),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _, err = process.communicate()
    if process.returncode != 0:
        _fail('%s failed for %s: %s' % (ar, label,
                                        err.decode('utf-8', 'replace')))
    return tree.spelled(out)


# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest='command')
    component = sub.add_parser('component', help='plan libddk')
    component.add_argument('--map', required=True)
    component.add_argument('--selection', required=True)
    component.add_argument('--rules', required=True)
    component.add_argument('--root', required=True)
    component.add_argument('--build-dir', required=True)
    component.add_argument('--plan', required=True,
                           help='write destination -> source as JSON')
    component.add_argument('--manifest', help='write the portable manifest')
    component.add_argument('--layout', help='write the layout')
    component.add_argument('--stage', help='where archives made from objects '
                                            'go; under the build directory')
    component.add_argument('--ar', help='the pinned llvm-ar')
    component.add_argument('--abi', help='the directory export_abi.py wrote')
    component.add_argument('--lock', help='lock/libddk.lock.json')
    component.add_argument('--for', dest='for_half',
                           choices=['shared', 'private'],
                           help='shared: what the shared half needs, which is '
                                'what libddk carries. private: what it does '
                                'not, which travels inside the other half. '
                                'Omitted, the plan is the whole component.')
    component.add_argument('--shared-half', default='mtk_ddk',
                           help='the half libddk is shared with: what that '
                                'half needs is what libddk ships')
    component.add_argument('--modules',
                           help='directory of derived .ddk.mk files, whose '
                                'MODULE_LIBS say which half links which '
                                'library')
    component.add_argument('--trim-policy',
                           help="ddk_trim_policy.json: its include/ keeps are "
                                "published though nothing includes them")

    sources = sub.add_parser('sources', help='plan the source set')
    sources.add_argument('--map', required=True)
    sources.add_argument('--selection', required=True)
    sources.add_argument('--rules', required=True)
    sources.add_argument('--root', required=True)
    sources.add_argument('--build-dir', required=True)
    sources.add_argument('--modules', help='directory of derived .ddk.mk files '
                                           'to add, laid out by module '
                                           'directory; under the build dir')
    sources.add_argument('--plan', required=True)
    sources.add_argument('--lock', help='lock/libddk.lock.json')
    sources.add_argument('--manifest')
    sources.add_argument('--layout')
    args = parser.parse_args()
    try:
        the_map = _read_json(args.map)
        selection = _read_json(args.selection)
        rules = partition.read_rules(args.rules)
        tree = Tree(args.root, args.build_dir)
        archive_objects.the_map = the_map
        released_halves = None
        if getattr(args, 'lock', None):
            released_halves = _read_json(args.lock).get('halves')
        if args.command == 'sources':
            plan = plan_sources(the_map, selection, rules, tree,
                                released_halves)
            extra = []
            if args.modules:
                base = os.path.abspath(args.modules)
                for current, dirs, names in os.walk(base):
                    for name in names:
                        if name.endswith('.ddk.mk'):
                            path = os.path.join(current, name)
                            relative = os.path.relpath(path, base).replace(
                                os.sep, '/')
                            extra.append((relative, tree.spelled(path)))
            with io.open(args.plan, 'w', encoding='utf-8') as handle:
                text = json.dumps({'files': dict(plan)}, indent=1,
                                  sort_keys=True) + '\n'
                handle.write(text if isinstance(text, type(u''))
                             else text.decode('utf-8'))
            count = len(plan) + len(extra)
            if args.manifest and args.layout:
                count = write_inputs(plan, tree, 'ddk_selftest_sources', None,
                                     args.manifest, args.layout, extra)
            print('sources: %d file(s), %d module description(s) -> %s'
                  % (len(plan), len(extra), args.plan))
            return 0
        if args.command == 'component':
            halves = plan_sources(the_map, selection, rules, tree,
                                  released_halves)
            keeps = []
            if args.trim_policy:
                for entry in _read_json(args.trim_policy).get('keep', []):
                    prefix = entry.get('prefix', '')
                    if prefix.startswith('include/') and \
                            prefix.endswith(PUBLISHED_HEADER_SUFFIXES):
                        keeps.append(prefix[len('include/'):])
            released_files = set()
            if args.lock:
                # Everything the last release shipped, whichever component
                # carries it now: splitting libddk moved most of it inside
                # grt_ddk, and a header that moved is still a header the
                # release shipped.
                released_files = set(
                    build_map.lock_surface(_read_json(args.lock)))
            plan, reasons = plan_component(the_map, selection, rules, tree,
                                           halves, keeps, released_files,
                                           read_module_libs(args.modules))
            # libddk carries what the shared half needs; what it does not
            # need travels inside the other half, and never reaches the
            # customer who receives only the shared one.  A file no half was
            # measured needing stays out of the shared component for the
            # same reason.
            if args.for_half:
                wants = reasons.get('_wants', {})
                shared = args.shared_half
                keep = set(d for d in plan
                           if (shared in set(wants.get(d, ())))
                           == (args.for_half == 'shared'))
                left = len(plan) - len(keep)
                plan = dict((d, v) for d, v in plan.items() if d in keep)
                why_of = dict((d, w) for d, w in reasons['_why'].items()
                              if d in keep)
                reasons = {'_why': why_of,
                           '_wants': dict((d, h) for d, h
                                          in wants.items() if d in keep)}
                for why in why_of.values():
                    reasons[why] = reasons.get(why, 0) + 1
                print('  %s side: %d file(s), %d left to the other'
                      % (args.for_half, len(plan), left))
            with io.open(args.plan, 'w', encoding='utf-8') as handle:
                text = json.dumps({'files': plan, 'reasons': reasons},
                                  indent=1, sort_keys=True) + '\n'
                handle.write(text if isinstance(text, type(u''))
                             else text.decode('utf-8'))
            if args.manifest and args.layout:
                extra = []
                if args.abi:
                    for name in sorted(os.listdir(args.abi)):
                        if name in ('ABI.fingerprint', 'SYSCALLS.json'):
                            extra.append(('abi/' + name, tree.spelled(
                                os.path.join(os.path.abspath(args.abi),
                                             name))))
                write_inputs(plan, tree,
                             'half_libddk' if args.for_half == 'private'
                             else 'libddk',
                             os.path.abspath(args.stage or os.path.join(
                                 tree.build_dir, 'ddk-derived')),
                             args.manifest, args.layout, extra, args.ar)
            print('component: %d file(s) planned -> %s'
                  % (len(plan), args.plan))
            for why, count in sorted(reasons.items()):
                if why not in ('_why', '_wants'):
                    print('  %5d  %s' % (count, why))
            return 0
    except (DeriveError, build_map.MapError, partition.PartitionError) as error:
        sys.stderr.write('error: %s\n' % error)
        return 1
    parser.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())

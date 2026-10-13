#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Lays the finished delivery out, and refuses to ship anything else.

Everything upstream of this produces a piece: run_trim.sh produces the
component, run_split_selftest.sh produces the source halves, selftest.mk is
the build.  This is where they become one directory, and it is the only place
that says what a delivery contains:

    output/
      libddk/     the component, the compiler inside it
      grt_ddk/    one source half, with the Makefile that builds it
      mtk_ddk/    the other, the same way

Each part sits where the layout says -- one level down, at the name of its
repo project, when `--layout` gives one; see layout.py.

Directories and nothing else.  MTK keep every part under `repo`, which
manages projects and not loose files, so the Makefile that used to sit beside
the parts is installed into each half instead: a half is a directory somebody
owns, and the build that goes with it travels inside it.

Three entries and no fourth.  That is checked rather than intended: a stray
directory in a delivery is read as part of it, and the thing most likely to
turn up is a leftover build/ from testing the very layout this writes.

A symlink is allowed only where it stays inside the delivery.  The compiler
ships four of those on purpose -- clang++ and clang-7.0 beside clang, lld
beside ld.lld -- and turning them into copies would add 234 MB of the same
bytes.  One that is absolute, or that resolves outside, is refused: it is a
path back to the machine that built the delivery, which is what the
sandbox's old toolchain/ link was and why it is gone.
"""

from __future__ import print_function

import argparse
import errno
import hashlib
import json
import os
import shutil
import sys

import layout as layout_

MAKEFILE_NAME = 'Makefile'
RELEASE_NAME = 'RELEASE'
# The ABI of the libddk a half was cut against, as libddk states it in
# abi/ABI.fingerprint.  The recipe on MTK's side refuses a half whose
# LIBDDK_ABI is not the fingerprint of the libddk beside it; the releases
# may differ, the ABI may not.
ABI_LOCK_NAME = 'LIBDDK_ABI'
LIBDDK_FINGERPRINT = os.path.join('abi', 'ABI.fingerprint')

# A half may arrive as a git repository -- mtk_ddk does, because MTK work in
# it and need our commits, not a fresh tree every release.  Its history is
# part of what ships; the object store is not part of what the delivery *is*.
# Left out of the id for a plainer reason as well: pack files are not
# reproducible, so a digest over them would change every time nothing did.
GIT_DIR = '.git'
LIBDDK_NAME = 'libddk'
MODULE_SUFFIX = '.ddk.mk'
TOOLCHAIN_DRIVER = os.path.join('toolchain', 'bin', 'clang')

# What a licensed part carries.  Checked for every part of the delivery, the
# source halves included: a half is source somebody else compiles and
# redistributes, so the terms have to travel with it and not only with the
# component beside it.
LICENSES_DIR = 'LICENSES'
FILE_MAP_NAME = 'FILES'
NOTICE_NAME = 'NOTICE'


class DeliveryError(Exception):
    """A layout that would not be a delivery."""


def _fail(message):
    raise DeliveryError(message)


def _makedirs(path):
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise


def split_half(spelling):
    """`NAME=PATH`, or a path whose directory name is the name."""
    if '=' in spelling:
        name, _, path = spelling.partition('=')
    else:
        path = spelling
        name = os.path.basename(os.path.normpath(path))
    if not name or '/' in name or name in (LIBDDK_NAME, 'build', '.', '..'):
        _fail('unusable half name %r' % name)
    return name, path


def count_files(root):
    total = 0
    for _, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != GIT_DIR]
        total += len(names)
    return total


def has_module(root):
    for _, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != GIT_DIR]
        for name in names:
            if name.endswith(MODULE_SUFFIX):
                return True
    return False


def escaping_links(root):
    """Every symlink under `root` that does not resolve inside it.

    Copied as links rather than followed, so the question is where they point
    once the tree has moved: one that stays inside travels with the delivery,
    one that does not is a path back to the machine that built it, and
    following it instead would inline the bytes and hide that the source was
    never self-contained.
    """
    real_root = os.path.realpath(root)
    found = []
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != GIT_DIR)
        for name in sorted(dirs) + sorted(names):
            path = os.path.join(current, name)
            if not os.path.islink(path):
                continue
            target = os.readlink(path)
            resolved = os.path.realpath(
                target if os.path.isabs(target)
                else os.path.join(os.path.dirname(path), target))
            if (resolved != real_root
                    and not resolved.startswith(real_root + os.sep)):
                found.append((os.path.relpath(path, root), target))
    return found


def copy_tree(source, destination, label):
    """Copies a tree, keeping symlinks as symlinks."""
    if not os.path.isdir(source):
        _fail('missing %s: %s' % (label, source))
    escaping = escaping_links(source)
    if escaping:
        _fail('%s has %d symlink(s) pointing outside it, each of them a path '
              'back to the machine that built the delivery:\n  %s'
              % (label, len(escaping),
                 '\n  '.join('%s -> %s' % item for item in escaping[:20])))
    shutil.copytree(source, destination, symlinks=True)
    return count_files(destination)


def release_id(out, entries, layout=None):
    """One identifier, stamped into every part of the delivery.

    The three parts travel separately -- a git repository each -- and nothing
    in git ties them together.  `repo sync` can bring the sources forward and
    leave the component where it was, which compiles and links against the
    previous ABI and fails somewhere else entirely.  This is what a single
    repository used to do for free.

    A digest over the contents, not a counter: two deliveries built from the
    same tree get the same id, and one file different anywhere gets a
    different one.  The date is in front so a human can read which is newer.
    """
    digest = hashlib.sha256()
    for name in sorted(entries):
        root = layout_.part_dir(out, name, layout)
        if not os.path.isdir(root):
            digest.update(name.encode('utf-8'))
            with open(root, 'rb') as handle:
                digest.update(handle.read())
            continue
        for current, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d != GIT_DIR)
            for leaf in sorted(names):
                path = os.path.join(current, leaf)
                # Under the part's name, not its directory: the id is about
                # what the parts hold, not where the layout puts them.
                relative = '%s/%s' % (name, os.path.relpath(
                    path, root).replace(os.sep, '/'))
                if os.path.basename(relative) == RELEASE_NAME:
                    continue
                digest.update(relative.encode('utf-8'))
                if os.path.islink(path):
                    digest.update(os.readlink(path).encode('utf-8'))
                    continue
                with open(path, 'rb') as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b''):
                        digest.update(chunk)

    day = 'unknown'
    info = os.path.join(layout_.part_dir(out, LIBDDK_NAME, layout),
                        'BUILD_INFO.json')
    if os.path.isfile(info):
        with open(info, 'r') as handle:
            stamp = json.load(handle).get('source_date') or ''
        if len(stamp) >= 10:
            day = stamp[:10].replace('-', '')
    return '%s-%s' % (day, digest.hexdigest()[:12])


def stamp_release(out, entries, identifier, layout=None):
    """Writes the identifier into each directory the delivery has.

    The halves included, repositories though they are: the file is the one
    anchor everything downstream reads.  publish_delivery takes the id from
    it and commits it into the repository as `Release <id>`, the tag check
    compares against it, selftest.mk names a component and a half from
    different releases by it, and the recipe on MTK's side reads it where a
    tag cannot travel -- a change pushed for review carries no tags.  The
    commit's message is not in their format; what deploys the delivery
    rewrites it, since the release is the pipeline's own work.
    """
    written = []
    for name in sorted(entries):
        root = layout_.part_dir(out, name, layout)
        if not os.path.isdir(root):
            continue
        with open(os.path.join(root, RELEASE_NAME), 'w') as handle:
            handle.write(identifier + '\n')
        written.append('%s/%s' % (name, RELEASE_NAME))
    return written


def stamp_abi_lock(out, halves, layout=None):
    """Writes libddk's ABI fingerprint into each half as LIBDDK_ABI.

    A half travels on its own and may be updated without the libddk beside
    it, so it has to say which ABI it was built against; the recipe compares
    that with the libddk it finds.  A component that states no fingerprint
    has nothing to lock to, and the halves go out without one.
    """
    source = os.path.join(layout_.part_dir(out, LIBDDK_NAME, layout),
                          LIBDDK_FINGERPRINT)
    if not os.path.isfile(source):
        return []
    with open(source) as handle:
        fingerprint = handle.read().strip()
    if not fingerprint:
        _fail('%s/%s is empty: a half cannot be locked to no ABI'
              % (LIBDDK_NAME, LIBDDK_FINGERPRINT))
    written = []
    for name in halves:
        root = layout_.part_dir(out, name, layout)
        if not os.path.isdir(root):
            continue
        with open(os.path.join(root, ABI_LOCK_NAME), 'w') as handle:
            handle.write(fingerprint + '\n')
        written.append('%s/%s' % (name, ABI_LOCK_NAME))
    return written


def build(libddk, halves, makefile, out, require_toolchain=True,
          components=(), layout=None):
    """Writes the delivery and returns what it contains."""
    if os.path.exists(out):
        shutil.rmtree(out)
    _makedirs(out)

    def where(name):
        return layout_.part_dir(out, name, layout)

    contents = {}
    contents[LIBDDK_NAME] = copy_tree(libddk, where(LIBDDK_NAME), 'component')
    if require_toolchain:
        driver = os.path.join(where(LIBDDK_NAME), TOOLCHAIN_DRIVER)
        if not os.path.isfile(driver):
            _fail('the component carries no compiler at %s/%s. A delivery '
                  'without one is a delivery the customer cannot build; pass '
                  '--no-toolchain if that is deliberate.'
                  % (LIBDDK_NAME, TOOLCHAIN_DRIVER))

    seen = set()
    for spelling in halves:
        name, path = split_half(spelling)
        if name in seen:
            _fail('two halves claim the name %r' % name)
        seen.add(name)
        contents[name] = copy_tree(path, where(name), 'half %s' % name)
        if not has_module(where(name)):
            _fail('half %r holds no module description, so nothing in it '
                  'would be built' % name)
    if not seen:
        _fail('a delivery needs at least one source half')

    # What only one half needs travels inside it, as libddk/ of its own: the
    # other half -- and the customer who receives only that one -- never sees
    # those headers or archives.  The component beside the halves holds what
    # both need, and the compiler.
    #
    # The half arrives with a copy of it already, and that copy is the
    # stripped and redacted one: it is trimmed before the licences are
    # collected and before the mirror is made, so that what the mirror commits
    # -- the history MTK clone and keep -- holds the archives that ship rather
    # than the 79 MB of DWARF they were built from.  Replacing it here instead
    # would only mend the working tree; the objects would keep every source
    # path the trim exists to remove, one release at a time.
    #
    # Installing it again is still worth doing, and cheap: the bytes are the
    # same, and a delivery that puts every part where it says it does is one
    # nobody has to reason about.
    for spelling in components:
        name, path = split_half(spelling)
        if name not in seen:
            _fail('--half-component %s: no half of that name in this delivery'
                  % name)
        inside = os.path.join(where(name), LIBDDK_NAME)
        if os.path.isdir(inside):
            contents[name] -= count_files(inside)
            shutil.rmtree(inside)
        contents[name] += copy_tree(path, inside, 'component for %s' % name)

    if not os.path.isfile(makefile):
        _fail('missing makefile: %s' % makefile)
    for name in sorted(seen):
        shutil.copyfile(makefile, os.path.join(where(name), MAKEFILE_NAME))
        contents[name] += 1

    identifier = release_id(out, list(contents), layout)
    for relative in stamp_release(out, list(contents), identifier, layout):
        contents[relative.split('/')[0]] += 1
    for relative in stamp_abi_lock(out, sorted(seen), layout):
        contents[relative.split('/')[0]] += 1

    check(out, sorted(seen), layout)
    return contents, identifier


def check(out, halves, layout=None):
    """The delivery holds what it should and nothing else."""
    parts = [LIBDDK_NAME] + list(halves)
    places = dict((name, layout_.where(name, layout)) for name in parts)
    expected = layout_.expected_entries(layout, parts)
    # The top level, and every directory the layout puts a part under: each
    # may hold what the layout names there and nothing else.
    wrappers = [''] + sorted(e for e in expected if e not in places.values())
    for wrapper in wrappers:
        inside = os.path.join(out, *wrapper.split('/')) if wrapper else out
        found = set(('%s/%s' % (wrapper, name)) if wrapper else name
                    for name in os.listdir(inside))
        extra = sorted(found - expected)
        if extra:
            _fail('%d entr(y/ies) in the delivery that do not belong to it: '
                  '%s. The likely one is a build/ left by testing the layout.'
                  % (len(extra), ', '.join(extra)))
    missing = sorted(places[name] for name in parts
                     if not os.path.isdir(layout_.part_dir(out, name, layout)))
    if missing:
        _fail('the delivery is missing: %s' % ', '.join(missing))

    unbuildable = [name for name in halves
                   if not os.path.isfile(os.path.join(
                       layout_.part_dir(out, name, layout), MAKEFILE_NAME))]
    if unbuildable:
        _fail('%s carr(y/ies) no %s. Each half is built on its own, from '
              'inside itself, so the build has to travel in it.'
              % (', '.join(unbuildable), MAKEFILE_NAME))

    unlicensed = []
    for name in parts:
        for relative in (os.path.join(LICENSES_DIR, FILE_MAP_NAME),
                         NOTICE_NAME):
            if not os.path.isfile(os.path.join(
                    layout_.part_dir(out, name, layout), relative)):
                unlicensed.append('%s/%s' % (name, relative))
    if unlicensed:
        _fail('%d part(s) of the delivery carry no licence record: %s. Every '
              'part ships on its own -- one repo project each -- so each one '
              'has to say what it is under without the others beside it.'
              % (len(unlicensed), ', '.join(unlicensed)))

    escaping = escaping_links(out)
    if escaping:
        _fail('%d symlink(s) in the delivery point outside it, each of them a '
              'path back to the machine that built it:\n  %s'
              % (len(escaping),
                 '\n  '.join('%s -> %s' % item for item in escaping[:20])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--libddk', required=True,
                        help='the trimmed component, compiler installed')
    parser.add_argument('--half', required=True, action='append', default=[],
                        help='a source half, as NAME=PATH or just PATH. '
                             'Repeat: MTK receives both.')
    parser.add_argument('--half-component', action='append', default=[],
                        help='NAME=PATH: a component that belongs to one half '
                             'and lands inside it as libddk/. Repeat.')
    parser.add_argument('--makefile', required=True,
                        help='the standalone build, installed as each '
                             'half\'s Makefile')
    parser.add_argument('--out', required=True, help='the delivery directory')
    parser.add_argument('--no-toolchain', action='store_true',
                        help='allow a component that carries no compiler')
    parser.add_argument('--layout', default='',
                        help='PART=DIR,...: where each part sits under --out. '
                             'A part not named sits at its own name.')
    args = parser.parse_args()

    try:
        layout = layout_.parse(args.layout)
        contents, identifier = build(args.libddk, args.half, args.makefile,
                                     args.out, not args.no_toolchain,
                                     args.half_component, layout)
    except (DeliveryError, layout_.LayoutError) as error:
        sys.stderr.write('error: %s\n' % error)
        return 1

    total = sum(contents.values())
    for name in sorted(contents):
        print('  %-10s %6d files' % (name, contents[name]))
    print('delivery: %d entries, %d files, release %s -> %s'
          % (len(contents), total, identifier, args.out))
    return 0


if __name__ == '__main__':
    sys.exit(main())

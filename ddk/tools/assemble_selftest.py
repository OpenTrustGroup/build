#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Assembles the sealed DDK self-test sandbox.

The sandbox is built *outside* the product tree and contains only:

  libddk/     the materialized component, the compiler inside it
  grt_ddk/    one source half, named for what it is, with its Makefile, and
              libddk/ of its own where the delivery gives it one
  mtk_ddk/    the other, when the sandbox is meant to hold both

Whichever halves are handed over land beside each other under their own
names, because which of them is present is what tells the two deliveries
apart: MTK receives both, a customer receives one and the other as binaries.
The compiler is not linked in -- it ships inside libddk, and a symlink here
would be a fourth thing in a layout that has three.

Isolation here is physical, not advisory.  The product source root and the
product build directory are simply not reachable from inside the sandbox, so a
driver cannot silently satisfy an include from the product tree and report a
green build.  A full copy of Garnet would defeat exactly that: `mt8678_ufs`
depends on `//garnet/public/lib/blk_crypto`, which MTK never receives, so it
must come from libddk or not build at all.
"""

import argparse
import errno
import os
import shutil
import sys

import partition


MAKEFILE_NAME = 'Makefile'


class AssembleError(Exception):
    """A sandbox that would not actually be sealed."""


def _real(path):
    return os.path.realpath(os.path.abspath(path))


def _is_within(path, root):
    path = _real(path)
    root = _real(root)
    return path == root or path.startswith(root + os.sep)


def check_out_of_tree(sandbox, forbidden_roots):
    """Refuses to build a sandbox that can see the product tree."""
    for label, root in forbidden_roots:
        if not root:
            continue
        if _is_within(sandbox, root) or _is_within(root, sandbox):
            raise AssembleError(
                'the self-test sandbox must live outside the %s (%s); '
                'a sandbox nested with the product tree can reach it through '
                'relative paths and the build would not prove anything'
                % (label, root))


def make_dirs(path):
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno != errno.EEXIST or not os.path.isdir(path):
            raise


def copy_tree(source, destination, label, rename=None):
    """Copies a tree, optionally laying it out the way a delivery does.

    Where the rules rename the top-level directory, libddk publishes its
    headers under the delivered name, so a sandbox built from the tree has to
    be rendered into that form too -- or the source says
    `#include "garnet/bin/guest/proto/vm.pb.h"` and the component has it
    somewhere else.  No rename is declared today and this copies as it is.  Only the whole-tree sandboxes need
    this: the split assembles from parts that were rendered when they were
    split, and rendering those a second time would be refused -- undoing it
    would not reproduce them.
    """
    if not os.path.isdir(source):
        raise AssembleError('missing %s tree: %s' % (label, source))
    if os.path.isdir(destination):
        shutil.rmtree(destination)
    if rename is None:
        shutil.copytree(source, destination)
    else:
        partition.render(source, destination, rename)
    count = 0
    for _, _, files in os.walk(destination):
        count += len(files)
    if not count:
        raise AssembleError('%s tree is empty: %s' % (label, source))
    return count


def write_makefile(sandbox, template, roots):
    """Installs the standalone Makefile into each half, as a delivery does.

    A half is built from inside itself, so the build travels in it; the
    sandbox is laid out the way the delivery is, or it proves something else.
    The module list is not written out: the Makefile finds it from the
    descriptions the half ships, so the sandbox cannot disagree with them.
    """
    with open(template, 'r') as handle:
        text = handle.read()
    for name in roots:
        with open(os.path.join(sandbox, name, MAKEFILE_NAME), 'w') as handle:
            handle.write(text)


MODULE_SUFFIX = '.ddk.mk'


def split_source(spelling):
    """`NAME=PATH`, or a path whose directory name is the name.

    The name is the directory a half lands in, and it is what tells one
    delivery from another: `grt_ddk` and `mtk_ddk` beside each other is what
    MTK receives, `mtk_ddk` alone is what reaches a customer.
    """
    if '=' in spelling:
        name, _, path = spelling.partition('=')
    else:
        path = spelling
        name = os.path.basename(os.path.normpath(path))
    if not name or '/' in name or name in ('libddk', 'build', '.', '..'):
        raise AssembleError('unusable source root name %r' % name)
    return name, path


def discover_modules(source_roots):
    """Every module description shipped with the source halves.

    A description sits beside the sources it describes, so that a partition by
    path carries it to the same half as the code.  Several modules
    legitimately share a directory, so the module is named by its description
    rather than by the directory -- and a name claimed by two halves is an
    error, not a merge: the build would produce one output for two sets of
    sources and nobody would be told which won.
    """
    found = {}
    for source_root in source_roots:
        for current, dirs, files in os.walk(source_root):
            dirs.sort()
            for name in sorted(files):
                if not name.endswith(MODULE_SUFFIX):
                    continue
                module = name[:-len(MODULE_SUFFIX)]
                path = os.path.join(current, name)
                if module in found:
                    raise AssembleError(
                        'two module descriptions claim the name %s: %s and %s'
                        % (module, found[module], path))
                found[module] = path
    if not found:
        raise AssembleError(
            'no module description found under %s; a source half must declare '
            'at least one module to build' % ', '.join(source_roots))
    return sorted(found)


def assemble(args):
    sandbox = _real(args.out)
    check_out_of_tree(sandbox, [
        ('product source root', args.source_root),
        ('product build directory', args.out_root),
    ])

    if os.path.isdir(sandbox):
        shutil.rmtree(sandbox)
    make_dirs(sandbox)

    libddk_files = copy_tree(args.libddk, os.path.join(sandbox, 'libddk'),
                             'libddk')
    # The rules are read, not asked for a rename: the sandbox has to be laid
    # out the way the delivery is, and whether that involves renaming the
    # top-level directory is the rules file's decision. It declares none
    # today -- the parts keep the tree's `garnet` -- and a sandbox that
    # refused to assemble without one would make turning that back on a
    # change here as well as there.
    rename = None
    if getattr(args, 'rename_rules', None):
        rename = partition.read_rules(args.rename_rules).get('rename')
    source_files = 0
    roots = []
    for spelling in args.sources:
        name, path = split_source(spelling)
        roots.append(name)
        source_files += copy_tree(path, os.path.join(sandbox, name),
                                  'DDK source %s' % name, rename)
    # A half's own component, where the delivery gives it one: what only that
    # half needs travels in it, so the other half's sandbox proves it can be
    # built without ever seeing those headers.
    extra_files = 0
    for spelling in getattr(args, 'extra', None) or []:
        name, path = split_source(spelling)
        if name not in roots:
            raise AssembleError(
                '--extra %s: no source half of that name in this sandbox'
                % name)
        extra_files += copy_tree(
            path, os.path.join(sandbox, name, 'libddk'),
            'component for %s' % name)
    # No build/ at the sandbox root: each half is built from inside itself
    # and its output is its own, which is what the delivery does.

    # No toolchain link.  The compiler ships inside the component, and a
    # sandbox built before it is put there is told where it is on make's
    # command line rather than given a symlink that would appear in the
    # delivered layout and in nothing a consumer receives.
    modules = discover_modules(
        [os.path.join(sandbox, name) for name in roots])
    write_makefile(sandbox, args.makefile, roots)

    return {
        'sandbox': sandbox,
        'libddk_files': libddk_files,
        'source_files': source_files,
        'extra_files': extra_files,
        'roots': roots,
        'modules': modules,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--libddk', required=True,
                        help='materialized libddk component')
    parser.add_argument('--sources', required=True, action='append',
                        default=[],
                        help='a source half, as NAME=PATH or just PATH, in '
                             'which case the directory name is used. Repeat '
                             'for a sandbox holding more than one -- which is '
                             'what MTK receives.')
    parser.add_argument('--toolchain',
                        help='pinned toolchain root, recorded so the sealed '
                             'build can be told where it is. Not linked into '
                             'the sandbox: the compiler ships inside libddk.')
    parser.add_argument('--rename-rules',
                        help='partition rules whose rename lays the sources '
                             'out the way a delivery does. For a whole-tree '
                             'sandbox only: a part is rendered when it is '
                             'split.')
    parser.add_argument('--extra', action='append', default=[],
                        help='NAME=PATH: a component that belongs to one half '
                             'and lands in it as libddk/. Repeat.')
    parser.add_argument('--makefile', required=True,
                        help='standalone build, installed as each half\'s '
                             'Makefile')
    parser.add_argument('--out', required=True,
                        help='sandbox directory, outside the product tree')
    parser.add_argument('--source-root', required=True,
                        help='product source root the sandbox must not touch')
    parser.add_argument('--out-root', required=True,
                        help='product build dir the sandbox must not touch')
    parser.add_argument('--stamp', help='file to touch on success')
    args = parser.parse_args()

    try:
        report = assemble(args)
    except AssembleError as error:
        sys.stderr.write('error: %s\n' % error)
        return 1

    if args.stamp:
        with open(args.stamp, 'w') as stamp:
            stamp.write('')
    sys.stdout.write(
        'assembled sealed self-test sandbox: %s\n'
        '  libddk files: %d\n'
        '  source files: %d\n'
        '  modules: %s\n'
        % (report['sandbox'], report['libddk_files'], report['source_files'],
           ', '.join(report['modules'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())

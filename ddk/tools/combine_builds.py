#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Exports a built half as binaries, and combines halves into one delivery.

The delivery has two tiers, and they differ in what is source:

  to MTK       grt_ddk source + mtk_ddk source + libddk
  to a client  mtk_ddk source + grt_ddk *binaries* + libddk

So a half has to be deliverable as binaries alone.  `export` turns a built
half into a bundle -- the artifacts plus a manifest of what is in it -- with
no source in it, and `combine` unions bundles into one tree.

Both steps are declarative, like everything else here.  What a half is
expected to produce is read from the module descriptions it was built from,
not from whatever happens to be in its output directory: a half that silently
stopped building one of its drivers would otherwise export a bundle that is
short one file and says nothing about it.

Combining is a union, not a link.  That holds because no module in one half
links a module in the other -- `check` reports the cross-half edges, and
there are none today.  If that ever changes, the halves stop being separately
buildable and this is where it will show.
"""

from __future__ import print_function

import argparse
import errno
import json
import os
import re
import shutil
import stat
import sys

FORMAT_VERSION = 1
MANIFEST_NAME = 'BUILD_CONTENTS.json'
MODULE_SUFFIX = '.ddk.mk'

# How each module type appears in a build directory, and where it installs in
# a bundle.  Drivers are loadable modules and keep their module name, which
# the build writes as <module>/module.so.
ARTIFACTS = {
    'driver': ('%s/module.so', 'drivers/%s.so'),
    'executable': ('bin/%s', 'bin/%s'),
    'shared_library': ('lib/lib%s.so', 'lib/lib%s.so'),
    'static_library': ('lib/lib%s.a', 'lib/lib%s.a'),
}


class CombineError(Exception):
    """A half that did not produce what it declared, or two that collide."""


def _fail(message):
    raise CombineError(message)


def _makedirs(path):
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise


def read_modules(source_root):
    """Every module description in a source tree, as name -> fields."""
    modules = {}
    for current, dirs, files in os.walk(source_root):
        dirs.sort()
        for name in sorted(files):
            if not name.endswith(MODULE_SUFFIX):
                continue
            path = os.path.join(current, name)
            fields = {}
            with open(path, 'r') as handle:
                for line in handle:
                    match = re.match(r'^(MODULE_\w+)\s*:?=\s*(.*)$', line)
                    if match:
                        fields[match.group(1)] = match.group(2).split()
            module = fields.get('MODULE_NAME')
            if not module:
                _fail('%s declares no MODULE_NAME' % path)
            module = module[0]
            if module in modules:
                _fail('two module descriptions claim the name %s' % module)
            kind = (fields.get('MODULE_TYPE') or ['driver'])[0]
            if kind not in ARTIFACTS:
                _fail('%s: unsupported MODULE_TYPE %s' % (path, kind))
            modules[module] = {
                'type': kind,
                'deps': fields.get('MODULE_DEPS', []),
            }
    return modules


def export(sandbox, part, out):
    """Turns a built half into a bundle of binaries plus a manifest.

    No source is copied.  A client receiving this bundle gets what the half
    produces and nothing it was produced from.
    """
    # The half is built from inside itself and its output is its own, so the
    # build directory is under the half rather than beside it.
    build_root = os.path.join(sandbox, part, 'build')
    if not os.path.isdir(build_root):
        build_root = os.path.join(sandbox, 'build')
    if not os.path.isdir(build_root):
        _fail('%s has no build directory; build the half first'
              % os.path.join(sandbox, part))

    # The half lives under its own name -- grt_ddk, mtk_ddk -- not under a
    # fixed `src`, because which halves a sandbox holds is what tells the two
    # deliveries apart.  Read from the sandbox rather than named here, the
    # same way the Makefile and the hermeticity check read it.
    source_roots = sorted(
        os.path.join(sandbox, name) for name in os.listdir(sandbox)
        if name not in ('libddk', 'build')
        and os.path.isdir(os.path.join(sandbox, name)))
    modules = {}
    for source_root in source_roots:
        modules.update(read_modules(source_root))
    if not modules:
        _fail('no module descriptions under %s'
              % ', '.join(source_roots) or sandbox)

    if os.path.exists(out):
        shutil.rmtree(out)
    _makedirs(out)

    entries = {}
    missing = []
    for module in sorted(modules):
        kind = modules[module]['type']
        built_pattern, install_pattern = ARTIFACTS[kind]
        built = os.path.join(build_root, built_pattern % module)
        if not os.path.isfile(built):
            missing.append('%s (%s, expected %s)'
                           % (module, kind, built_pattern % module))
            continue
        destination = install_pattern % module
        target = os.path.join(out, destination)
        _makedirs(os.path.dirname(target))
        shutil.copyfile(built, target)
        os.chmod(target, stat.S_IMODE(os.lstat(built).st_mode))
        entries[destination] = {
            'module': module,
            'type': kind,
            'size': os.path.getsize(target),
        }
    if missing:
        _fail('%s declares modules it did not produce:\n  %s'
              % (part, '\n  '.join(missing)))

    manifest = {
        'format_version': FORMAT_VERSION,
        'part': part,
        'entries': entries,
    }
    with open(os.path.join(out, MANIFEST_NAME), 'w') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True,
                  separators=(',', ': '))
        handle.write('\n')
    return entries


def read_bundle(bundle):
    manifest_path = os.path.join(bundle, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        _fail('%s is not an exported half: no %s' % (bundle, MANIFEST_NAME))
    with open(manifest_path, 'r') as handle:
        manifest = json.load(handle)
    if manifest.get('format_version') != FORMAT_VERSION:
        _fail('%s: unsupported manifest format' % bundle)
    return manifest


def combine(bundles, out):
    """Unions exported halves into one delivery tree.

    A path claimed by two halves is refused rather than resolved: one half
    quietly overwriting the other's driver is exactly the failure this whole
    split exists to make impossible.
    """
    if os.path.exists(out):
        shutil.rmtree(out)
    _makedirs(out)

    claimed = {}
    combined = {}
    for bundle in bundles:
        manifest = read_bundle(bundle)
        part = manifest['part']
        for destination, entry in sorted(manifest['entries'].items()):
            if destination in claimed:
                _fail('%s is produced by both %s and %s'
                      % (destination, claimed[destination], part))
            claimed[destination] = part
            source = os.path.join(bundle, destination)
            if not os.path.isfile(source):
                _fail('%s: manifest names %s, which is not in the bundle'
                      % (bundle, destination))
            target = os.path.join(out, destination)
            _makedirs(os.path.dirname(target))
            shutil.copyfile(source, target)
            os.chmod(target, stat.S_IMODE(os.lstat(source).st_mode))
            record = dict(entry)
            record['part'] = part
            combined[destination] = record

    manifest = {
        'format_version': FORMAT_VERSION,
        'parts': sorted(set(claimed.values())),
        'entries': combined,
    }
    with open(os.path.join(out, MANIFEST_NAME), 'w') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True,
                  separators=(',', ': '))
        handle.write('\n')
    return combined


def cross_half_edges(source_roots):
    """Dependencies that cross from one half into another.

    Combining is a union, which is only sound while this is empty.
    """
    owner = {}
    graphs = {}
    for part, root in sorted(source_roots.items()):
        graphs[part] = read_modules(root)
        for module in graphs[part]:
            owner[module] = part
    crossings = []
    for part in sorted(graphs):
        for module in sorted(graphs[part]):
            for dependency in graphs[part][module]['deps']:
                other = owner.get(dependency)
                if other is not None and other != part:
                    crossings.append('%s/%s depends on %s/%s'
                                     % (part, module, other, dependency))
    return crossings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['export', 'combine', 'check'])
    parser.add_argument('--sandbox', help='a built half, for export')
    parser.add_argument('--part', help='name of the half, for export')
    parser.add_argument('--bundle', action='append', default=[],
                        help='an exported half, for combine (repeatable)')
    parser.add_argument('--source', action='append', default=[],
                        help='<part>=<source root>, for check (repeatable)')
    parser.add_argument('--out', help='where to write')
    args = parser.parse_args()

    try:
        if args.command == 'export':
            if not args.sandbox or not args.part or not args.out:
                parser.error('export needs --sandbox, --part and --out')
            entries = export(args.sandbox, args.part, args.out)
            print('%s: %d artifacts -> %s'
                  % (args.part, len(entries), args.out))
            return 0

        if args.command == 'combine':
            if not args.bundle or not args.out:
                parser.error('combine needs --bundle (repeatable) and --out')
            combined = combine(args.bundle, args.out)
            by_part = {}
            for entry in combined.values():
                by_part[entry['part']] = by_part.get(entry['part'], 0) + 1
            for part in sorted(by_part):
                print('  %-10s %3d artifacts' % (part, by_part[part]))
            print('combined %d artifacts -> %s' % (len(combined), args.out))
            return 0

        if args.command == 'check':
            if not args.source:
                parser.error('check needs --source <part>=<root>')
            roots = {}
            for item in args.source:
                if '=' not in item:
                    parser.error('--source takes <part>=<root>')
                part, root = item.split('=', 1)
                roots[part] = root
            crossings = cross_half_edges(roots)
            if crossings:
                sys.stderr.write(
                    'the halves are not separately buildable; combining them '
                    'would need a link, not a union:\n  %s\n'
                    % '\n  '.join(crossings))
                return 1
            print('no dependency crosses the halves: combining is a union')
            return 0
    except CombineError as error:
        sys.stderr.write('error: %s\n' % error)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

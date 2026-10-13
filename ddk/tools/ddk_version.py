#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Describes a delivery, describes the tree, and says what moved.

A customer reports a driver misbehaving against a libddk released some months
ago.  The first question is always the same -- did the kernel ABI move under
it? -- and answering it used to mean opening two components by hand and
comparing them by eye.

  describe --component <dir>   what a delivered libddk was built against
  describe --tree <root>       what the tree here would build against now
  compare <a> <b>              what is different, and whether it matters

Both descriptions have the same shape, so either can be compared with either:
two deliveries against each other, a delivery against the tree, or the tree
against itself at two revisions.

A name whose digest moved is the answer nine times out of ten.  When the
answer is "the numbers changed and nothing else did", the numbers are
syscalls.abigen at the zircon revision BUILD_INFO.json names, on each side.
"""

from __future__ import print_function

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_info  # noqa: E402
import export_abi  # noqa: E402

FORMAT_VERSION = 1


class VersionError(Exception):
    """A description that could not be made, or two that cannot be compared."""


def _fail(message):
    raise VersionError(message)


def _read_json(path, what):
    if not os.path.isfile(path):
        _fail('%s has no %s' % (os.path.dirname(path) or '.', what))
    with open(path, 'r') as handle:
        return json.load(handle)


def describe_component(component):
    """What a delivered component says about itself."""
    abi_dir = os.path.join(component, 'abi')
    syscalls = _read_json(os.path.join(abi_dir, export_abi.SYSCALLS_FILE),
                          'abi/' + export_abi.SYSCALLS_FILE)
    fingerprint_path = os.path.join(abi_dir, export_abi.FINGERPRINT_FILE)
    if not os.path.isfile(fingerprint_path):
        _fail('%s has no abi/%s' % (component, export_abi.FINGERPRINT_FILE))
    with open(fingerprint_path, 'r') as handle:
        fingerprint = handle.read().strip()

    info = _read_json(os.path.join(component, build_info.BUILD_INFO_FILE),
                      build_info.BUILD_INFO_FILE)
    return {
        'format_version': FORMAT_VERSION,
        'from': 'component',
        'origin': os.path.abspath(component),
        'component': info.get('component'),
        'abi': {
            'fingerprint': fingerprint,
            'count': syscalls.get('count'),
            'syscalls': syscalls.get('syscalls', {}),
        },
        'build': {
            'built_at': info.get('built_at'),
            'source_date': info.get('source_date'),
            'dirty': info.get('dirty', []),
            'revisions': dict(
                (name, entry['commit'])
                for name, entry in info.get('revisions', {}).items()),
            'toolchain': info.get('toolchain'),
        },
    }


def describe_tree(source_root, abigen, ordinals=None, component='libddk',
                  like=None):
    """What the tree here would build against now.

    `ordinals` is optional because the generated number table lives in the
    Zircon build output, which a checkout that has not been built does not
    have.  Without it there is no fingerprint -- the fingerprint covers the
    numbers -- but the per-syscall digests are still there, and those are what
    name the syscall that moved.
    """
    declarations = export_abi.read_abigen(abigen)
    if ordinals:
        numbers = export_abi.read_ordinals(ordinals)
        record, fingerprint = export_abi.build(declarations, numbers)
    else:
        record, fingerprint = export_abi.build(declarations, {}), None
        record = record[0] if isinstance(record, tuple) else record

    # The abigen file alone names one repository, and a description that
    # records one where the other records eight compares as seven differences
    # that are not differences.  `like` takes the set from a delivery, so the
    # two line up and a moved revision is the only thing that shows as one.
    repositories = build_info.repositories_for(source_root, [abigen])
    if like:
        repositories = sorted(set(repositories) | set(like))
    info = build_info.build_info(component, source_root, repositories)
    return {
        'format_version': FORMAT_VERSION,
        'from': 'tree',
        'origin': os.path.abspath(source_root),
        'component': component,
        'abi': {
            'fingerprint': fingerprint,
            'count': record['count'],
            'syscalls': record['syscalls'],
        },
        'build': {
            'built_at': info['built_at'],
            'source_date': info['source_date'],
            'dirty': info['dirty'],
            'revisions': dict((name, entry['commit'])
                              for name, entry in info['revisions'].items()),
            'toolchain': info.get('toolchain'),
        },
    }


def compare(left, right):
    """Everything that differs, most consequential first."""
    for side, description in (('left', left), ('right', right)):
        if description.get('format_version') != FORMAT_VERSION:
            _fail('%s is not a version description' % side)

    la, ra = left['abi'], right['abi']
    findings = []

    if la['fingerprint'] and ra['fingerprint']:
        same = la['fingerprint'] == ra['fingerprint']
    else:
        same = None  # One side has no ordinals, so no fingerprint.

    ls, rs = la['syscalls'], ra['syscalls']
    added = sorted(set(rs) - set(ls))
    removed = sorted(set(ls) - set(rs))
    changed = sorted(name for name in set(ls) & set(rs)
                     if ls[name]['digest'] != rs[name]['digest'])
    renumbered = sorted(name for name in set(ls) & set(rs)
                        if ls[name]['digest'] == rs[name]['digest']
                        and ls[name].get('numbered') !=
                        rs[name].get('numbered'))

    lb, rb = left['build'], right['build']
    # Only where both sides say something: a repository one description does
    # not name has not moved, it is unrecorded, and calling that a difference
    # buries the ones that are.
    both = sorted(set(lb['revisions']) & set(rb['revisions']))
    moved = [(name, lb['revisions'][name], rb['revisions'][name])
             for name in both
             if lb['revisions'][name] != rb['revisions'][name]]
    unrecorded = sorted(set(lb['revisions']) ^ set(rb['revisions']))

    # A toolchain change is its own answer to "why does this not link any
    # more", and it is not visible in any revision: the compiler is
    # downloaded, so moving the pin moves nothing in git.  Reported only when
    # both sides recorded one -- a component built before the field existed
    # says nothing about its compiler, and treating that as "changed" would
    # cry wolf on every old delivery.
    lt, rt = lb.get('toolchain'), rb.get('toolchain')
    toolchain_moved = None
    if lt and rt and lt != rt:
        toolchain_moved = (lt, rt)

    return {
        'toolchain_moved': toolchain_moved,
        'abi_same': same,
        'fingerprints': (la['fingerprint'], ra['fingerprint']),
        'added': added,
        'removed': removed,
        'changed': changed,
        'renumbered': renumbered,
        'revisions_moved': moved,
        'revisions_unrecorded': unrecorded,
        'dirty': sorted(set(lb['dirty']) | set(rb['dirty'])),
    }


def report(result, stream):
    """The comparison, said in the order somebody diagnosing wants it."""
    same = result['abi_same']
    if same is True:
        stream.write('ABI: identical.  A driver built against one runs against'
                     ' the other.\n')
    elif same is False:
        stream.write('ABI: MOVED.  A driver built against one may call the '
                     'wrong entry in the other.\n')
    else:
        stream.write('ABI: cannot say -- one side has no ordinals, so there '
                     'is no fingerprint to compare.\n')
    stream.write('  %s\n  %s\n' % (result['fingerprints'][0] or '(none)',
                                   result['fingerprints'][1] or '(none)'))

    for label, names in (('removed', result['removed']),
                         ('added', result['added']),
                         ('declaration changed', result['changed']),
                         ('entered or left the kernel',
                          result['renumbered'])):
        if names:
            stream.write('\n%s (%d):\n' % (label, len(names)))
            for name in names[:40]:
                stream.write('    %s\n' % name)
            if len(names) > 40:
                stream.write('    ... and %d more\n' % (len(names) - 40))

    if same is False and not (result['removed'] or result['added']
                              or result['changed'] or result['renumbered']):
        stream.write('\nNo declaration changed, so what moved is the numbers:'
                     ' compare zircon/system/public/zircon/syscalls.abigen\n'
                     '  at the two revisions BUILD_INFO.json names.\n')

    if result.get('toolchain_moved'):
        before, after = result['toolchain_moved']
        stream.write('\nTOOLCHAIN CHANGED.  The C++ symbols in the two '
                     'deliveries do not have to match, and a link failure '
                     'here is not an ABI problem:\n'
                     '    clang %s  %s\n'
                     '    clang %s  %s\n'
                     % (before.get('clang_release') or '?',
                        before.get('clang_stamp') or '(unrecorded)',
                        after.get('clang_release') or '?',
                        after.get('clang_stamp') or '(unrecorded)'))

    if result['revisions_moved']:
        stream.write('\nrepositories at different revisions (%d):\n'
                     % len(result['revisions_moved']))
        for name, before, after in result['revisions_moved']:
            stream.write('    %-26s %s -> %s\n'
                         % (name, (before or '(absent)')[:12],
                            (after or '(absent)')[:12]))

    if result.get('revisions_unrecorded'):
        stream.write('\nnot recorded on both sides, so nothing can be said '
                     'about them: %s\n'
                     % ', '.join(result['revisions_unrecorded']))

    if result['dirty']:
        stream.write('\nbuilt from a tree with uncommitted changes, so the '
                     'revisions above do not describe it: %s\n'
                     % ', '.join(result['dirty']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')

    description = sub.add_parser('describe')
    description.add_argument('--component', help='a delivered libddk')
    description.add_argument('--tree', help='a source root')
    description.add_argument('--abigen', help='syscalls.abigen, with --tree')
    description.add_argument('--ordinals',
                          help='zx-syscall-numbers.h, with --tree. Without it '
                               'there is no fingerprint')
    description.add_argument('--like', metavar='COMPONENT',
                            help='with --tree: record the same repositories '
                                 'this delivery does, so the two descriptions '
                                 'compare like with like')
    description.add_argument('--out', help='write here instead of stdout')

    comparison = sub.add_parser('compare')
    comparison.add_argument('left')
    comparison.add_argument('right')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 2

    try:
        if args.command == 'describe':
            if bool(args.component) == bool(args.tree):
                parser.error('describe takes exactly one of --component '
                             'and --tree')
            if args.component:
                description = describe_component(args.component)
            else:
                if not args.abigen:
                    parser.error('--tree needs --abigen')
                like = None
                if args.like:
                    like = describe_component(args.like)['build']['revisions']
                description = describe_tree(args.tree, args.abigen,
                                            args.ordinals, like=like)
            text = json.dumps(description, indent=2, sort_keys=True,
                              separators=(',', ': ')) + '\n'
            if args.out:
                with open(args.out, 'w') as handle:
                    handle.write(text)
                sys.stdout.write('described %s -> %s\n'
                                 % (description['from'], args.out))
            else:
                sys.stdout.write(text)
            return 0

        if args.command == 'compare':
            left = _read_json(args.left, os.path.basename(args.left))
            right = _read_json(args.right, os.path.basename(args.right))
            result = compare(left, right)
            report(result, sys.stdout)
            return 0 if result['abi_same'] is True else 1
    except VersionError as error:
        sys.stderr.write('error: %s\n' % error)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

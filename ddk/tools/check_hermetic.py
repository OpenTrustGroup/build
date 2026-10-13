#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Proves the DDK self-test build read nothing outside the sandbox.

The sandbox already makes the product tree physically unreachable.  This is the
second half of the argument: every dependency the compiler actually recorded
must resolve inside the sandbox or inside the pinned toolchain, and anything
else fails.  A host header picked up from /usr/include is the failure this
catches -- the export machine has the package installed, the customer's machine
does not, and nothing else in the pipeline would notice.

The measured closure is written out as a by-product.  It is the input for the
`measured is a subset of declared` check: it says what the build used, never
what the component ships.
"""

import argparse
import json
import os
import sys


class HermeticError(Exception):
    """A dependency the sandbox was not supposed to be able to reach."""


def _real(path):
    return os.path.realpath(os.path.abspath(path))


def _is_within(path, root):
    return path == root or path.startswith(root + os.sep)


def parse_depfile(path):
    """Returns the dependency paths recorded in a Make-style depfile."""
    with open(path, 'r') as handle:
        text = handle.read()
    text = text.replace('\\\n', ' ')

    # -MP adds a phony rule per header ("a.h:" on its own line).  Splitting on
    # the first colon only would read those rule names back as dependencies,
    # so walk the rules instead: each line contributes what follows its own
    # first colon, and a phony rule contributes nothing.
    rules = 0
    tokens = []
    for line in text.splitlines():
        _, separator, dependencies = line.partition(':')
        if not separator:
            continue
        rules += 1
        for token in dependencies.split():
            token = token.replace('\\ ', ' ').strip()
            if token:
                tokens.append(token)
    if not rules:
        raise HermeticError('malformed dependency file: %s' % path)
    return tokens


TRACE_NAME = 'link.trace'


def parse_tracefile(path):
    """Returns the paths a linker reported opening, one per line.

    A dependency file records what the compiler read and says nothing about
    what the linker opened, so without this every archive in the component
    looks unused.  `-Wl,--trace` is the linker's own answer to the same
    question, and is read the same way: as evidence, not as a declaration.
    """
    tokens = []
    with open(path, 'r') as handle:
        for line in handle:
            token = line.strip()
            # lld prefixes an archive member as "archive(member.o)"; the file
            # that ships is the archive.
            if token.endswith(')') and '(' in token:
                token = token[:token.index('(')]
            if token:
                tokens.append(token)
    return tokens


def find_tracefiles(build_dir):
    found = []
    for current, _, files in os.walk(build_dir):
        if TRACE_NAME in files:
            found.append(os.path.join(current, TRACE_NAME))
    return sorted(found)


def find_depfiles(build_dir):
    found = []
    for current, _, files in os.walk(build_dir):
        for name in files:
            if name.endswith('.d'):
                found.append(os.path.join(current, name))
    return sorted(found)


def classify(dependency, zones):
    """The zone a dependency belongs to, and its root, or (None, None).

    The root comes back with the name because several zones share a name:
    each source half is its own directory, and a file's path has to be
    reported relative to the half it came from.
    """
    for name, root in zones:
        if _is_within(dependency, root):
            return name, root
    return None, None


def source_zones(sandbox):
    """Every top-level directory in the sandbox that holds a source half.

    Found rather than named, the same way the Makefile finds them: which
    halves are present is what tells the two deliveries apart -- MTK receives
    both, a customer receives one -- and a fixed `src` could describe neither.
    """
    found = []
    for name in sorted(os.listdir(sandbox)):
        path = os.path.join(sandbox, name)
        if name in ('libddk', 'build') or not os.path.isdir(path):
            continue
        found.append(('src', path))
    return found


def builds_in(sandbox):
    """Where each half's build output is, and the directory make ran in.

    A half carries its own Makefile and is built from inside itself, so each
    has a build/ of its own and the paths in it are relative to the half.
    """
    found = []
    for _, half in source_zones(sandbox):
        build = os.path.join(half, 'build')
        if os.path.isdir(build):
            found.append((build, half))
    return found


def check(build_dir, sandbox, toolchain, require_depfiles=True,
          compile_dir=None):
    """Every file the compiler read, sorted into zones, or an error.

    A relative path in a dependency file is relative to the directory the
    compiler ran in, which is the half being built.  Resolving them against
    the dependency file's own directory instead lands them under build/,
    which is a zone, so nothing escapes and nothing is measured: the check
    passes while proving nothing.

    `build_dir` and `compile_dir` name one build to scan; given neither, every
    half in the sandbox is scanned against its own directory.
    """
    sandbox = _real(sandbox)
    halves = source_zones(sandbox)
    zones = [
        ('libddk', os.path.join(sandbox, 'libddk')),
        ('build', os.path.join(sandbox, 'build')),
        ('toolchain', _real(toolchain)),
    ] + [('build', os.path.join(half, 'build')) for _, half in halves] \
      + halves

    if build_dir:
        scans = [(build_dir, _real(compile_dir) if compile_dir else sandbox)]
    else:
        scans = [(build, half) for build, half in builds_in(sandbox)]
    depfiles = [d for build, _ in scans for d in find_depfiles(build)]
    if require_depfiles and not depfiles:
        raise HermeticError(
            'the self-test build produced no dependency files under %s; '
            'without them nothing proves what the compiler read'
            % ', '.join(build for build, _ in scans) or sandbox)

    escapes = []
    closure = {}
    evidence = []
    for build, where in scans:
        evidence += [(d, where, parse_depfile(d)) for d in find_depfiles(build)]
        evidence += [(t, where, parse_tracefile(t))
                     for t in find_tracefiles(build)]
    for source, where, tokens in evidence:
        for token in tokens:
            dependency = _real(token if os.path.isabs(token)
                               else os.path.join(where, token))
            zone, root = classify(dependency, zones)
            if zone is None:
                escapes.append((os.path.relpath(source, sandbox), token))
                continue
            if zone in ('libddk', 'src'):
                closure.setdefault(zone, set()).add(
                    os.path.relpath(dependency, root))

    if escapes:
        details = ['%s -> %s' % (source, token)
                   for source, token in sorted(set(escapes))[:50]]
        raise HermeticError(
            'the self-test build read %d dependency path(s) from outside the '
            'sandbox and the pinned toolchain. Each one is a file the customer '
            'will not have:\n  %s' % (len(set(escapes)), '\n  '.join(details)))

    return {
        'libddk': sorted(closure.get('libddk', set())),
        'src': sorted(closure.get('src', set())),
        'depfiles': len(depfiles),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sandbox', required=True,
                        help='sealed self-test sandbox root')
    parser.add_argument('--build-dir',
                        help='one build output to scan; by default every '
                             'half\'s own build/ is scanned')
    parser.add_argument('--toolchain', required=True,
                        help='pinned toolchain root, the permitted exception')
    parser.add_argument('--compile-dir',
                        help='directory the compiler ran in, which relative '
                             'dependency paths are relative to. Only with '
                             '--build-dir: a half is scanned against itself.')
    parser.add_argument('--closure-out',
                        help='write the measured closure here as JSON')
    parser.add_argument('--stamp', help='file to touch on success')
    args = parser.parse_args()

    build_dir = args.build_dir
    try:
        report = check(build_dir, args.sandbox, args.toolchain,
                       compile_dir=args.compile_dir)
    except HermeticError as error:
        sys.stderr.write('error: %s\n' % error)
        return 1

    if args.closure_out:
        with open(args.closure_out, 'w') as handle:
            json.dump({
                'format_version': 1,
                'libddk': report['libddk'],
                'src': report['src'],
            }, handle, indent=2, sort_keys=True)
            handle.write('\n')
    if args.stamp:
        with open(args.stamp, 'w') as stamp:
            stamp.write('')
    sys.stdout.write(
        'self-test build is sealed: %d dependency files, '
        '%d libddk files used, %d source files used\n'
        % (report['depfiles'], len(report['libddk']), len(report['src'])))
    return 0


if __name__ == '__main__':
    sys.exit(main())

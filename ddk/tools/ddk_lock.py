#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Holds libddk still from one release to the next.

A delivery is derived from the build, and the build moves: a target is
renamed, a dependency added, a header split in two.  Derived faithfully, each
of those reaches libddk, and a consumer who built against the last release
finds a header gone, a library called something else, or a symbol missing at
link time -- none of it decided by anybody.

So what a release promised is written down, and the next one is held to it.
The lock records libddk's surface -- every header, library and sysroot file
it installs, each with its hash; every symbol each library exports; the
kernel ABI fingerprint; the name, half and type of every source module -- and
`check` compares a new delivery against it:

  added                    reported, and allowed
  changed, still a superset  a header edited, a library gaining symbols:
                           reported, and allowed
  removed or renamed       a file, a module, or a symbol a library used to
                           export: the release stops
  moved                    a module changing half or type: stops
  kernel ABI               a different fingerprint: stops

A release that stops is not broken; it is waiting for a person.  `accept`
rewrites the lock from the delivery, and the lock's diff in review is the
decision and its record.  Nothing else about libddk needs anybody's input.

The symbols are a separate file, one `library<TAB>symbol` per line and sorted,
so that the review of an accepted change reads as the list of what went.

Commands:

  check   compare a delivery against the lock
  accept  write the lock from a delivery
"""

from __future__ import print_function

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys

import layout as layout_

FORMAT_VERSION = 1

LIBDDK = 'libddk'
HALVES = ('grt_ddk', 'mtk_ddk')

# What a consumer compiles and links against.  Everything else libddk carries
# is either about the delivery -- its licences, what built it, its release --
# or the compiler, which is tracked by its stamp rather than file by file.
SURFACE = ('include/', 'lib/', 'sysroot/', 'abi/')

LIBRARY_SUFFIXES = ('.a', '.so')
FINGERPRINT = 'abi/ABI.fingerprint'
TOOLCHAIN_INFO = 'toolchain/TOOLCHAIN_INFO.json'


class LockError(Exception):
    """A lock or a delivery that cannot be read."""


def _fail(message):
    raise LockError(message)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value):
    return value if isinstance(value, type(u'')) else value.decode('utf-8')


# ---------------------------------------------------------------------------
# reading a delivery


def half_components(delivery, nm, symbols, layout=None):
    """What each half carries as a component of its own, hashed like libddk.

    Splitting libddk moved most of it inside grt_ddk, and a surface nobody
    records is a surface nobody holds still: GRT's own half is built against
    these headers and archives exactly as MTK's is built against libddk.
    The symbols go into the same table, under the half's path, so a library
    that stops exporting something is caught wherever it lives.
    """
    found = {}
    for half in HALVES:
        root = os.path.join(layout_.part_dir(delivery, half, layout), LIBDDK)
        if not os.path.isdir(root):
            continue
        files = {}
        for relative in surface_files(root):
            path = os.path.join(root, relative)
            files[relative] = None if os.path.islink(path) else _sha256(path)
            if relative.endswith(LIBRARY_SUFFIXES) and \
                    not os.path.islink(path) and is_object(path):
                symbols['%s/%s/%s' % (half, LIBDDK, relative)] = \
                    exported_symbols(path, nm)
        found[half] = files
    return found


def surface_files(libddk):
    """Every file a consumer builds against, root-relative, sorted."""
    found = []
    for current, dirs, names in os.walk(libddk):
        dirs.sort()
        for name in sorted(names):
            relative = os.path.relpath(os.path.join(current, name),
                                       libddk).replace(os.sep, '/')
            if relative.startswith(SURFACE):
                found.append(relative)
    return sorted(found)


def is_object(path):
    """An ELF file or an archive -- something with symbols to read.

    musl's libdl.so, libm.so, libpthread.so and librt.so are linker scripts
    of four lines each, pointing at libc.so.  They are still files a consumer
    links against, and still tracked by their hash; they have no symbols.
    """
    with open(path, 'rb') as handle:
        head = handle.read(8)
    return head.startswith(b'\x7fELF') or head == b'!<arch>\n'


def exported_symbols(path, nm):
    """The global symbols a library defines.

    Defined and external only: an undefined reference is something the
    library needs, not something it offers, and a local symbol is nobody
    else's business.  An archive's member headers are dropped.
    """
    command = [nm, '--defined-only', '--extern-only', '-P']
    if path.endswith('.so'):
        command.insert(1, '-D')
    process = subprocess.Popen(command + [path], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    out, err = process.communicate()
    if process.returncode != 0:
        _fail('%s could not read %s: %s'
              % (nm, path, err.decode('utf-8', 'replace').strip()))
    symbols = set()
    for line in out.decode('utf-8', 'replace').splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0].endswith(':'):
            continue
        symbols.add(fields[0])
    return sorted(symbols)


def read_modules(delivery, layout=None):
    """name -> half, directory, type and sources, from every half delivered."""
    modules = {}
    for half in HALVES:
        root = layout_.part_dir(delivery, half, layout)
        if not os.path.isdir(root):
            continue
        for current, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d != '.git')
            for name in sorted(names):
                if not name.endswith('.ddk.mk'):
                    continue
                fields = {}
                with io.open(os.path.join(current, name),
                             encoding='utf-8') as handle:
                    for line in handle:
                        match = re.match(r'^(MODULE_[A-Z_]+)\s*:=\s*(.*)$',
                                         line.rstrip('\n'))
                        if match:
                            fields[match.group(1)] = match.group(2).split()
                module = ' '.join(fields.get('MODULE_NAME', []))
                if not module:
                    _fail('%s names no module' % os.path.join(current, name))
                if module in modules:
                    _fail('two halves describe a module called %s' % module)
                modules[module] = {
                    'half': half,
                    'dir': os.path.relpath(current, root).replace(os.sep, '/'),
                    'type': ' '.join(fields.get('MODULE_TYPE', [])),
                    'sources': sorted(fields.get('MODULE_SRCS', [])),
                }
    return modules


HALF_BOOKKEEPING = ('RELEASE', 'LIBDDK_ABI', 'NOTICE', '.partition.json')
HALF_BOOKKEEPING_DIRS = ('.git', 'LICENSES')


def read_half_files(delivery, layout=None):
    """half -> every source file it ships, less its bookkeeping.

    Recorded so that the next release keeps shipping what this one did where
    the build still uses it, rather than re-deciding it by rule: a header of
    a closed library that a delivered driver includes was shipped in the
    source half, and keeps being.  Losing a file from a half is reported and
    does not stop a release -- nobody links against a source file.
    """
    halves = {}
    for half in HALVES:
        root = layout_.part_dir(delivery, half, layout)
        if not os.path.isdir(root):
            continue
        files = []
        for current, dirs, names in os.walk(root):
            if current == root:
                dirs[:] = [d for d in dirs if d not in HALF_BOOKKEEPING_DIRS]
            dirs.sort()
            for name in sorted(names):
                relative = os.path.relpath(os.path.join(current, name),
                                           root).replace(os.sep, '/')
                if relative not in HALF_BOOKKEEPING:
                    files.append(relative)
        halves[half] = sorted(files)
    return halves


def snapshot(delivery, nm, layout=None):
    """What a delivery promises, and the symbols its libraries export."""
    libddk = layout_.part_dir(delivery, LIBDDK, layout)
    if not os.path.isdir(libddk):
        _fail('%s has no %s/' % (delivery, layout_.where(LIBDDK, layout)))

    files = {}
    symbols = {}
    libraries = {}
    for relative in surface_files(libddk):
        path = os.path.join(libddk, relative)
        files[relative] = None if os.path.islink(path) else _sha256(path)
        if relative.startswith(('lib/', 'sysroot/')) and \
                relative.endswith(LIBRARY_SUFFIXES) and \
                not os.path.islink(path) and is_object(path):
            exported = exported_symbols(path, nm)
            symbols[relative] = exported
            digest = hashlib.sha256(
                '\n'.join(exported).encode('utf-8')).hexdigest()
            libraries[relative] = {'symbols': len(exported),
                                   'digest': digest}

    fingerprint = None
    if os.path.isfile(os.path.join(libddk, FINGERPRINT)):
        with io.open(os.path.join(libddk, FINGERPRINT),
                     encoding='utf-8') as handle:
            fingerprint = handle.read().strip()
    toolchain = None
    if os.path.isfile(os.path.join(libddk, TOOLCHAIN_INFO)):
        with io.open(os.path.join(libddk, TOOLCHAIN_INFO),
                     encoding='utf-8') as handle:
            info = json.load(handle)
        toolchain = {'prebuilt_stamp': info.get('prebuilt_stamp'),
                     'clang_release': info.get('clang_release'),
                     'target': info.get('target')}

    release = None
    if os.path.isfile(os.path.join(libddk, 'RELEASE')):
        with io.open(os.path.join(libddk, 'RELEASE'),
                     encoding='utf-8') as handle:
            release = handle.read().strip()

    lock = {
        'format_version': FORMAT_VERSION,
        'release': release,
        'abi_fingerprint': fingerprint,
        'toolchain': toolchain,
        'files': files,
        'libraries': libraries,
        'modules': read_modules(delivery, layout),
        'halves': read_half_files(delivery, layout),
        'half_components': half_components(delivery, nm, symbols, layout),
    }
    return lock, symbols


# ---------------------------------------------------------------------------
# reading and writing the lock


def write_lock(lock, symbols, lock_path, symbols_path):
    directory = os.path.dirname(os.path.abspath(lock_path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    text = json.dumps(lock, indent=1, sort_keys=True) + '\n'
    with io.open(lock_path, 'w', encoding='utf-8') as handle:
        handle.write(_text(text))
    with io.open(symbols_path, 'w', encoding='utf-8') as handle:
        for library in sorted(symbols):
            for symbol in symbols[library]:
                handle.write(u'%s\t%s\n' % (library, _text(symbol)))


def read_lock(lock_path, symbols_path):
    if not os.path.isfile(lock_path):
        _fail('no lock at %s. The first release writes it with `accept`.'
              % lock_path)
    with io.open(lock_path, encoding='utf-8') as handle:
        lock = json.load(handle)
    if lock.get('format_version') != FORMAT_VERSION:
        _fail('%s is format %r; this reads %d'
              % (lock_path, lock.get('format_version'), FORMAT_VERSION))
    symbols = {}
    if os.path.isfile(symbols_path):
        with io.open(symbols_path, encoding='utf-8') as handle:
            for line in handle:
                library, _, symbol = line.rstrip('\n').partition('\t')
                if symbol:
                    symbols.setdefault(library, []).append(symbol)
    for library, record in lock.get('libraries', {}).items():
        if len(symbols.get(library, [])) != record['symbols']:
            _fail('%s says %s exports %d symbol(s) and %s lists %d. The two '
                  'are written together; one was edited by hand.'
                  % (lock_path, library, record['symbols'], symbols_path,
                     len(symbols.get(library, []))))
    return lock, symbols


# ---------------------------------------------------------------------------
# comparing


# A file gone from a half's own component stops a release for the same
# reason one gone from libddk does: the half that builds against it is the
# consumer, and it finds out at compile time.
BLOCKING = ('files_removed', 'component_files_removed', 'symbols_removed',
            'modules_removed', 'modules_moved', 'abi_changed')


def compare(old, old_symbols, new, new_symbols):
    """Everything that differs, sorted into what stops a release and what does not."""
    old_files, new_files = old['files'], new['files']
    changes = {
        'files_added': sorted(set(new_files) - set(old_files)),
        'files_removed': sorted(set(old_files) - set(new_files)),
        'files_changed': sorted(
            p for p in set(old_files) & set(new_files)
            if old_files[p] is not None and new_files[p] is not None and
            old_files[p] != new_files[p]),
    }

    # A half's own component is a surface too: GRT's half builds against the
    # headers and archives inside it exactly as MTK's builds against libddk,
    # so what was moved out of libddk is still held still.
    old_half = old.get('half_components', {})
    new_half = new.get('half_components', {})
    half_removed, half_added, half_changed = [], [], []
    for half in sorted(set(old_half) | set(new_half)):
        before = old_half.get(half, {})
        after = new_half.get(half, {})
        half_removed += ['%s/%s' % (half, p)
                         for p in sorted(set(before) - set(after))]
        half_added += ['%s/%s' % (half, p)
                       for p in sorted(set(after) - set(before))]
        half_changed += [
            '%s/%s' % (half, p) for p in sorted(set(before) & set(after))
            if before[p] is not None and after[p] is not None
            and before[p] != after[p]]
    changes['component_files_removed'] = half_removed
    changes['component_files_added'] = half_added
    changes['component_files_changed'] = half_changed

    # Every library the delivery ships, wherever it ships it from.
    present = set(new_files)
    for half, files in new_half.items():
        present.update('%s/%s/%s' % (half, LIBDDK, p) for p in files)

    removed, added = {}, {}
    for library in sorted(set(old_symbols) | set(new_symbols)):
        before = set(old_symbols.get(library, []))
        after = set(new_symbols.get(library, []))
        # A library that went away entirely is already a removed file.
        if library in present and before - after:
            removed[library] = sorted(before - after)
        if after - before:
            added[library] = sorted(after - before)
    changes['symbols_removed'] = removed
    changes['symbols_added'] = added

    old_modules, new_modules = old['modules'], new['modules']
    changes['modules_added'] = sorted(set(new_modules) - set(old_modules))
    gone = sorted(set(old_modules) - set(new_modules))
    # A module whose sources carry on under another name was renamed, which
    # breaks a consumer's -l as surely as removing it; say which.
    renamed = {}
    for name in gone:
        sources = set(old_modules[name]['sources'])
        for other in changes['modules_added']:
            if sources and sources == set(new_modules[other]['sources']):
                renamed[name] = other
    changes['modules_removed'] = [
        name if name not in renamed else '%s (now %s)' % (name, renamed[name])
        for name in gone]
    changes['modules_moved'] = sorted(
        '%s: %s %s -> %s %s' % (name, old_modules[name]['half'],
                                old_modules[name]['type'],
                                new_modules[name]['half'],
                                new_modules[name]['type'])
        for name in set(old_modules) & set(new_modules)
        if (old_modules[name]['half'], old_modules[name]['type']) !=
        (new_modules[name]['half'], new_modules[name]['type']))
    changes['modules_changed'] = sorted(
        name for name in set(old_modules) & set(new_modules)
        if old_modules[name]['sources'] != new_modules[name]['sources'])

    changes['abi_changed'] = (
        [old['abi_fingerprint'], new['abi_fingerprint']]
        if old.get('abi_fingerprint') != new.get('abi_fingerprint') else [])
    old_halves, new_halves = old.get('halves', {}), new.get('halves', {})
    changes['half_files_removed'] = sorted(
        '%s/%s' % (half, path) for half in old_halves
        for path in set(old_halves[half]) - set(new_halves.get(half, [])))
    changes['half_files_added'] = sorted(
        '%s/%s' % (half, path) for half in new_halves
        for path in set(new_halves[half]) - set(old_halves.get(half, [])))
    changes['toolchain_changed'] = (
        [old.get('toolchain'), new.get('toolchain')]
        if old.get('toolchain') != new.get('toolchain') else [])
    return changes


def blocking(changes):
    return [key for key in BLOCKING if changes.get(key)]


def summarize(changes, old, new, stream):
    stream.write('libddk against release %s:\n' % (old.get('release') or '?'))

    def listing(label, items, limit=20):
        if not items:
            return
        stream.write('  %s: %d\n' % (label, len(items)))
        for item in list(items)[:limit]:
            stream.write('      %s\n' % item)
        if len(items) > limit:
            stream.write('      ... and %d more\n' % (len(items) - limit))

    if changes['abi_changed']:
        stream.write('  kernel ABI changed: %s -> %s\n'
                     % tuple(v[:16] if v else v
                             for v in changes['abi_changed']))
    listing('files removed', changes['files_removed'])
    for library, symbols in sorted(changes['symbols_removed'].items()):
        listing('symbols no longer exported by %s' % library, symbols, 10)
    listing('modules removed or renamed', changes['modules_removed'])
    listing('modules moved', changes['modules_moved'])
    listing('files added', changes['files_added'], 10)
    listing('files changed', changes['files_changed'], 10)
    if changes['symbols_added']:
        stream.write('  symbols added: %d, in %s\n'
                     % (sum(len(v) for v in changes['symbols_added'].values()),
                        ', '.join(sorted(changes['symbols_added']))))
    listing('modules added', changes['modules_added'])
    listing('modules whose sources changed', changes['modules_changed'], 10)
    listing('files no longer in a half', changes['half_files_removed'], 10)
    listing('files new in a half', changes['half_files_added'], 10)
    if changes['toolchain_changed']:
        stream.write('  compiler changed: %s -> %s\n'
                     % tuple(json.dumps(v, sort_keys=True)
                             for v in changes['toolchain_changed']))
    stops = blocking(changes)
    if stops:
        stream.write('STOP: %s. A consumer of the last release would break. '
                     'If that is intended, `accept` this delivery and have the '
                     'lock reviewed.\n' % ', '.join(stops))
    elif any(changes[k] for k in changes):
        stream.write('compatible: everything the last release promised is '
                     'still here.\n')
    else:
        stream.write('identical to the lock.\n')


# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest='command')
    for name, text in (('check', 'compare a delivery against the lock'),
                       ('accept', 'write the lock from a delivery')):
        command = sub.add_parser(name, help=text)
        command.add_argument('--delivery', required=True,
                             help='output/: libddk and the halves')
        command.add_argument('--lock', required=True)
        command.add_argument('--symbols', required=True)
        command.add_argument('--nm', required=True, help='the pinned llvm-nm')
        command.add_argument('--layout', default='',
                             help='PART=DIR,...: where each part sits in the '
                                  'delivery')
    sub.choices['check'].add_argument('--report',
                                      help='write the changes as JSON')

    args = parser.parse_args()
    try:
        new, new_symbols = snapshot(os.path.abspath(args.delivery), args.nm,
                                    layout_.parse(args.layout))
        if args.command == 'accept':
            write_lock(new, new_symbols, args.lock, args.symbols)
            print('lock written from release %s: %d file(s), %d librar(y/ies) '
                  'exporting %d symbol(s), %d module(s) -> %s'
                  % (new['release'], len(new['files']), len(new['libraries']),
                     sum(len(v) for v in new_symbols.values()),
                     len(new['modules']), args.lock))
            return 0
        if args.command == 'check':
            old, old_symbols = read_lock(args.lock, args.symbols)
            changes = compare(old, old_symbols, new, new_symbols)
            if args.report:
                with io.open(args.report, 'w', encoding='utf-8') as handle:
                    handle.write(_text(json.dumps(changes, indent=1,
                                                  sort_keys=True) + '\n'))
            summarize(changes, old, new, sys.stdout)
            return 1 if blocking(changes) else 0
    except (LockError, layout_.LayoutError) as error:
        sys.stderr.write('error: %s\n' % error)
        return 2
    parser.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())

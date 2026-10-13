#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Records the kernel ABI a component was built against.

libddk is released to MTK every so often; Zircon keeps moving in between.  A
syscall inserted or reordered renumbers everything after it, and a driver
built against the old numbers then calls the wrong kernel entry -- while the
headers the customer holds look exactly as they did.  Nothing in the delivery
said which ABI it was built for, so nothing could tell that apart from a bug.

Two files go into the component, and they are deliberately not one:

  ABI.fingerprint   one line.  Compare it against the kernel in the field and
                    you know whether the ABI moved at all.

  SYSCALLS.json     one digest per syscall, keyed by name.  Says *which*
                    syscall moved.

Both are plaintext, and nothing else is written.  The names and the full
prototypes ship anyway, in sysroot/include/zircon/syscalls/definitions.h and
in libzircon.so's dynamic symbols -- a driver cannot call what it cannot
declare -- so a digest of them gives nothing away.  The numbers themselves
are not in the delivery at all: BUILD_INFO.json names the zircon revision,
and syscalls.abigen at that revision is the numbering.  A record of them used
to travel here encrypted to a certificate; the private key for it was lost
before the second release, and a record only its holder can read, held by
nobody, is worse than none.
"""

from __future__ import print_function

import argparse
import errno
import hashlib
import json
import os
import re
import sys

FORMAT_VERSION = 1
FINGERPRINT_FILE = 'ABI.fingerprint'
SYSCALLS_FILE = 'SYSCALLS.json'

# `syscall <name> [attributes]` opens an entry; it runs to the `;`.
SYSCALL_START = re.compile(r'^syscall\s+([a-z_][a-z0-9_]*)\s*(.*)$')
ORDINAL = re.compile(r'^#define\s+ZX_SYS_([a-z_][a-z0-9_]*)\s+(\d+)\s*$', re.M)


class AbiError(Exception):
    """An ABI that could not be read, or a tool that could not be run."""


def _fail(message):
    raise AbiError(message)


def _makedirs(path):
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise


def read_abigen(path):
    """Every syscall in an abigen file, as name -> normalised declaration.

    Normalised so that reformatting is not mistaken for an ABI change and a
    real change is not hidden by one: comments go, runs of whitespace become
    single spaces, and the entry is joined onto one line.
    """
    with open(path, 'rb') as handle:
        text = handle.read().decode('utf-8', 'replace')

    syscalls = {}
    name = None
    parts = []
    for line in text.split('\n'):
        line = line.split('#', 1)[0] if line.lstrip().startswith('#') else line
        stripped = line.strip()
        if name is None:
            match = SYSCALL_START.match(stripped)
            if not match:
                continue
            name = match.group(1)
            parts = [match.group(2)]
        else:
            parts.append(stripped)
        if stripped.endswith(';'):
            declaration = re.sub(r'\s+', ' ', ' '.join(parts)).strip()
            if name in syscalls:
                _fail('%s: %s is declared twice' % (path, name))
            syscalls[name] = declaration.rstrip(';').strip()
            name = None
    if name is not None:
        _fail('%s: declaration of %s is not terminated' % (path, name))
    if not syscalls:
        _fail('%s: no syscall declarations found' % path)
    return syscalls


def read_ordinals(path):
    """The syscall numbers, as name -> ordinal."""
    with open(path, 'rb') as handle:
        text = handle.read().decode('utf-8', 'replace')
    numbers = {}
    for match in ORDINAL.finditer(text):
        name, value = match.group(1), int(match.group(2))
        if name == 'COUNT':
            continue
        numbers[name] = value
    if not numbers:
        _fail('%s: no ZX_SYS_ definitions found' % path)
    return numbers


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def build(syscalls, ordinals):
    """The plaintext record, and the fingerprint over the whole ABI."""
    declared = set(syscalls)
    numbered = set(ordinals)
    # A syscall may be declared and not numbered -- vdsocall entries are
    # resolved in userspace and never enter the kernel -- but a number with
    # no declaration means the two files disagree about what the ABI is.
    orphans = sorted(numbered - declared)
    if orphans:
        _fail('numbered but not declared, so the two inputs describe '
              'different builds: %s' % ', '.join(orphans[:10]))

    entries = {}
    for name in sorted(declared):
        entries[name] = {
            'digest': digest(syscalls[name]),
            'numbered': name in numbered,
        }

    # The fingerprint covers the ordinals as well as the declarations:
    # reordering two syscalls without touching either one is exactly the
    # change that breaks a prebuilt library, and it leaves every declaration
    # digest untouched.
    canonical = json.dumps({
        'syscalls': dict((n, entries[n]['digest']) for n in entries),
        'ordinals': ordinals,
    }, sort_keys=True, separators=(',', ':'))

    return {
        'format_version': FORMAT_VERSION,
        'count': len(entries),
        'numbered': len(numbered),
        'syscalls': entries,
    }, digest(canonical)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--abigen', required=True,
                        help='syscalls.abigen, the authoritative declarations')
    parser.add_argument('--ordinals', required=True,
                        help='zx-syscall-numbers.h, generated by abigen')
    parser.add_argument('--out', required=True,
                        help='directory to write the two files into')
    parser.add_argument('--stamp', help='file to touch on success')
    args = parser.parse_args()

    try:
        syscalls = read_abigen(args.abigen)
        ordinals = read_ordinals(args.ordinals)
        record, fingerprint = build(syscalls, ordinals)

        _makedirs(args.out)
        with open(os.path.join(args.out, SYSCALLS_FILE), 'w') as handle:
            json.dump(record, handle, indent=2, sort_keys=True,
                      separators=(',', ': '))
            handle.write('\n')
        with open(os.path.join(args.out, FINGERPRINT_FILE), 'w') as handle:
            handle.write('%s\n' % fingerprint)

        if args.stamp:
            with open(args.stamp, 'w') as handle:
                handle.write('')
        sys.stdout.write(
            'abi: %d syscalls, %d numbered, fingerprint %s\n'
            % (record['count'], record['numbered'], fingerprint[:16]))
    except AbiError as error:
        sys.stderr.write('error: %s\n' % error)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Exports the compiler a libddk consumer needs, and nothing else.

libddk ships headers and binaries; it does not ship the thing that turns one
into the other.  The archives in it were produced by a specific clang, their
C++ symbols carry that clang's libc++ ABI namespace, and a consumer building
with a different one gets undefined references across the whole delivery.  So
the compiler is part of the contract, and a delivery that leaves it out is
asking the customer to guess.

The prebuilt is 1.4 GB and almost none of it is reachable from a DDK build.
Five *copies* of the clang driver at 78 MB each, three of lld, an x86_64
runtime nothing here targets, and two dozen tools -- clangd, clang-tidy, opt,
llc -- that no rule in selftest.mk invokes.  What is left after keeping only
what the build touches is 168 MB.

The keep set is declared here rather than measured, and that is the one place
this differs from the trim.  A compiler decides what to run from argv[0] and
from its own installation layout, so "what did this build execute" is not the
same question as "what will the next build execute": clang finds ld.lld by
looking beside itself, and a driver that took the integrated assembler path
today will fork one tomorrow.  Declaring the set and then proving it by
rebuilding the whole DDK against it is the check that actually holds.

The export is a subdirectory of the component, not a package beside it:
libddk/toolchain/.  One delivery, one NOTICE, one manifest.  That puts it
inside reach of two passes that must not touch it, which is why it is
installed after the trim has finished -- redact_source_paths rewrites strings
in place inside every .a and .so it finds, and libc++.so is one; a compiler
with rewritten strings is a compiler nobody can debug and possibly one that
does not run.  The trim also drops whatever no build measured, and no build
measures the compiler it ran.

Two things here are deliberately not what the trim does:

  Host binaries are verified, not stripped.  The pinned objcopy is
  aarch64-elf-objcopy -- it cannot read an x86-64 ELF at all, and the release
  rule is that no host tool does the work.  The prebuilt's host binaries ship
  stripped already, so there is nothing to do; the gate still reads every one
  of them and fails if that ever stops being true.

  The source-path gate has an allow list.  clang has the names of system
  headers compiled into it -- sys/types.h, arpa/inet.h, objc/runtime.h -- and
  those are the compiler knowing what a header is called, not a build path
  leaking.  Bare names only: anything with a directory component that is not
  on the list is still a failure.
"""

from __future__ import print_function

import argparse
import errno
import hashlib
import json
import os
import shutil
import stat
import struct
import sys

import trim_ddk

FORMAT_VERSION = 1
INFO_NAME = 'TOOLCHAIN_INFO.json'

EM_AARCH64 = 183
EM_X86_64 = 62

HOST_LIB_DIR = 'lib/host'

# What the host tools may ask the loader for and not get from us.  These are
# glibc and libgcc: every Linux has them, they must match the loader that is
# already running, and shipping them would be shipping a second C library.
# Everything else the tools need travels with them -- see collect_host_libs.
HOST_BASELINE = frozenset([
    'ld-linux-x86-64.so.2',
    'libc.so.6',
    'libdl.so.2',
    'libgcc_s.so.1',
    'libm.so.6',
    'libpthread.so.0',
    'librt.so.1',
])

# What a DDK build reaches, by the name the build uses it under.
#
# Each driver is one real file plus the names clang answers to: the driver
# picks C or C++ from argv[0], so clang++ has to exist, and it is a symlink
# here where the prebuilt has a second 78 MB copy.  llvm-ranlib is the same
# binary as llvm-ar reading its own name.  ld.lld is not invoked by any rule;
# clang forks it, by that name, from beside itself.
DRIVERS = [
    ('bin/clang', ['bin/clang++', 'bin/clang-7.0']),
    ('bin/ld.lld', ['bin/lld']),
    ('bin/llvm-ar', ['bin/llvm-ranlib']),
]

# Whole directories, spelled against the toolchain root.  {version} is the
# clang release directory, discovered rather than written down so a toolchain
# bump cannot quietly produce an empty tree; {target} is the triple the export
# is for, which is declared on the command line -- the prebuilt carries an
# x86_64 runtime beside the aarch64 one and keeping "whatever is there" ships
# both.
TREES = [
    'include/c++/v1',                  # libc++ headers
    'lib/clang/{version}/include',     # clang's own builtin headers
    'lib/{target}',                    # libc++.so, libc++abi.so, libunwind.so
]

# Dropped out of the trees above unless sanitizers are asked for.  Two thirds
# of the target runtime by size, and no DDK link reaches it: the sealed
# build's own link traces name libc++, libc++abi, libunwind and five members
# of libclang_rt.builtins, and nothing else.
SANITIZER_SUBTREES = ['lib/{target}/lib/asan']

# The compiler runtime, target architecture only.  A filter and not a tree,
# for the same reason: the directory holds both architectures side by side.
RUNTIME_DIR = 'lib/clang/{version}/lib/fuchsia'

# What of that runtime a plain build needs.  Measured, not guessed -- see
# SANITIZER_SUBTREES.  --with-sanitizers keeps every file for the target
# architecture instead.
RUNTIME_PLAIN = ['builtins']

# System header names clang carries as data.  Bare names only; see the module
# docstring.  Kept sorted and exact -- a substring rule here would let a real
# path through under a name that happens to end the same way.
BUILTIN_HEADER_NAMES = frozenset([
    'Foundation/NSObjCRuntime.h',
    'arpa/inet.h',
    'netinet/tcp.h',
    'objc/message.h',
    'objc/objc-auto.h',
    'objc/objc-exception.h',
    'objc/objc-sync.h',
    'objc/runtime.h',
    'sys/queue.h',
    'sys/resource.h',
    'sys/statvfs.h',
    'sys/times.h',
    'sys/types.h',
    'sys/utsname.h',
])


class ToolchainError(Exception):
    """An export that would not build, or would ship more than it should."""


def _fail(message):
    raise ToolchainError(message)


def _makedirs(path):
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise


# ---------------------------------------------------------------------------
# reading the prebuilt


def discover(source):
    """The clang release and target triple this prebuilt is laid out under.

    Read from the tree instead of taken as an argument.  The version appears
    in a path the runtime libraries live under, and getting it wrong produces
    an export that is missing the compiler runtime and links anyway -- until
    a consumer's build needs __aarch64_ldadd8_acq_rel and does not.
    """
    releases = os.path.join(source, 'lib', 'clang')
    if not os.path.isdir(releases):
        _fail('not a clang toolchain: %s has no lib/clang' % source)
    versions = sorted(name for name in os.listdir(releases)
                      if os.path.isdir(os.path.join(releases, name)))
    if len(versions) != 1:
        _fail('expected exactly one clang release under %s, found %s'
              % (releases, ', '.join(versions) or 'none'))

    lib = os.path.join(source, 'lib')
    targets = sorted(name for name in os.listdir(lib)
                     if os.path.isdir(os.path.join(lib, name, 'lib')))
    return versions[0], targets


def stamp_of(source):
    """The CIPD stamp beside the toolchain, or None if it is not there.

    zircon/prebuilt/config.mk pins the toolchain by this hash, and it is the
    only thing in the tree that says which build of clang this is.  Recording
    it is what lets a consumer's failure be traced to a toolchain change
    rather than to an ABI one.
    """
    for candidate in (source + '.stamp',
                      os.path.join(os.path.dirname(source), 'clang.stamp')):
        if os.path.isfile(candidate):
            with open(candidate, 'rb') as handle:
                value = handle.read().decode('ascii', 'replace').strip()
            if value:
                return value
    return None


def elf_machine(path):
    """The architecture of an ELF file or an archive, or None for neither.

    Read here rather than asked of a tool, for the same reason the trim reads
    section names itself: the decision this drives is which stripper may touch
    a file, and handing an x86-64 binary to aarch64-elf-objcopy gets "Unable
    to recognise the format of the input file" -- which is the good case.  The
    bad case is a stripper that recognises it, does nothing and exits zero.

    Archives count, and getting that wrong is how two of them shipped with
    DWARF still in: an earlier version of this read only the ELF magic, so
    libc++experimental.a and libclang_rt.builtins-aarch64.a were neither
    stripped nor checked, and the report said "stripped 8 target binaries"
    while meaning eight of the ten.  The trim's own gate, which reads
    archives, is what caught it.
    """
    with open(path, 'rb') as handle:
        head = handle.read(8)
        if head.startswith(b'!<arch>'):
            data = handle.read()
            for member in trim_ddk._archive_members(b'!<arch>\n' + data):
                machine = _elf_machine(member)
                if machine is not None:
                    return machine
            return None
        return _elf_machine(head + handle.read(12))


def _elf_machine(data):
    """The e_machine of one ELF image, or None if it is not one."""
    if len(data) < 20 or not data.startswith(b'\x7fELF'):
        return None
    elf_data, = struct.unpack_from('B', data, 5)
    endian = '<' if elf_data == 1 else '>'
    machine, = struct.unpack_from(endian + 'H', data, 18)
    return machine


def needed_libraries(path):
    """The shared libraries a host binary asks the loader for.

    Recorded because the prebuilt is not self-contained and nothing says so:
    ld.lld links against libxml2.so.2, and on a host that has moved on to
    libxml2.so.16 the whole DDK build dies at the first link with "error while
    loading shared libraries".  The CI image is ubuntu:focal and carries the
    right soname, so this never fails there -- which is exactly why it has to
    be written into the export rather than discovered by the customer.
    """
    with open(path, 'rb') as handle:
        data = handle.read()
    if len(data) < 64 or not data.startswith(b'\x7fELF'):
        return []
    elf_class, elf_data = struct.unpack_from('BB', data, 4)
    if elf_class != 2:
        return []
    endian = '<' if elf_data == 1 else '>'
    phoff, = struct.unpack_from(endian + 'Q', data, 0x20)
    phentsize, phnum = struct.unpack_from(endian + 'HH', data, 0x36)

    dynamic = None
    for index in range(phnum):
        header = phoff + index * phentsize
        p_type, = struct.unpack_from(endian + 'I', data, header)
        if p_type == 2:  # PT_DYNAMIC
            p_offset, = struct.unpack_from(endian + 'Q', data, header + 0x08)
            p_filesz, = struct.unpack_from(endian + 'Q', data, header + 0x20)
            dynamic = (p_offset, p_filesz)
            break
    if dynamic is None:
        return []

    # DT_STRTAB is a virtual address; map it back through the load segments
    # rather than assuming it equals its file offset, which is true for these
    # binaries and not true in general.
    loads = []
    for index in range(phnum):
        header = phoff + index * phentsize
        p_type, = struct.unpack_from(endian + 'I', data, header)
        if p_type != 1:  # PT_LOAD
            continue
        p_offset, = struct.unpack_from(endian + 'Q', data, header + 0x08)
        p_vaddr, = struct.unpack_from(endian + 'Q', data, header + 0x10)
        p_filesz, = struct.unpack_from(endian + 'Q', data, header + 0x20)
        loads.append((p_vaddr, p_filesz, p_offset))

    def to_offset(vaddr):
        for base, size, offset in loads:
            if base <= vaddr < base + size:
                return offset + (vaddr - base)
        return None

    offset, size = dynamic
    strtab = None
    entries = []
    for pos in range(offset, offset + size, 16):
        tag, value = struct.unpack_from(endian + 'QQ', data, pos)
        if tag == 0:  # DT_NULL
            break
        if tag == 5:  # DT_STRTAB
            strtab = to_offset(value)
        elif tag == 1:  # DT_NEEDED
            entries.append(value)
    if strtab is None:
        return []
    found = []
    for name_off in entries:
        end = data.find(b'\0', strtab + name_off)
        found.append(data[strtab + name_off:end].decode('ascii', 'replace'))
    return sorted(set(found))


# ---------------------------------------------------------------------------
# the libraries the host tools need


def collect_host_libs(dest, search_dirs, baseline=HOST_BASELINE):
    """Copies in every non-baseline library the host tools ask for.

    ld.lld is dynamically linked against libxml2.so.2 -- for COFF manifests,
    which no ELF link here touches -- and a machine whose libxml2 has moved on
    to another soname cannot run it at all.  That is not a note for a README:
    the delivery either carries what it needs or it does not work, and 200
    files compile before the first link says so.

    Taken only from `search_dirs`, which are pinned trees in the checkout --
    the sysroot buildtools ships.  Never from the machine running this: a
    delivery assembled out of whatever happened to be installed is a delivery
    that works here and nowhere else, and there would be nothing to say which
    version went in.
    """
    if not search_dirs:
        _fail('no --host-libs directory given, so the tools would have to '
              'take their libraries from this machine')
    for directory in search_dirs:
        if not os.path.isdir(directory):
            _fail('not a directory: %s' % directory)

    def locate(soname):
        for directory in search_dirs:
            candidate = os.path.join(directory, soname)
            if os.path.exists(candidate):
                return os.path.realpath(candidate)
        return None

    out_dir = os.path.join(dest, HOST_LIB_DIR)
    _makedirs(out_dir)

    pending = [os.path.join(dest, driver) for driver, _ in DRIVERS
               if os.path.isfile(os.path.join(dest, driver))]
    shipped = {}
    missing = []
    seen = set()
    while pending:
        for soname in needed_libraries(pending.pop()):
            if soname in baseline or soname in seen:
                continue
            seen.add(soname)
            found = locate(soname)
            if found is None:
                missing.append(soname)
                continue
            # Installed under the soname the loader will ask for, not under
            # the versioned filename it resolves to on the machine we took
            # it from.
            target = os.path.join(out_dir, soname)
            shutil.copyfile(found, target)
            shutil.copystat(found, target)
            shipped[soname] = os.path.getsize(target)
            pending.append(target)
    if missing:
        _fail('%d librar(y/ies) the host tools need are in none of the pinned '
              'directories given: %s. Taking them from this machine instead '
              'is what --host-libs exists to prevent.'
              % (len(missing), ', '.join(sorted(missing))))
    return shipped


# ---------------------------------------------------------------------------
# the keep set


def plan(source, version, target, sanitizers=False):
    """What the export contains: real files, and the names that alias them."""
    files = []
    links = []

    for driver, aliases in DRIVERS:
        path = os.path.join(source, driver)
        if not os.path.isfile(path):
            _fail('the toolchain has no %s' % driver)
        files.append(driver)
        for alias in aliases:
            links.append((alias, os.path.basename(driver)))

    excluded = tuple(
        spelling.format(version=version, target=target) + '/'
        for spelling in (SANITIZER_SUBTREES if not sanitizers else []))
    for spelling in TREES:
        relative = spelling.format(version=version, target=target)
        root = os.path.join(source, relative)
        if not os.path.isdir(root):
            _fail('the toolchain has no %s' % relative)
        for current, dirs, names in os.walk(root):
            dirs.sort()
            for name in sorted(names):
                found = os.path.relpath(os.path.join(current, name),
                                        source).replace(os.sep, '/')
                if found.startswith(excluded):
                    continue
                files.append(found)

    runtime = RUNTIME_DIR.format(version=version)
    root = os.path.join(source, runtime)
    if not os.path.isdir(root):
        _fail('the toolchain has no %s' % runtime)
    arch = target.split('-')[0]
    kept = [name for name in sorted(os.listdir(root))
            if ('-' + arch) in name
            and (sanitizers or any(('.' + part + '-') in name
                                   for part in RUNTIME_PLAIN))]
    if not kept:
        _fail('no %s runtime under %s' % (arch, runtime))
    files.extend(runtime + '/' + name for name in kept)

    return sorted(set(files)), sorted(set(links))


def copy(source, dest, files, links):
    """Lays the keep set down, with the aliases as symlinks."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    for relative in files:
        target = os.path.join(dest, relative)
        _makedirs(os.path.dirname(target))
        origin = os.path.join(source, relative)
        if os.path.islink(origin):
            os.symlink(os.readlink(origin), target)
            continue
        shutil.copyfile(origin, target)
        shutil.copystat(origin, target)
    for alias, points_at in links:
        target = os.path.join(dest, alias)
        _makedirs(os.path.dirname(target))
        if os.path.lexists(target):
            os.remove(target)
        os.symlink(points_at, target)


# ---------------------------------------------------------------------------
# stripping and the gates


def strip_target_binaries(dest, objcopy):
    """Strips the target-architecture libraries; leaves host binaries alone.

    Nineteen of the twenty-two ELF files here carry DWARF -- libc++.so,
    libc++abi.so, libunwind.so and the compiler-rt archives are all built with
    debug information -- and all nineteen are aarch64, which the pinned
    objcopy reads.  The host binaries are the other three, and they ship
    stripped; check_stripped below is what holds that.
    """
    if not os.path.isfile(objcopy):
        _fail('not a file: %s' % objcopy)
    before = after = 0
    stripped = []
    for relative in sorted(_walk(dest)):
        path = os.path.join(dest, relative)
        machine = elf_machine(path)
        if machine is None or machine == EM_X86_64:
            continue
        before += os.path.getsize(path)
        # Read-only in the prebuilt; objcopy writes in place.
        os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
        trim_ddk._run([objcopy, '--strip-debug', path], path)
        after += os.path.getsize(path)
        stripped.append(relative)
    return {'binaries': len(stripped), 'before': before, 'after': after,
            'files': stripped}


def check_stripped(dest):
    """Every ELF file in the export, and the debug sections it must not have.

    Runs over host binaries too, which nothing here strips.  That is the
    point: the export relies on the prebuilt already being stripped, and this
    is the only thing that would notice if a future one is not.
    """
    offenders = []
    for relative in sorted(_walk(dest)):
        path = os.path.join(dest, relative)
        if elf_machine(path) is None:
            continue
        sections = trim_ddk.debug_sections(path)
        if sections:
            offenders.append((relative, len(sections)))
    return offenders


def check_redacted(dest, allowed=BUILTIN_HEADER_NAMES):
    """Source paths a binary carries, minus the ones clang legitimately has.

    Same regex the trim uses, so the two cannot drift.  Only the allow list is
    different, and it is exact names: a path that merely ends in sys/types.h
    is not sys/types.h.
    """
    offenders = []
    for relative in sorted(_walk(dest)):
        path = os.path.join(dest, relative)
        if elf_machine(path) is None:
            continue
        with open(path, 'rb') as handle:
            data = handle.read()
        found = set()
        for match in trim_ddk.SOURCE_PATH.finditer(data):
            name = match.group(1).decode('ascii', 'replace')
            if name not in allowed:
                found.add(name)
        if found:
            offenders.append((relative, sorted(found)))
    return offenders


def _walk(root):
    for current, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(current, name)
            if os.path.islink(path):
                continue
            yield os.path.relpath(path, root).replace(os.sep, '/')


# ---------------------------------------------------------------------------
# what the export says about itself


def describe(dest, source, version, target, stamp, source_date=None,
             source_root=None):
    """The record that travels with the export.

    `source` is written relative to the checkout, never absolutely.  A record
    that ships to a customer is not the place for
    /home/<somebody>/data/ddk/grpower/workspace/nebula -- the trim spends a
    whole pass cutting exactly those out of the binaries, and leaving one in
    the metadata beside them would undo it.
    """
    hosts = []
    requirements = set()
    for driver, _ in DRIVERS:
        path = os.path.join(dest, driver)
        if not os.path.isfile(path):
            continue
        hosts.append(driver)
        requirements.update(needed_libraries(path))
    # What the delivery still expects to find on the machine.  Anything it
    # carries itself is not a requirement, and saying so was how a customer
    # was left to discover libxml2.so.2 at the first link.
    bundled_dir = os.path.join(dest, HOST_LIB_DIR)
    if os.path.isdir(bundled_dir):
        requirements -= set(os.listdir(bundled_dir))

    digest = hashlib.sha256()
    count = 0
    total = 0
    for relative in sorted(_walk(dest)):
        if relative == INFO_NAME:
            continue
        path = os.path.join(dest, relative)
        digest.update(relative.encode('utf-8'))
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b''):
                digest.update(chunk)
        count += 1
        total += os.path.getsize(path)

    where = source
    if source_root:
        relative = os.path.relpath(source, os.path.abspath(source_root))
        # Only when it really is inside the checkout: a ../../.. chain names
        # the host layout just as well as an absolute path does.
        where = relative if not relative.startswith('..') else '(external)'

    info = {
        'format_version': FORMAT_VERSION,
        'clang_release': version,
        'target': target,
        'prebuilt_stamp': stamp,
        'source': where,
        'files': count,
        'bytes': total,
        'fingerprint': digest.hexdigest()[:16],
        'host_tools': sorted(hosts),
        'host_requirements': sorted(requirements),
    }
    if source_date is not None:
        info['source_date'] = source_date
    return info


def write_info(dest, info):
    path = os.path.join(dest, INFO_NAME)
    with open(path, 'wb') as handle:
        handle.write(json.dumps(info, indent=2, sort_keys=True,
                                separators=(',', ': ')).encode('utf-8'))
        handle.write(b'\n')
    return path


# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True,
                        help='the prebuilt clang to export from')
    parser.add_argument('--out', required=True, help='where to write it')
    parser.add_argument('--target', required=True,
                        help='the triple this export is for, e.g. '
                             'aarch64-fuchsia. Declared rather than '
                             'discovered: the prebuilt carries more than one.')
    parser.add_argument('--host-libs', action='append', default=[],
                        help='a pinned directory to take the host tools\' '
                             'own shared libraries from, such as the sysroot '
                             'buildtools ships. Repeatable, and required: '
                             'without one the delivery would depend on '
                             'whatever this machine happens to have.')
    parser.add_argument('--no-host-libs', action='store_true',
                        help='ship none, leaving the delivery dependent on '
                             'the machine that builds it')
    parser.add_argument('--with-sanitizers', action='store_true',
                        help='also keep the sanitizer runtimes, which no '
                             'plain DDK build links')
    parser.add_argument('--objcopy',
                        help='the pinned aarch64 objcopy; required unless '
                             '--no-strip')
    parser.add_argument('--no-strip', action='store_true',
                        help='skip stripping (the gate still runs)')
    parser.add_argument('--source-date', type=int,
                        help='SOURCE_DATE_EPOCH, recorded in %s' % INFO_NAME)
    parser.add_argument('--source-root',
                        help='the checkout, so the record names the prebuilt '
                             'relatively instead of by host path')
    args = parser.parse_args()

    try:
        source = os.path.abspath(args.source)
        dest = os.path.abspath(args.out)
        version, targets = discover(source)
        if args.target not in targets:
            _fail('%s has no %s runtime; it carries %s'
                  % (source, args.target, ', '.join(targets) or 'none'))
        stamp = stamp_of(source)

        files, links = plan(source, version, args.target,
                            args.with_sanitizers)
        copy(source, dest, files, links)
        print('  kept %d files + %d aliases from %s'
              % (len(files), len(links), source))

        bundled = {}
        if not args.no_host_libs:
            bundled = collect_host_libs(dest, args.host_libs)
            print('    bundled %d host librar(y/ies): %s'
                  % (len(bundled), ', '.join(sorted(bundled))))

        if not args.no_strip:
            if not args.objcopy:
                _fail('--objcopy is required; pass --no-strip to skip')
            result = strip_target_binaries(dest, args.objcopy)
            print('    stripped %d target binaries, %d -> %d bytes'
                  % (result['binaries'], result['before'], result['after']))

        offenders = check_stripped(dest)
        if offenders:
            _fail('%d binary(ies) still carry debug information:\n  %s'
                  % (len(offenders),
                     '\n  '.join('%s (%d sections)' % item
                                 for item in offenders[:20])))

        offenders = check_redacted(dest)
        if offenders:
            _fail('%d binary(ies) carry a source path that is not a known '
                  'builtin header name:\n  %s'
                  % (len(offenders),
                     '\n  '.join('%s: %s' % (path, ', '.join(names[:6]))
                                 for path, names in offenders[:20])))

        info = describe(dest, source, version, args.target, stamp,
                        args.source_date, args.source_root)
        info['sanitizers'] = bool(args.with_sanitizers)
        info['bundled_libraries'] = sorted(bundled)
        write_info(dest, info)
        print('    clang %s, target %s, stamp %s'
              % (info['clang_release'], info['target'],
                 info['prebuilt_stamp'] or 'unknown'))
        print('    host requirements: %s'
              % ', '.join(info['host_requirements']))
        print('  toolchain: %d files, %d bytes, fingerprint %s -> %s'
              % (info['files'], info['bytes'], info['fingerprint'], dest))
    except (ToolchainError, trim_ddk.TrimError) as error:
        sys.stderr.write('error: %s\n' % error)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

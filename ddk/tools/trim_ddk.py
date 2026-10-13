#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Trims a DDK component to what a delivery actually needs.

The component the build produces is what libddk *may* contain.  What ships to
a given customer is narrower: a file nobody compiles or links against still
names our functions, our structures and our internal paths, and every one of
those is a free hint to somebody reading the delivery.  Trimming is how the
two stop being the same thing.

The keep set is built from three sources, in this order:

  measured   the files the compiler read and the linker opened, taken from
             the sealed self-test's closure.  Evidence, not opinion.
  declared   the libraries the module descriptions name.  A linker's trace
             reports the archives it took something from, not the ones it was
             pointed at, so an archive nobody needs a symbol from still has
             to be there: -lvirtio fails on a missing file, not on an unused
             one.
  reachable  everything a kept header includes, followed to a fixed point.
             A build that takes an #ifdef branch this one did not would
             otherwise fail at the customer, which is the worst place to
             learn it.  Include lines are read textually, so a guarded
             include counts even when it was not taken.
  policy     files kept by declaration, with a reason.  A component has
             entry points nothing includes yet, and licences that must
             travel whatever the compiler thinks.

Everything else is dropped, and the report says what and why, so the trim is
a reviewed decision rather than a silent one.

Trimming is deliberately not part of the component's own build.  What may
ship is declared; what does ship is derived from measurement and has to be
proved by rebuilding against the trimmed tree.  Keeping the two apart is what
makes the second check meaningful.
"""

from __future__ import print_function

import argparse
import collections
import errno
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys

FORMAT_VERSION = 1
CONTENTS_NAME = 'SDK_CONTENTS.json'
NOTICE_NAME = 'NOTICE'
BUILD_INFO_NAME = 'BUILD_INFO.json'
LICENSES_DIR = 'LICENSES'

HEADER_SUFFIXES = ('.h', '.hh', '.hpp', '.hxx', '.inc', '.inl', '.ipp',
                   '.def', '.defs')

INCLUDE_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)
MODULE_SUFFIX = '.ddk.mk'
LIB_FIELD_RE = re.compile(r'^MODULE_LIBS\s*:?=\s*(.*)$', re.M)

try:
    STRING_TYPES = (str, unicode)  # noqa: F821  (python 2)
except NameError:
    STRING_TYPES = (str,)


class TrimError(Exception):
    """A trim that would remove something the delivery needs."""


def _fail(message):
    raise TrimError(message)


def _makedirs(path):
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise


def _is_clean_relative(path):
    if not isinstance(path, STRING_TYPES) or not path:
        return False
    if '\\' in path or path.startswith('/'):
        return False
    return all(part not in ('', '.', '..') for part in path.split('/'))


# ---------------------------------------------------------------------------
# inputs


def read_component(component):
    """Every file in a component, relative to its root."""
    files = set()
    for current, dirs, names in os.walk(component):
        dirs.sort()
        for name in sorted(names):
            relative = os.path.relpath(os.path.join(current, name),
                                       component).replace(os.sep, '/')
            files.add(relative)
    if CONTENTS_NAME not in files:
        _fail('%s is not a materialized component: no %s'
              % (component, CONTENTS_NAME))
    return files


def read_closures(paths):
    """The union of the measured closures, as component-relative paths."""
    measured = set()
    for path in paths:
        with open(path, 'r') as handle:
            closure = json.load(handle)
        entries = closure.get('libddk')
        if not isinstance(entries, list):
            _fail('%s: no `libddk` list; is this a self-test closure?' % path)
        for entry in entries:
            if not _is_clean_relative(entry):
                _fail('%s: bad path in closure: %r' % (path, entry))
            measured.add(entry)
    if not measured:
        _fail('the closures name no files; trimming against them would empty '
              'the component')
    return measured


def read_policy(path):
    """Files kept by declaration, each with a reason."""
    if path is None:
        return {'keep': [], 'reasons': {}}
    with open(path, 'r') as handle:
        policy = json.load(handle)
    if policy.get('format_version') != FORMAT_VERSION:
        _fail('%s: unsupported format_version %r'
              % (path, policy.get('format_version')))
    entries = policy.get('keep')
    if not isinstance(entries, list):
        _fail('%s: `keep` must be a list' % path)
    reasons = {}
    for entry in entries:
        if not isinstance(entry, dict):
            _fail('%s: each keep entry must be an object' % path)
        prefix = entry.get('prefix')
        if not _is_clean_relative(prefix):
            _fail('%s: invalid keep prefix %r' % (path, prefix))
        reason = entry.get('reason')
        if not isinstance(reason, STRING_TYPES) or not reason.strip():
            _fail('%s: keep %s needs a reason' % (path, prefix))
        reasons[prefix] = reason
    return {'keep': sorted(reasons), 'reasons': reasons}


def declared_libraries(source_roots):
    """Library names the module descriptions ask the linker for."""
    names = set()
    for root in source_roots:
        for current, dirs, files in os.walk(root):
            dirs.sort()
            for name in sorted(files):
                if not name.endswith(MODULE_SUFFIX):
                    continue
                with open(os.path.join(current, name), 'r') as handle:
                    text = handle.read()
                for line in LIB_FIELD_RE.findall(text):
                    names.update(line.split())
    return names


def library_files(names, files):
    """Every file in the component that answers -l<name>."""
    wanted = set()
    for name in names:
        for directory in ('lib', 'sysroot/lib'):
            for suffix in ('.a', '.so'):
                candidate = '%s/lib%s%s' % (directory, name, suffix)
                if candidate in files:
                    wanted.add(candidate)
    return wanted


def policy_matches(relative, prefixes):
    for prefix in prefixes:
        if relative == prefix or relative.startswith(prefix + '/'):
            return prefix
    return None


# ---------------------------------------------------------------------------
# reachability


def include_roots(files):
    """Directories an #include is resolved against, longest first.

    A component installs its headers under include/ and its sysroot's under
    sysroot/include/, and both are on the consumer's include path.
    """
    roots = []
    for candidate in ('include', 'sysroot/include'):
        if any(f.startswith(candidate + '/') for f in files):
            roots.append(candidate)
    return roots


def reachable_headers(component, seeds, files, roots):
    """Everything the seed headers include, to a fixed point.

    Textual: an include inside an #ifdef this build did not take is still an
    include, and a delivery that drops it works until the customer's
    configuration differs from ours.
    """
    index = set(files)
    pending = [s for s in seeds if s.endswith(HEADER_SUFFIXES)]
    seen = set(pending)
    added = set()
    while pending:
        current = pending.pop()
        directory = os.path.dirname(current)
        try:
            with open(os.path.join(component, current), 'rb') as fh:
                text = fh.read().decode('utf-8', 'replace')
        except (IOError, OSError):
            continue
        for spelling in INCLUDE_RE.findall(text):
            if not _is_clean_relative(spelling):
                continue
            candidates = [os.path.normpath(os.path.join(directory, spelling))
                          .replace(os.sep, '/')]
            candidates += ['%s/%s' % (root, spelling) for root in roots]
            for candidate in candidates:
                if candidate in index and candidate not in seen:
                    seen.add(candidate)
                    added.add(candidate)
                    pending.append(candidate)
                    break
    return added


# ---------------------------------------------------------------------------
# the trim


BINARY_SUFFIXES = ('.a', '.so', '.o')


def strip_debug(component, objcopy):
    """Removes debug information from the binaries a component ships.

    The archives are built with -g, so each one carries DWARF: function
    names, parameter names, structure layouts, line numbers and the source
    paths they came from.  libzx.a names eighteen Zircon sources and
    libnbl_fwk_hee.a thirteen of ours.  That is a far larger surface than the
    headers a trim removes, and a consumer needs none of it to link.

    The tool is named rather than assumed, and the pinned one to name is
    zircon/prebuilt/downloads/gcc/bin/aarch64-elf-objcopy: it reads an archive
    directly, and it is in the tree, so a release machine needs nothing
    installed.  The pinned *clang* of this vintage cannot be used -- LLVM 7's
    llvm-objcopy cannot open an archive at all, and stripping the members one
    at a time produces objects that llvm-nm reads happily and the linker then
    rejects with "invalid section index".  The trim's rebuild against the
    stripped component is what caught that; nothing cheaper did.

    What this costs: nobody can step into libddk with a debugger any more.
    That is the trade, and it is why this is asked for rather than assumed.
    """
    if not os.path.isfile(objcopy):
        _fail('not a file: %s' % objcopy)

    before = after = 0
    stripped = 0
    skipped = []
    for current, dirs, names in os.walk(component):
        dirs.sort()
        for name in sorted(names):
            if not name.endswith(BINARY_SUFFIXES):
                continue
            path = os.path.join(current, name)
            if _binary_kind(path) is None:
                # A .so that is not ELF: musl installs linker scripts under
                # libdl.so, libm.so, libpthread.so and librt.so, so that -ldl
                # and friends resolve to libc.  Nothing to strip, and skipping
                # by content rather than by name keeps a real failure loud.
                skipped.append(os.path.relpath(path, component))
                continue
            before += os.path.getsize(path)
            _run([objcopy, '--strip-debug', path], path)
            after += os.path.getsize(path)
            stripped += 1
    return {'binaries': stripped, 'before': before, 'after': after,
            'skipped': sorted(skipped)}


def _run(command, subject):
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    output, error = process.communicate()
    message = (error or output).decode('utf-8', 'replace').strip()
    if process.returncode != 0 or message:
        # GNU objcopy reports an unrecognised architecture on stderr and still
        # exits zero, leaving the file untouched.  Treat anything it says as a
        # failure: a strip that silently did nothing is the worst outcome.
        _fail('could not strip %s: %s' % (subject, message or 'exit %d'
                                          % process.returncode))


def debug_sections(path):
    """The names of any .debug_* sections in an ELF file or an archive.

    Read here rather than asked of a tool.  The point of the check is to
    catch a stripper that did not run, ran on the wrong architecture, or
    reported success and changed nothing -- all three have happened -- so it
    must not depend on the same tool it is checking.
    """
    with open(path, 'rb') as handle:
        data = handle.read()
    if data.startswith(b'!<arch>\n'):
        found = []
        for member in _archive_members(data):
            found.extend(_elf_debug_sections(member))
        return sorted(set(found))
    return sorted(set(_elf_debug_sections(data)))


def _archive_members(data):
    """Each member of a Unix archive, as bytes."""
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset:offset + 60]
        if header[58:60] != b'`\n':
            break
        try:
            size = int(header[48:58].decode('ascii').strip())
        except ValueError:
            break
        start = offset + 60
        yield data[start:start + size]
        offset = start + size + (size & 1)


def _elf_debug_sections(data):
    """The .debug_* section names of one ELF image, or nothing if not ELF."""
    if len(data) < 64 or not data.startswith(b'\x7fELF'):
        return []
    # struct rather than indexing: data[4] is an int on python 3 and a
    # one-character string on python 2.
    elf_class, elf_data = struct.unpack_from('BB', data, 4)
    if elf_class != 2:
        return []  # The delivery is 64-bit; anything else is not ours.
    endian = '<' if elf_data == 1 else '>'
    shoff, = struct.unpack_from(endian + 'Q', data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from(endian + 'HHH', data, 0x3a)
    if not shoff or not shnum or shstrndx >= shnum:
        return []
    strtab_header = shoff + shstrndx * shentsize
    str_off, = struct.unpack_from(endian + 'Q', data, strtab_header + 0x18)
    str_size, = struct.unpack_from(endian + 'Q', data, strtab_header + 0x20)
    names = data[str_off:str_off + str_size]
    found = []
    for index in range(shnum):
        header = shoff + index * shentsize
        name_off, = struct.unpack_from(endian + 'I', data, header)
        end = names.find(b'\0', name_off)
        name = names[name_off:end if end >= 0 else None]
        if name.startswith(b'.debug_'):
            found.append(name.decode('ascii', 'replace'))
    return found


# A __FILE__ baked into an assert message by the compiler.
#
# NUL-terminated on the right; on the left the preceding byte must not be
# printable, so the match is a whole string rather than the tail of a longer
# one -- "cannot open dir/file.c" is a message, not a path.  Anchoring on a
# preceding NUL is not enough: a string laid down straight after code is
# preceded by the last instruction, and fourteen protobuf paths survived that
# way into the linked binary while the archive looked clean.
#
# A directory is required: a bare "config.h" names nothing, and is likelier
# to be real data than a path.
SOURCE_PATH = re.compile(
    br'(?<![\x20-\x7e])((?:\.\./)*[A-Za-z_][A-Za-z0-9_.+-]*'
    br'(?:/[A-Za-z0-9_.+-]+)+\.(?:c|cc|cpp|cxx|h|hh|hpp|S))\x00')


def redact_source_paths(component):
    """Cuts the directory off every source path a binary carries.

    Stripping removes DWARF; it does not touch the strings the compiler put in
    .rodata for assert().  473 of those survive in this component, and 53 name
    our own files: zircon/system/ulib/fs/connection.cpp tells a reader the
    subsystem, the layout and the file, from a delivery that ships none of it.

    The basename is kept and the rest of the buffer filled with NUL, so the
    string gets shorter in place: every offset a relocation points at stays
    where it was, no section changes size, and an assert still says which file
    it fired in.  Rewriting the archives here rather than compiling them
    differently is deliberate -- half of them come out of Zircon's own make
    build, and changing how that compiles would change the product.
    """
    changed = collections.Counter()
    for current, dirs, names in os.walk(component):
        dirs.sort()
        for name in sorted(names):
            if not name.endswith(BINARY_SUFFIXES):
                continue
            path = os.path.join(current, name)
            if _binary_kind(path) is None:
                continue
            with open(path, 'rb') as handle:
                data = bytearray(handle.read())
            count = 0
            for match in SOURCE_PATH.finditer(bytes(data)):
                start, end = match.start(1), match.end(1)
                base = match.group(1).rsplit(b'/', 1)[-1]
                data[start:end] = base + b'\x00' * (end - start - len(base))
                count += 1
            if count:
                with open(path, 'wb') as handle:
                    handle.write(bytes(data))
                changed[os.path.relpath(path, component)] = count
    return changed


def check_redacted(component):
    """Every shipped binary still carrying a source path with a directory."""
    offenders = []
    for current, dirs, names in os.walk(component):
        dirs.sort()
        for name in sorted(names):
            if not name.endswith(BINARY_SUFFIXES):
                continue
            path = os.path.join(current, name)
            if _binary_kind(path) is None:
                continue
            with open(path, 'rb') as handle:
                found = SOURCE_PATH.findall(handle.read())
            if found:
                offenders.append((os.path.relpath(path, component),
                                  len(found)))
    return offenders


def check_stripped(component):
    """Every shipped binary, and the debug sections it should not have."""
    offenders = []
    for current, dirs, names in os.walk(component):
        dirs.sort()
        for name in sorted(names):
            if not name.endswith(BINARY_SUFFIXES):
                continue
            path = os.path.join(current, name)
            sections = debug_sections(path)
            if sections:
                offenders.append((os.path.relpath(path, component),
                                  len(sections)))
    return offenders


def _binary_kind(path):
    """'archive', 'elf', or None for a file that is neither."""
    with open(path, 'rb') as handle:
        magic = handle.read(8)
    if magic.startswith(b'!<arch>'):
        return 'archive'
    if magic.startswith(b'\x7fELF'):
        return 'elf'
    return None


def plan(component, closures, policy, module_sources=(), subset=False,
         keep_all=False):
    """What to keep, what to drop, and which rule decided each."""
    files = read_component(component)
    if keep_all:
        # A component that is already the closure of the half it goes to has
        # nothing to trim: it was derived from what that half reads and
        # links, not scraped from a build.  What it still needs is the rest
        # of this tool -- stripped of debug information, its source paths
        # redacted, and refused if either did not take.  Trimming it as well
        # would cut the headers its own headers include: the seeds of that
        # walk are the measured set, and the half's measurement answers for
        # two components while this is one of them.
        return files, dict((relative, 'whole') for relative in files), []
    measured = read_closures(closures)

    unknown = sorted(measured - files)
    if unknown and not subset:
        _fail('the closure names %d file(s) the component does not have; it '
              'was measured against a different component:\n  %s'
              % (len(unknown), '\n  '.join(unknown[:20])))
    # A half builds against two components -- the one beside it and the one
    # inside it -- and one closure measures both.  Trimming either alone,
    # what the other answered is not this one's to hold.
    measured -= set(unknown)

    kept_by = {}
    for relative in measured:
        kept_by[relative] = 'measured'

    for relative in library_files(declared_libraries(module_sources), files):
        kept_by.setdefault(relative, 'declared')

    roots = include_roots(files)
    for relative in reachable_headers(component, measured, files, roots):
        kept_by.setdefault(relative, 'reachable')

    for relative in sorted(files):
        prefix = policy_matches(relative, policy['keep'])
        if prefix is not None and relative not in kept_by:
            kept_by[relative] = 'policy:' + prefix

    # Delivery metadata, not content: the manifest, the notice and the
    # licence texts.  They are rewritten for the trimmed tree rather than
    # carried, but dropping them would put them in the report as if somebody
    # had decided the delivery did not need its licences.
    for relative in sorted(files):
        if (relative in (CONTENTS_NAME, NOTICE_NAME, BUILD_INFO_NAME)
                or relative.startswith(LICENSES_DIR + '/')):
            kept_by[relative] = 'policy:metadata'
    dropped = sorted(files - set(kept_by))
    return files, kept_by, dropped


def write(component, out, kept_by, dropped, policy):
    if os.path.exists(out):
        shutil.rmtree(out)
    _makedirs(out)

    with open(os.path.join(component, CONTENTS_NAME), 'r') as handle:
        contents = json.load(handle)

    for relative in sorted(kept_by):
        if relative == CONTENTS_NAME:
            continue
        source = os.path.join(component, relative)
        target = os.path.join(out, relative)
        _makedirs(os.path.dirname(target))
        shutil.copyfile(source, target)
        os.chmod(target, stat.S_IMODE(os.lstat(source).st_mode))

    kept_paths = set(kept_by)
    contents['files'] = [entry for entry in contents.get('files', [])
                         if entry.get('path') in kept_paths]
    contents['trimmed'] = True
    with open(os.path.join(out, CONTENTS_NAME), 'w') as handle:
        json.dump(contents, handle, indent=2, sort_keys=True,
                  separators=(',', ': '))
        handle.write('\n')

    reasons = {}
    for relative, rule in kept_by.items():
        reasons[rule] = reasons.get(rule, 0) + 1
    # Returned, not written into the component.  The report names all 337
    # files the trim removed -- the ACRN header held back for its licence,
    # every ddk/protocol/ header the delivery does not include, five archives
    # -- which is a map of what is there and was withheld, published by the
    # step whose whole purpose is to withhold it.  It belongs where the trim
    # is reviewed, which is here and not at the customer; --report says where.
    return {
        'format_version': FORMAT_VERSION,
        'kept': len(kept_by),
        'dropped': len(dropped),
        'kept_by_rule': reasons,
        'dropped_files': dropped,
        'policy': policy['reasons'],
    }


def summarize(files, kept_by, dropped, stream):
    def bucket(relative):
        parts = relative.split('/')
        return '/'.join(parts[:2]) if len(parts) > 2 else (
            parts[0] if len(parts) == 1 else '/'.join(parts[:1]))

    counts = {}
    for relative in dropped:
        key = bucket(relative)
        counts[key] = counts.get(key, 0) + 1
    stream.write('  component %d files, keeping %d, dropping %d\n'
                 % (len(files), len(kept_by), len(dropped)))
    by_rule = {}
    for rule in kept_by.values():
        key = rule.split(':')[0]
        by_rule[key] = by_rule.get(key, 0) + 1
    for rule in sorted(by_rule):
        stream.write('    kept by %-10s %5d\n' % (rule, by_rule[rule]))
    for key in sorted(counts, key=lambda k: -counts[k])[:15]:
        stream.write('    dropping %-28s %5d\n' % (key, counts[key]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', required=True,
                        help='materialized component to trim')
    parser.add_argument('--closure', action='append', default=[],
                        help='a sealed self-test closure (repeatable). '
                             'Required unless --keep-all, which keeps every '
                             'file and so has nothing to measure against.')
    parser.add_argument('--policy', help='files to keep by declaration')
    parser.add_argument('--module-sources', action='append', default=[],
                        help='a DDK source set, whose module descriptions name '
                             'the libraries the linker must be able to find '
                             '(repeatable)')
    parser.add_argument('--out', help='write the trimmed component here')
    parser.add_argument('--strip', metavar='OBJCOPY',
                        help='remove debug information from the binaries with '
                             'this objcopy. Nothing needs the DWARF to link, '
                             'and it names our sources, our functions and our '
                             'structures. Use the pinned '
                             'zircon/prebuilt/downloads/gcc/bin/'
                             'aarch64-elf-objcopy; the pinned clang of this '
                             'vintage corrupts what it writes')
    parser.add_argument('--keep-all', action='store_true',
                        help='keep every file, and strip and redact them. '
                             'For a component that is already what its half '
                             'needs, where a trim would only cut what the '
                             'closure of the other component answered for.')
    parser.add_argument('--subset', action='store_true',
                        help='this component is part of what the closure was '
                             'measured against, so a measured file it does '
                             'not have belongs to the other part rather than '
                             'meaning the closure is the wrong one')
    parser.add_argument('--report', required=True,
                        help='where to write the report of what was kept and '
                             'dropped. Required, and outside the component: a '
                             'trim nobody can review is a set of deletions '
                             'nobody agreed to, and the report names every '
                             'file that was withheld.')
    args = parser.parse_args()
    if not args.closure and not args.keep_all:
        parser.error('--closure is required: without a measurement there is '
                     'nothing to trim against. Use --keep-all for a component '
                     'that is already what its half needs.')

    try:
        policy = read_policy(args.policy)
        files, kept_by, dropped = plan(args.component, args.closure, policy,
                                       args.module_sources, args.subset,
                                       args.keep_all)
        summarize(files, kept_by, dropped, sys.stdout)
        if args.out:
            report = write(args.component, args.out, kept_by, dropped, policy)
            print('trimmed component -> %s' % args.out)
            if args.strip:
                sizes = strip_debug(args.out, args.strip)
                report['stripped'] = sizes
                print('  stripped %d binaries, %d -> %d bytes'
                      % (sizes['binaries'], sizes['before'], sizes['after']))
                redacted = redact_source_paths(args.out)
                report['redacted'] = dict(redacted)
                print('  redacted %d source paths in %d binaries'
                      % (sum(redacted.values()), len(redacted)))
            offenders = check_stripped(args.out)
            if offenders:
                sys.stderr.write(
                    'error: %d binar(ies) still carry debug information, '
                    'which names our sources, functions and structures. '
                    'Strip them before shipping%s:\n  %s\n'
                    % (len(offenders),
                       '' if args.strip else ' (--strip was not given)',
                       '\n  '.join('%s (%d debug sections)' % (path, count)
                                    for path, count in offenders[:20])))
                return 1
            leaking = check_redacted(args.out)
            if leaking:
                sys.stderr.write(
                    'error: %d binar(ies) still name the directories our '
                    'sources live in%s:\n  %s\n'
                    % (len(leaking),
                       '' if args.strip else ' (--strip was not given)',
                       '\n  '.join('%s (%d paths)' % (path, count)
                                    for path, count in leaking[:20])))
                return 1
            report['stripped_verified'] = True
            report['redacted_verified'] = True
            if args.report:
                with open(args.report, 'w') as handle:
                    json.dump(report, handle, indent=2, sort_keys=True,
                              separators=(',', ': '))
                    handle.write('\n')
    except TrimError as error:
        sys.stderr.write('error: %s\n' % error)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

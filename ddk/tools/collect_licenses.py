#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Gives a component its licence texts, and refuses copyleft it cannot elect.

Two separate things go wrong with the licensing of a delivery, and this
handles both.

The loud one is contamination: something under a copyleft licence gets linked
in, and the obligation reaches everything it was linked into.  A component is
scanned for that here, against a declaration rather than by guesswork, and a
copyleft component with no permissive alternative stops the build.

The quiet one is under-attribution, and it is the one that actually happens.
BSD and MIT both ask only that the notice travel with the code, and a
component assembled out of headers has no natural place to put one: 257 of
libddk's files carry no inline notice at all, 212 of them the C library's.
Shipping them without a COPYRIGHT file is a breach of the most permissive
licence there is.

Ownership is declared by longest-prefix rules, the same shape as the layout
and the partition: the answer does not depend on the order the entries are
written in, and every file must be claimed by one.  A file no entry claims is
an error -- that is how something arrives in a delivery with nobody having
decided what it is.
"""

from __future__ import print_function

import argparse
import codecs
import errno
import hashlib
import io
import json
import os
import re
import shutil
import sys

FORMAT_VERSION = 1
LICENSES_DIR = 'LICENSES'
PROVENANCE_NAME = 'PROVENANCE.json'
FILE_MAP_NAME = 'FILES'

NOTICE_NAME = 'NOTICE'
# The delivery's own bookkeeping: what it contains, what it ships under, and
# which release it is.  Not content, so not covered by a licence row -- and
# named once, because the set was written out by hand in three places and a
# file added to one of them was reported by the other two as unlicensed.
BOOKKEEPING = frozenset(['SDK_CONTENTS.json', NOTICE_NAME, 'RELEASE',
                         'LIBDDK_ABI', '.partition.json'])

# Not content and not ours: a delivered half ships as a git repository, and
# walking into it claims every loose object as an unlicensed source file.
SKIP_DIRS = frozenset(['.git'])

# Substrings that mean copyleft.  Matched against the SPDX expression a
# component declares, not against its text: the declaration is what a reviewer
# reads, so it is what has to be right.
COPYLEFT_MARKERS = ('GPL', 'AGPL', 'LGPL', 'MPL', 'EPL', 'CDDL', 'CECILL',
                    'OSL', 'EUPL', 'SSPL')

# The declaration says what each part is; this says what each file claims to
# be.  They have to agree.  A prefix rule with a catch-all root will happily
# label a stranger that lands under include/ as ours, and the first anyone
# would hear of it is a customer's lawyer.
# A file's own SPDX tag is the strongest statement it makes about itself, and
# it is the one a prefix rule most easily gets wrong: a catch-all root labels
# whatever lands under include/ as ours.  Seventeen files were labelled
# BSD-3-Clause here while calling themselves MIT, Apache-2.0 or BSD-2-Clause.
SPDX_TAG = re.compile(
    r'SPDX-License-Identifier:\s*([A-Za-z0-9.\-+]+'
    r'(?:\s+(?:OR|AND|WITH)\s+[A-Za-z0-9.\-+]+)*)')

COPYLEFT_TEXT = re.compile(
    r'GNU General Public License'
    r'|GNU Lesser General Public'
    r'|GNU Library General Public'
    r'|GNU Affero'
    r'|Mozilla Public License'
    r'|Common Development and Distribution License'
    r'|Eclipse Public License'
    r'|Server Side Public License'
    r'|SPDX-License-Identifier:[^\n]*(?:GPL|MPL|EPL|CDDL|SSPL)',
    re.I)

# A copyright line, and the holder it names.  Deliberately loose on the year
# and punctuation that precede the name, and stopped by the first comma, full
# stop or comment marker after it.
# The word, not the tail of an identifier.  Lua defines LUA_COPYRIGHT as a
# version string, and without the guard the holder read out of it is the macro
# name -- a gap reported against a file whose notice is three characters
# further along the same line and perfectly in order.
COPYRIGHT = re.compile(
    r'(?<![A-Za-z0-9_])Copyright\s*(?:\(c\)|\(C\)|\u00a9)?\s*[-0-9,\s]*'
    r'([A-Z][^\n*/,.]{3,45})', re.I)

# A generator's own marking, with the source it was made from on the line
# after it.  protoc and abigen both write one.
GENERATED = re.compile(r'DO NOT EDIT!?\s*\n.*\bsource:\s*\S+', re.I)

TEXT_SUFFIXES = ('.h', '.hh', '.hpp', '.hxx', '.inc', '.inl', '.ipp', '.def',
                 '.defs', '.txt', '.md')

try:
    STRING_TYPES = (str, unicode)  # noqa: F821  (python 2)
except NameError:
    STRING_TYPES = (str,)


class LicenseError(Exception):
    """A file nobody claimed, or copyleft with no way out."""


def _fail(message):
    raise LicenseError(message)


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


def is_copyleft(expression):
    """Whether an SPDX expression names a copyleft licence."""
    upper = (expression or '').upper()
    return any(marker in upper for marker in COPYLEFT_MARKERS)


def relocate(components, prefix):
    """The same entries, for a component that sits under `prefix`.

    A half may carry a component of its own -- what only that half needs,
    under libddk/ inside it.  Those files are libddk's, and libddk's licence
    declaration is what describes them; what differs is only where they sit.
    Rewriting the prefixes keeps one declaration for one set of files, rather
    than a second copy of 88 entries that would drift from the first.

    Applied after validation, never before: until then the root entry's
    prefix is '', and a declaration without one is refused for good reason.
    """
    if not prefix:
        return components
    moved = []
    for entry in components:
        entry = dict(entry)
        entry['prefix'] = prefix if entry['prefix'] == '' \
            else prefix + '/' + entry['prefix']
        moved.append(entry)
    return moved


def read_declaration(path, source_root, under=''):
    """Reads and validates the licence declaration.

    `under` moves every entry beneath a directory, for a component that sits
    inside another part -- a half's own libddk/.  The files are libddk's and
    this is libddk's declaration; only where they sit differs.  Not named
    `prefix`: the validation loop below binds that name to each entry's own
    prefix, and a parameter of that name is silently replaced by the last
    one -- which relocated every entry under it and claimed nothing.
    """
    with open(path, 'r') as handle:
        declaration = json.load(handle)
    if declaration.get('format_version') != FORMAT_VERSION:
        _fail('%s: unsupported format_version %r'
              % (path, declaration.get('format_version')))
    components = declaration.get('components')
    if not isinstance(components, list) or not components:
        _fail('%s: `components` must be a non-empty list' % path)

    seen = set()
    root_entry = None
    for entry in components:
        if not isinstance(entry, dict):
            _fail('%s: each component must be an object' % path)
        prefix = entry.get('prefix')
        if prefix is None or not isinstance(prefix, STRING_TYPES):
            _fail('%s: a component needs a `prefix`' % path)
        if prefix == '':
            root_entry = entry
        elif not _is_clean_relative(prefix):
            _fail('%s: invalid prefix %r' % (path, prefix))
        if prefix in seen:
            _fail('%s: duplicate prefix %r' % (path, prefix))
        seen.add(prefix)
        owner = entry.get('owner')
        if owner is not None and (not isinstance(owner, STRING_TYPES)
                                  or not owner.strip()):
            _fail('%s: component %r has an empty owner' % (path, prefix))

        for field in ('name', 'spdx', 'license_file', 'note'):
            value = entry.get(field)
            if not isinstance(value, STRING_TYPES) or not value.strip():
                _fail('%s: component %r needs a non-empty %s'
                      % (path, prefix, field))
        entry['license_path'] = _resolve(entry['license_file'], source_root,
                                         path)

        holders = entry.get('embedded_holders')
        if holders is not None:
            if not isinstance(holders, list) or not holders:
                _fail('%s: %s declares an empty embedded_holders'
                      % (path, entry['name']))
            reason = entry.get('embedded_reason')
            if not isinstance(reason, STRING_TYPES) or not reason.strip():
                _fail('%s: %s declares embedded_holders and no '
                      'embedded_reason. Naming a holder the licence text does '
                      'not is a decision; it needs the reason written down.'
                      % (path, entry['name']))

        if is_copyleft(entry['spdx']):
            elected = entry.get('elected_spdx')
            if not elected:
                _fail('%s: %s is %s, which is copyleft, and declares no '
                      'elected_spdx. Either it is dual licensed and the '
                      'permissive half has to be named here, or it does not '
                      'belong in a component that ships closed.'
                      % (path, entry['name'], entry['spdx']))
            if is_copyleft(elected):
                _fail('%s: %s elects %s, which is also copyleft'
                      % (path, entry['name'], elected))
            if elected.upper() not in entry['spdx'].upper():
                _fail('%s: %s elects %s, which its licence %s does not offer'
                      % (path, entry['name'], elected, entry['spdx']))
    if root_entry is None:
        _fail('%s: no component with an empty prefix; every file has to be '
              'claimed by something' % path)

    # A licence directory that records where its texts came from is checked
    # here, before anything is written.  Texts fetched from upstream -- the
    # compiler's, which its own package ships none of -- are exactly the kind
    # of file nobody re-reads once it is in the tree.
    for directory in sorted(set(os.path.dirname(entry['license_path'])
                                for entry in components)):
        check_provenance(directory)
    return relocate(components, under)


def _resolve(spelling, source_root, declaration_path):
    if spelling.startswith('//'):
        resolved = os.path.join(source_root, spelling[2:])
    else:
        resolved = os.path.join(os.path.dirname(declaration_path), spelling)
    resolved = os.path.abspath(resolved)
    if not os.path.isfile(resolved):
        _fail('licence file does not exist: %s (%s)' % (spelling, resolved))
    return resolved


def check_provenance(directory):
    """Verifies the licence texts against the record of where they came from.

    These four texts are not in the prebuilt and not written by anyone here:
    they are fetched once from the upstream release that matches the pinned
    clang.sha1 and kept in the tree so a release never needs the network.
    That only holds if they are still the texts that were fetched, and a
    licence file is exactly the kind of thing nobody re-reads -- so the
    checksums travel beside them and are verified on every export.

    A directory with no PROVENANCE.json is not an error: a declaration may
    point at texts that live beside the code they cover, which is the normal
    case everywhere else in the delivery.
    """
    record_path = os.path.join(directory, PROVENANCE_NAME)
    if not os.path.isfile(record_path):
        return None
    with open(record_path, 'r') as handle:
        record = json.load(handle)

    wrong = []
    for name in sorted(record.get('files', {})):
        entry = record['files'][name]
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            wrong.append('%s: missing' % name)
            continue
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b''):
                digest.update(chunk)
        found = digest.hexdigest()
        if found != entry.get('sha256'):
            wrong.append('%s: sha256 %s, recorded %s (from %s)'
                         % (name, found[:16], (entry.get('sha256') or '?')[:16],
                            entry.get('url', 'unrecorded')))
    if wrong:
        _fail('%d licence text(s) do not match %s:\n  %s'
              % (len(wrong), record_path, '\n  '.join(wrong)))
    return record



def claim(relative, components):
    """The component owning a path, by longest matching prefix."""
    best = None
    for entry in components:
        prefix = entry['prefix']
        if prefix == '' or relative == prefix or relative.startswith(
                prefix + '/'):
            if best is None or len(prefix) > len(best['prefix']):
                best = entry
    return best


def _covers(declared, elected, claimed):
    """Whether a declared expression accounts for a file's own SPDX tag."""
    if elected and claimed.upper() == elected.upper():
        return True
    tokens = re.split(r'\s+(?:OR|AND|WITH)\s+', declared, flags=re.I)
    return claimed.upper() in [token.strip().upper() for token in tokens]


def contradictions(component, coverage):
    """Files that disagree with the licence they were declared under.

    Two ways a file can say so, and both are read from the file rather than
    from the declaration, because the declaration is exactly what is in doubt.

    An SPDX tag the declared expression does not account for is a
    disagreement: a prefix rule claimed the file and named the wrong licence.

    Copyleft text in a file whose component declares none is a disagreement
    too, and it is the one that matters most -- ACRN's ioctl header carries a
    dual BSD/GPLv2 notice and no tag at all, so nothing but its text says so.
    """
    found = []
    for name in sorted(coverage):
        entry = coverage[name]['entry']
        declared = entry['spdx']
        elected = entry.get('elected_spdx')
        for relative in coverage[name]['files']:
            if not relative.endswith(TEXT_SUFFIXES):
                continue
            path = os.path.join(component, relative)
            try:
                with open(path, 'rb') as handle:
                    text = handle.read(8000).decode('utf-8', 'replace')
            except (IOError, OSError):
                continue
            tag = SPDX_TAG.search(text)
            if tag is not None:
                claimed = tag.group(1).strip()
                if not _covers(declared, elected, claimed):
                    found.append((relative, name,
                                  'declared %s, calls itself %s'
                                  % (declared, claimed)))
                continue
            if not is_copyleft(declared) and COPYLEFT_TEXT.search(text):
                found.append((relative, name,
                              'declared %s, carries copyleft text'
                              % declared))
    return found


def copyright_holders(text):
    """The copyright holders a piece of text names."""
    found = set()
    for match in COPYRIGHT.finditer(text):
        holder = re.sub(r'\s+', ' ', match.group(1).strip().rstrip('.').strip())
        if holder and not holder.lower().startswith(('yyyy', '[')):
            found.add(holder)
    return found


def _is_named(holder, known):
    """Whether `known` mentions `holder` at all.

    A presence test, not an equality one: it catches a holder nobody wrote
    down, which is the failure that happened -- 304 files carried
    GoldenRiver's copyright under a licence text that named only the Fuchsia
    Authors.  It does not catch a text that names the right holder and says
    the wrong thing; only reading catches that.
    """
    first = holder.split()[0].lower()
    return any(first in candidate.lower() for candidate in known)


def attribution_gaps(component, coverage):
    """Holders a component's files name that its licence text does not.

    BSD and MIT both ask that the copyright notice travel with the code. A
    file's own header satisfies that on its own, so a gap here is not a breach
    -- it is the delivery telling a reader that somebody else wrote what we
    did, or that we wrote what somebody else did.
    """
    gaps = []
    for name in sorted(coverage):
        entry = coverage[name]['entry']
        try:
            with open(entry['license_path'], 'rb') as handle:
                licence = handle.read().decode('utf-8', 'replace')
        except (IOError, OSError):
            licence = ''
        known = copyright_holders(licence)
        known |= set(coverage[name].get('embedded_holders', []))
        missing = {}
        for relative in coverage[name]['files']:
            if not relative.endswith(TEXT_SUFFIXES):
                continue
            path = os.path.join(component, relative)
            try:
                with open(path, 'rb') as handle:
                    text = handle.read(8000).decode('utf-8', 'replace')
            except (IOError, OSError):
                continue
            for holder in copyright_holders(text):
                if not _is_named(holder, known):
                    missing.setdefault(holder, []).append(relative)
        for holder in sorted(missing):
            gaps.append((name, holder, len(missing[holder]),
                         missing[holder][0]))
    return gaps


def undeclared(component, coverage):
    """Files we answer for that say nothing about who wrote them.

    Upstream is exempt, and deliberately: musl's stdio.h and libc++'s
    wchar.h carry no per-file notice and never did, and LICENSES/musl.txt
    answers for them.  What is not exempt is a file under a component this
    delivery claims as its own -- an owner in the declaration is exactly
    that claim -- because there the licence text we ship is the only thing
    saying where the code came from, and a file with no notice of its own
    disappears into it.

    zircon/device/hash.h was Linux's, had no copyright line, and rode four
    releases declared BSD-3-Clause because nothing here looked.
    """
    silent = []
    for name in sorted(coverage):
        entry = coverage[name]['entry']
        if not entry.get('owner'):
            continue
        for relative in coverage[name]['files']:
            if not relative.endswith(TEXT_SUFFIXES):
                continue
            path = os.path.join(component, relative)
            try:
                with open(path, 'rb') as handle:
                    text = handle.read(8000).decode('utf-8', 'replace')
            except (IOError, OSError):
                continue
            if SPDX_TAG.search(text) or COPYRIGHT.search(text):
                continue
            if GENERATED.search(text):
                # protoc writes `DO NOT EDIT!` and the .proto it read, and
                # carries no notice across.  Putting one here would last
                # until the next build; the source it names is where the
                # notice belongs, and this check reads that file in the
                # tree, not this copy of its output.
                continue
            silent.append((relative, name))
    return silent


def scan(component, components, ignore):
    """Which declared licence covers each file, and which claim nothing."""
    coverage = {}
    unclaimed = []
    for current, dirs, names in os.walk(component):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            relative = os.path.relpath(os.path.join(current, name),
                                       component).replace(os.sep, '/')
            if relative in ignore or relative.startswith(LICENSES_DIR + '/'):
                continue
            entry = claim(relative, components)
            if entry is None:
                unclaimed.append(relative)
                continue
            record = coverage.setdefault(entry['name'],
                                         {'entry': entry, 'files': [],
                                          'notes': [],
                                          'embedded_holders': [],
                                          'owners': []})
            if entry.get('owner') and entry['owner'] not in record['owners']:
                record['owners'].append(entry['owner'])
            for holder in entry.get('embedded_holders', []):
                if holder not in record['embedded_holders']:
                    record['embedded_holders'].append(holder)
            record['files'].append(relative)

            # Several prefixes share one name -- the C library's headers, its
            # archives and its debug build are all musl -- and each says
            # something different about its own part.  Keeping only the first
            # would describe 217 files by whichever entry the walk happened to
            # reach first.
            if entry['note'] not in record['notes']:
                record['notes'].append(entry['note'])
    if unclaimed:
        _fail('%d file(s) in the component are claimed by no licence entry:\n'
              '  %s' % (len(unclaimed), '\n  '.join(sorted(unclaimed)[:20])))
    return coverage


def write(component, coverage):
    """Installs the licence texts and a NOTICE naming what covers what."""
    licenses_dir = os.path.join(component, LICENSES_DIR)
    if os.path.isdir(licenses_dir):
        shutil.rmtree(licenses_dir)
    _makedirs(licenses_dir)

    # "Part", not "component": this writes the NOTICE for the binary
    # component and for each source half, and a half is not a component.
    lines = [
        'NOTICE',
        '',
        'What is in this part of the delivery, and what it is used under.',
        'Each entry below names a licence; the full text of every licence',
        'named here is in %s/, and %s/%s lists every file in this'
        % (LICENSES_DIR, LICENSES_DIR, FILE_MAP_NAME),
        'part against the entry that covers it.',
        '',
    ]

    # Say plainly which parts are ours.  Several of them are forks of upstream
    # work and their files carry both copyrights, so a reader with only the
    # licence texts in front of them cannot tell which is which.
    owned = {}
    for name in sorted(coverage):
        for owner in coverage[name].get('owners', []):
            owned.setdefault(owner, []).append(name)
    for owner in sorted(owned):
        # No verb agreeing with the owner: several of them are plural
        # ("The littlefs authors"), several are not, and the string comes
        # from a declaration rather than from anything that knows which.
        for line in _wrap('Copyright in the following is held by %s, '
                          'alongside the upstream authors named in their '
                          'licence texts:' % owner, 72):
            lines.append(line)
        lines.append('')
        for name in owned[owner]:
            lines.append('    %s' % name)
        lines.append('')
    for name in sorted(coverage):
        entry = coverage[name]['entry']
        files = coverage[name]['files']
        target = os.path.join(licenses_dir, name + '.txt')
        if not os.path.isfile(target):
            shutil.copyfile(entry['license_path'], target)
        lines.append('%s' % name)
        lines.append('  licence   %s' % entry['spdx'])
        if entry.get('elected_spdx'):
            lines.append('  elected   %s' % entry['elected_spdx'])
        lines.append('  files     %d' % len(files))
        lines.append('  text      %s/%s.txt' % (LICENSES_DIR, name))
        for note in coverage[name].get('notes', [entry['note']]):
            for line in _wrap(note, 68):
                lines.append('  %s' % line)
        lines.append('')

    # Explicitly UTF-8: a note may carry a name that is not ASCII, and
    # python 2's default codec would refuse to write it -- during an export,
    # after the tree is already staged.
    with codecs.open(os.path.join(component, NOTICE_NAME), 'w',
                     encoding='utf-8') as handle:
        handle.write('\n'.join(lines).rstrip('\n') + '\n')

    write_file_map(licenses_dir, coverage)
    return licenses_dir


def write_file_map(licenses_dir, coverage):
    """One line per delivered file, naming the licence it ships under.

    NOTICE answers "what is in this delivery and under what"; it does not
    answer "this file here -- which licence is that?", which is the question
    a customer actually arrives with, about one path out of a thousand.
    Counting files by component and leaving the reader to work out which
    component a path falls into is asking them to re-derive the prefix rules
    from the outside.

    Sorted by path and plain text, so `grep` answers it directly.  The one
    thing not listed is this directory itself: the licence texts and this
    file are the notice, not the thing being licensed.
    """
    claimed = []
    width = 0
    for name in sorted(coverage):
        entry = coverage[name]['entry']
        spdx = entry['spdx']
        if entry.get('elected_spdx'):
            spdx = '%s (elected %s)' % (spdx, entry['elected_spdx'])
        for relative in coverage[name]['files']:
            claimed.append((relative, name, spdx))
            width = max(width, len(relative))
    claimed.sort()

    lines = [
        'Every file in this delivery, and the licence it ships under.',
        '',
        'Look a path up here, then read %s/<component>.txt for the terms and'
        % LICENSES_DIR,
        'NOTICE for what the component is and who holds copyright in it.',
        '',
        'Not listed: %s/ itself.  The licence texts and this file are the'
        % LICENSES_DIR,
        'notice, not the thing being licensed.',
        '',
        '%-*s  %-22s  %s' % (width, 'FILE', 'COMPONENT', 'LICENCE'),
        '%s  %s  %s' % ('-' * width, '-' * 22, '-' * 30),
    ]
    for relative, name, spdx in claimed:
        lines.append('%-*s  %-22s  %s' % (width, relative, name, spdx))

    path = os.path.join(licenses_dir, FILE_MAP_NAME)
    with codecs.open(path, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')
    return len(claimed)


def _wrap(text, width):
    words = text.split()
    out, line = [], ''
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = word if not line else line + ' ' + word
    if line:
        out.append(line)
    return out


def read_file_map(component):
    """The path -> component -> licence rows a delivery carries."""
    path = os.path.join(component, LICENSES_DIR, FILE_MAP_NAME)
    if not os.path.isfile(path):
        _fail('%s has no %s/%s, so nothing in it says which licence any file '
              'ships under' % (component, LICENSES_DIR, FILE_MAP_NAME))
    rows = {}
    # io.open, not codecs.open: this one only reads, and codecs.open is
    # deprecated from python 3.13.  The two writers below stay on codecs,
    # which is how python 2 is told the encoding at all.
    with io.open(path, 'r', encoding='utf-8') as handle:
        body = handle.read().split('\n')
    started = False
    for line in body:
        if line.startswith('----'):
            started = True
            continue
        if not started or not line.strip():
            continue
        parts = line.split('  ')
        parts = [p for p in parts if p.strip()]
        if len(parts) < 3:
            _fail('%s: unreadable row: %r' % (path, line))
        rows[parts[0].strip()] = (parts[1].strip(), parts[2].strip())
    return rows


def verify(component, ignore=None):
    """Checks a delivery against its own licence table, and nothing else.

    Deliberately reads no declaration.  The declaration is what produced the
    delivery; running the same input through the same rules would agree with
    itself whatever it did.  This is the artefact as shipped, checked with
    only what is inside it -- which is what an auditor holds, and what a
    customer can be handed to repeat.

    Three ways it can be wrong, and the first is the one that matters: a file
    nobody assigned a licence to has left the building.
    """
    ignore = set(ignore or ()) | set(BOOKKEEPING)
    rows = read_file_map(component)

    on_disk = set()
    for current, dirs, names in os.walk(component):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            relative = os.path.relpath(os.path.join(current, name),
                                       component).replace(os.sep, '/')
            if relative in ignore or relative.startswith(LICENSES_DIR + '/'):
                continue
            on_disk.add(relative)

    problems = []
    unlicensed = sorted(on_disk - set(rows))
    if unlicensed:
        problems.append(
            '%d file(s) ship with no licence named anywhere in the '
            'delivery:\n  %s' % (len(unlicensed), '\n  '.join(unlicensed[:20])))

    stale = sorted(set(rows) - on_disk)
    if stale:
        problems.append(
            '%d file(s) are named in %s and are not in the delivery, so the '
            'table describes something else:\n  %s'
            % (len(stale), FILE_MAP_NAME, '\n  '.join(stale[:20])))

    missing_text = sorted(set(
        name for name, _ in rows.values()
        if not os.path.isfile(os.path.join(component, LICENSES_DIR,
                                           name + '.txt'))))
    if missing_text:
        problems.append(
            '%d component(s) are named against files and have no licence '
            'text beside them: %s'
            % (len(missing_text), ', '.join(missing_text)))

    if problems:
        _fail('\n'.join(problems))
    return {'files': len(rows),
            'components': len(set(name for name, _ in rows.values()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', required=True,
                        help='materialized component to cover')
    parser.add_argument('--declaration', action='append', default=[],
                        help='the licence declaration; required unless '
                             '--verify. Repeat as DECLARATION or '
                             'PREFIX=DECLARATION to cover a part that holds '
                             'more than one thing: a half carrying libddk/ '
                             'of its own is source under one declaration and '
                             'a component under the other.')
    parser.add_argument('--source-root',
                        help='root the // spellings resolve against')
    parser.add_argument('--verify', action='store_true',
                        help='check a finished delivery against its own '
                             'licence table instead of writing one. Reads no '
                             'declaration: the point is the artefact as '
                             'shipped, checked with only what is inside it.')
    parser.add_argument('--check-only', action='store_true',
                        help='report without writing anything')
    args = parser.parse_args()

    try:
        if args.verify:
            result = verify(args.component)
            print('every one of the %d file(s) in %s names a licence, and '
                  'each of the %d component(s) has its text'
                  % (result['files'], args.component, result['components']))
            return 0
        if not args.declaration or not args.source_root:
            parser.error('--declaration and --source-root are required '
                         'unless --verify')
        components = []
        for spelling in args.declaration:
            under, _, path = spelling.rpartition('=')
            components += read_declaration(path, args.source_root, under)
        ignore = set(BOOKKEEPING)
        coverage = scan(args.component, components, ignore)
        against = contradictions(args.component, coverage)
        if against:
            sys.stderr.write(
                'error: %d file(s) disagree with the licence they are '
                'declared under:\n  %s\n'
                % (len(against),
                   '\n  '.join('%s (under %s): %s' % (path, name, why)
                                for path, name, why in against[:20])))
            return 1
        gaps = attribution_gaps(args.component, coverage)
        if gaps:
            sys.stderr.write(
                'error: %d copyright holder(s) are named in the files but not '
                'in the licence text they ship under. Add them to the text, '
                'or declare them as embedded_holders with a reason:\n  %s\n'
                % (len(gaps),
                   '\n  '.join('%s: %s, in %d file(s), e.g. %s'
                                % (name, holder, count, example)
                                for name, holder, count, example in gaps[:20])))
            return 1
        copyleft = [name for name in coverage
                    if is_copyleft(coverage[name]['entry']['spdx'])]
        for name in sorted(coverage):
            entry = coverage[name]['entry']
            note = entry['spdx']
            if entry.get('elected_spdx'):
                note += '  -> elected %s' % entry['elected_spdx']
            print('  %-14s %5d files   %s'
                  % (name, len(coverage[name]['files']), note))
        if copyleft:
            print('  copyleft present, each with a permissive election: %s'
                  % ', '.join(sorted(copyleft)))
        if not args.check_only:
            write(args.component, coverage)
            print('licences installed -> %s/%s and %s'
                  % (args.component, LICENSES_DIR, NOTICE_NAME))
    except LicenseError as error:
        sys.stderr.write('error: %s\n' % error)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

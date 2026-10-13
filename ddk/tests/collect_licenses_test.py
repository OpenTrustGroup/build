#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for collect_licenses.py.

Two failures matter and they fail differently.  Copyleft with no permissive
election is loud and stops everything.  A file nobody decided about is quiet,
and the tests below are mostly about making it loud.
"""

from __future__ import print_function

import io
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import collect_licenses  # noqa: E402

GPL_TEXT = ('/* This program is free software; you can redistribute it under\n'
            ' * the terms of the GNU General Public License version 2. */\n')
BSD_TEXT = '/* Redistribution and use in source and binary forms ... */\n'


def write(path, content):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, 'w') as handle:
        handle.write(content)


class LicenseTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='licences-test-')
        self.component = os.path.join(self.root, 'libddk')
        write(os.path.join(self.component, 'include/ours.h'), BSD_TEXT)
        write(os.path.join(self.component, 'include/openssl/ssl.h'), BSD_TEXT)
        write(os.path.join(self.component, 'SDK_CONTENTS.json'), '{}')
        write(os.path.join(self.root, 'src/OURS'), 'our licence\n')
        write(os.path.join(self.root, 'src/THEIRS'), 'their licence\n')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def declaration(self, components):
        path = os.path.join(self.root, 'licences.json')
        write(path, json.dumps({'format_version': 1,
                                'components': components}))
        return path

    def base(self, **overrides):
        entries = [
            {'prefix': '', 'name': 'ours', 'spdx': 'BSD-3-Clause',
             'license_file': '//src/OURS', 'note': 'everything else'},
            {'prefix': 'include/openssl', 'name': 'theirs',
             'spdx': 'BSD-3-Clause', 'license_file': '//src/THEIRS',
             'note': 'a third party'},
        ]
        for index, patch in overrides.items():
            entries[int(index)].update(patch)
        return entries

    def read(self, components):
        return collect_licenses.read_declaration(
            self.declaration(components), self.root)

    def scan(self, components):
        entries = self.read(components)
        return collect_licenses.scan(self.component, entries,
                                     {'SDK_CONTENTS.json'})


class RelocationTest(LicenseTestCase):
    """A part that holds more than one thing takes more than one declaration.

    A source half that carries a component of its own is source under one
    declaration and a component under the other.  The component's own
    declaration describes those files; only where they sit differs, and the
    source declaration's root entry would otherwise claim them as GRT's.
    """

    def test_entries_move_under_the_prefix(self):
        entries = collect_licenses.read_declaration(
            self.declaration(self.base()), self.root, 'libddk')
        self.assertEqual(['libddk', 'libddk/include/openssl'],
                         sorted(e['prefix'] for e in entries))

    def test_without_a_prefix_nothing_moves(self):
        entries = collect_licenses.read_declaration(
            self.declaration(self.base()), self.root)
        self.assertEqual(['', 'include/openssl'],
                         sorted(e['prefix'] for e in entries))

    def test_two_declarations_cover_one_part(self):
        write(os.path.join(self.component, 'libddk/include/openssl/x.h'),
              'x\n')
        write(os.path.join(self.component, 'ours.h'), 'x\n')
        entries = collect_licenses.read_declaration(
            self.declaration(self.base()), self.root) + \
            collect_licenses.read_declaration(
                self.declaration(self.base()), self.root, 'libddk')
        coverage = collect_licenses.scan(self.component, entries,
                                        {'SDK_CONTENTS.json'})
        claimed = dict((name, record['files'])
                       for name, record in coverage.items())
        self.assertIn('libddk/include/openssl/x.h', claimed['theirs'])
        self.assertIn('ours.h', claimed['ours'])


class CoverageTest(LicenseTestCase):

    def test_longest_prefix_claims_the_file(self):
        coverage = self.scan(self.base())
        self.assertEqual(coverage['theirs']['files'],
                         ['include/openssl/ssl.h'])
        self.assertEqual(coverage['ours']['files'], ['include/ours.h'])

    def test_order_does_not_matter(self):
        forward = self.scan(self.base())
        backward = self.scan(list(reversed(self.base())))
        self.assertEqual(sorted(forward), sorted(backward))
        self.assertEqual(forward['theirs']['files'],
                         backward['theirs']['files'])

    def test_a_declaration_without_a_root_is_refused(self):
        entries = [e for e in self.base() if e['prefix'] != '']
        with self.assertRaises(collect_licenses.LicenseError) as caught:
            self.read(entries)
        self.assertIn('empty prefix', str(caught.exception))

    def test_a_component_without_a_note_is_refused(self):
        entries = self.base()
        del entries[1]['note']
        self.assertRaises(collect_licenses.LicenseError, self.read, entries)

    def test_a_missing_licence_file_is_refused(self):
        entries = self.base()
        entries[1]['license_file'] = '//src/NOT_THERE'
        self.assertRaises(collect_licenses.LicenseError, self.read, entries)

    def test_a_delivered_half_is_a_git_repository_and_git_is_not_content(self):
        # mtk_ddk ships as a repository. Walking into .git claims every loose
        # object as an unlicensed source file, which is both wrong and 50
        # files of noise for every 5 real ones.
        write(os.path.join(self.component, '.git/objects/ab/cdef'), 'binary')
        write(os.path.join(self.component, '.git/HEAD'), 'ref: refs/heads/x\n')
        coverage = self.scan(self.base())
        claimed = [f for files in coverage.values() for f in files['files']]
        self.assertEqual([], [f for f in claimed if f.startswith('.git')])

    def test_a_halfs_partition_manifest_is_bookkeeping(self):
        # It says which files the half owns. It is not one of them.
        self.assertIn('.partition.json', collect_licenses.BOOKKEEPING)


class CopyleftTest(LicenseTestCase):

    def test_copyleft_without_an_election_is_refused(self):
        entries = self.base()
        entries[1]['spdx'] = 'GPL-2.0-or-later'
        with self.assertRaises(collect_licenses.LicenseError) as caught:
            self.read(entries)
        self.assertIn('copyleft', str(caught.exception))

    def test_a_dual_licence_may_elect_the_permissive_half(self):
        entries = self.base()
        entries[1]['spdx'] = 'GPL-2.0-or-later OR BSD-2-Clause'
        entries[1]['elected_spdx'] = 'BSD-2-Clause'
        read = self.read(entries)
        self.assertEqual(read[1]['elected_spdx'], 'BSD-2-Clause')

    def test_electing_something_the_licence_does_not_offer_is_refused(self):
        entries = self.base()
        entries[1]['spdx'] = 'GPL-2.0-or-later OR BSD-2-Clause'
        entries[1]['elected_spdx'] = 'MIT'
        with self.assertRaises(collect_licenses.LicenseError) as caught:
            self.read(entries)
        self.assertIn('does not offer', str(caught.exception))

    def test_electing_another_copyleft_is_refused(self):
        entries = self.base()
        entries[1]['spdx'] = 'GPL-2.0-or-later OR LGPL-2.1'
        entries[1]['elected_spdx'] = 'LGPL-2.1'
        self.assertRaises(collect_licenses.LicenseError, self.read, entries)

    def test_is_copyleft_recognises_the_usual_families(self):
        for expression in ('GPL-2.0-only', 'AGPL-3.0', 'LGPL-2.1',
                           'MPL-2.0', 'EPL-1.0', 'CDDL-1.0', 'SSPL-1.0'):
            self.assertTrue(collect_licenses.is_copyleft(expression),
                            expression)
        for expression in ('BSD-3-Clause', 'MIT', 'Apache-2.0', 'ISC'):
            self.assertFalse(collect_licenses.is_copyleft(expression),
                             expression)


class UndeclaredTest(LicenseTestCase):
    """A component we answer for says who wrote each of its files."""

    def setUp(self):
        LicenseTestCase.setUp(self)
        # The base checkout's own header carries licence text and no notice,
        # which is the very thing this check is about: give it one, so what
        # each test asserts is the file that test wrote.
        write(os.path.join(self.component, 'include/ours.h'),
              '// Copyright 2026 The GoldenRiverTek Authors.\n' + BSD_TEXT)

    def test_a_file_under_our_own_component_with_no_notice_is_caught(self):
        # zircon/device/hash.h was Linux's, carried no copyright line, and
        # shipped four releases declared BSD-3-Clause because nothing looked.
        entries = self.base(**{'0': {'owner': 'GoldenRiver'}})
        write(os.path.join(self.component, 'include/silent.h'),
              '#pragma once\nint f(void);\n')
        found = collect_licenses.undeclared(self.component, self.scan(entries))
        self.assertEqual([path for path, _ in found], ['include/silent.h'])

    def test_upstream_with_no_notice_is_exempt(self):
        # musl's stdio.h and libc++'s wchar.h never had a per-file notice,
        # and LICENSES/musl.txt answers for them.
        entries = self.base(**{'0': {'owner': 'GoldenRiver'}})
        write(os.path.join(self.component, 'include/openssl/silent.h'),
              '#pragma once\nint f(void);\n')
        self.assertEqual(
            collect_licenses.undeclared(self.component, self.scan(entries)),
            [])

    def test_either_a_tag_or_a_copyright_line_is_enough(self):
        entries = self.base(**{'0': {'owner': 'GoldenRiver'}})
        write(os.path.join(self.component, 'include/tagged.h'),
              '// SPDX-License-Identifier: BSD-3-Clause\n')
        write(os.path.join(self.component, 'include/noticed.h'),
              '// Copyright 2026 The GoldenRiverTek Authors.\n')
        self.assertEqual(
            collect_licenses.undeclared(self.component, self.scan(entries)),
            [])

    def test_a_generated_file_that_names_its_source_is_exempt(self):
        # protoc writes the marking and the .proto it read, and carries no
        # notice across; putting one in its output lasts until the next
        # build.  The notice belongs in the source it names.
        entries = self.base(**{'0': {'owner': 'GoldenRiver'}})
        write(os.path.join(self.component, 'include/vm_config.pb.h'),
              '// Generated by the protocol buffer compiler.  DO NOT EDIT!\n'
              '// source: vm_config.proto\n\n#pragma once\n')
        self.assertEqual(
            collect_licenses.undeclared(self.component, self.scan(entries)),
            [])

    def test_a_do_not_edit_banner_alone_is_not_a_way_out(self):
        # Without a source named, nothing says where it came from, and the
        # banner would otherwise be a one-line exemption for anything.
        entries = self.base(**{'0': {'owner': 'GoldenRiver'}})
        write(os.path.join(self.component, 'include/silent.h'),
              '// DO NOT EDIT\n\n#pragma once\n')
        found = collect_licenses.undeclared(self.component, self.scan(entries))
        self.assertEqual([path for path, _ in found], ['include/silent.h'])

    def test_a_component_with_no_owner_is_never_asked(self):
        # Without an owner the declaration is not claiming the code as ours.
        write(os.path.join(self.component, 'include/silent.h'),
              '#pragma once\n')
        self.assertEqual(
            collect_licenses.undeclared(self.component,
                                        self.scan(self.base())),
            [])


class ContradictionTest(LicenseTestCase):
    """The declaration says what a part is; the file says what it claims."""

    def test_a_stray_copyleft_file_under_our_prefix_is_caught(self):
        # The catch-all root would otherwise label it ours, and the first
        # anyone hears of it is a customer's lawyer.
        write(os.path.join(self.component, 'include/stranger.h'), GPL_TEXT)
        coverage = self.scan(self.base())
        found = collect_licenses.contradictions(self.component, coverage)
        self.assertEqual([path for path, _, _ in found],
                         ['include/stranger.h'])

    def test_an_spdx_tag_alone_is_enough_to_catch_it(self):
        write(os.path.join(self.component, 'include/tagged.h'),
              '// SPDX-License-Identifier: GPL-2.0-only\n')
        coverage = self.scan(self.base())
        found = collect_licenses.contradictions(self.component, coverage)
        self.assertEqual([path for path, _, _ in found], ['include/tagged.h'])

    def test_a_tag_the_declaration_does_not_account_for_is_caught(self):
        # The failure that actually happened: seventeen files labelled
        # BSD-3-Clause by a prefix rule while calling themselves MIT,
        # Apache-2.0 or BSD-2-Clause.
        write(os.path.join(self.component, 'include/tagged.h'),
              '// SPDX-License-Identifier: MIT\n')
        coverage = self.scan(self.base())
        found = collect_licenses.contradictions(self.component, coverage)
        self.assertEqual([path for path, _, _ in found], ['include/tagged.h'])
        self.assertIn('calls itself MIT', found[0][2])

    def test_a_tag_the_declaration_does_account_for_passes(self):
        entries = self.base()
        entries[0]['spdx'] = 'BSD-3-Clause OR MIT'
        write(os.path.join(self.component, 'include/tagged.h'),
              '// SPDX-License-Identifier: MIT\n')
        coverage = self.scan(entries)
        self.assertEqual(
            collect_licenses.contradictions(self.component, coverage), [])

    def test_an_elected_licence_satisfies_a_files_own_tag(self):
        entries = self.base()
        entries[1]['spdx'] = 'GPL-2.0-or-later OR BSD-2-Clause'
        entries[1]['elected_spdx'] = 'BSD-2-Clause'
        write(os.path.join(self.component, 'include/openssl/ssl.h'),
              '// SPDX-License-Identifier: BSD-2-Clause\n')
        coverage = self.scan(entries)
        self.assertEqual(
            collect_licenses.contradictions(self.component, coverage), [])

    def test_a_declared_copyleft_component_is_not_a_contradiction(self):
        entries = self.base()
        entries[1]['spdx'] = 'GPL-2.0-or-later OR BSD-2-Clause'
        entries[1]['elected_spdx'] = 'BSD-2-Clause'
        write(os.path.join(self.component, 'include/openssl/ssl.h'), GPL_TEXT)
        coverage = self.scan(entries)
        self.assertEqual(
            collect_licenses.contradictions(self.component, coverage), [])

    def test_a_clean_component_has_no_contradictions(self):
        coverage = self.scan(self.base())
        self.assertEqual(
            collect_licenses.contradictions(self.component, coverage), [])


class AttributionTest(LicenseTestCase):
    """A holder in the files that the licence text never names."""

    def setUp(self):
        LicenseTestCase.setUp(self)
        write(os.path.join(self.root, 'src/OURS'),
              'Copyright 2016 The Fuchsia Authors\n' + BSD_TEXT)

    def test_holders_are_read_out_of_a_copyright_line(self):
        self.assertEqual(
            collect_licenses.copyright_holders(
                'Copyright (c) 2013 Red Hat Inc.  All rights reserved.'),
            set(['Red Hat Inc']))

    def test_a_placeholder_holder_is_ignored(self):
        # The Apache text has "Copyright [yyyy] [name of copyright owner]".
        self.assertEqual(
            collect_licenses.copyright_holders('Copyright [yyyy] [owner]'),
            set())

    def test_a_holder_the_licence_never_names_is_reported(self):
        write(os.path.join(self.component, 'include/ours.h'),
              'Copyright 2026 GoldenRiver Technology Co., Ltd.\n' + BSD_TEXT)
        coverage = self.scan(self.base())
        gaps = collect_licenses.attribution_gaps(self.component, coverage)
        self.assertEqual([(n, h) for n, h, _, _ in gaps],
                         [('ours', 'GoldenRiver Technology Co')])

    def test_a_holder_the_licence_names_is_not_reported(self):
        write(os.path.join(self.component, 'include/ours.h'),
              'Copyright 2016 The Fuchsia Authors\n' + BSD_TEXT)
        coverage = self.scan(self.base())
        self.assertEqual(
            collect_licenses.attribution_gaps(self.component, coverage), [])

    def test_a_declared_embedded_holder_is_not_reported(self):
        entries = self.base()
        entries[0]['embedded_holders'] = ['GoldenRiver']
        entries[0]['embedded_reason'] = 'the notice travels in the file'
        write(os.path.join(self.component, 'include/ours.h'),
              'Copyright 2026 GoldenRiver Technology Co., Ltd.\n' + BSD_TEXT)
        coverage = self.scan(entries)
        self.assertEqual(
            collect_licenses.attribution_gaps(self.component, coverage), [])

    def test_embedded_holders_without_a_reason_are_refused(self):
        entries = self.base()
        entries[0]['embedded_holders'] = ['Somebody']
        with self.assertRaises(collect_licenses.LicenseError) as caught:
            self.read(entries)
        self.assertIn('embedded_reason', str(caught.exception))

    def test_an_empty_embedded_holders_list_is_refused(self):
        entries = self.base()
        entries[0]['embedded_holders'] = []
        entries[0]['embedded_reason'] = 'because'
        self.assertRaises(collect_licenses.LicenseError, self.read, entries)


class OwnershipTest(LicenseTestCase):
    """Several parts are forks; a reader cannot tell ours from upstream's."""

    def test_the_notice_says_which_parts_are_ours(self):
        entries = self.base()
        entries[0]['owner'] = 'GoldenRiver Technology Co., Ltd.'
        coverage = self.scan(entries)
        collect_licenses.write(self.component, coverage)
        with open(os.path.join(self.component,
                               collect_licenses.NOTICE_NAME)) as handle:
            notice = handle.read()
        # Normalised: the sentence is wrapped, so the owner's name can
        # arrive with a newline in the middle of it.
        flat = ' '.join(notice.split())
        self.assertIn('held by GoldenRiver Technology Co., Ltd., alongside',
                      flat)
        self.assertIn('    ours', notice)
        self.assertNotIn('    theirs', notice.split('acrn')[0])

    def test_a_component_with_no_owner_is_not_claimed(self):
        coverage = self.scan(self.base())
        collect_licenses.write(self.component, coverage)
        with open(os.path.join(self.component,
                               collect_licenses.NOTICE_NAME)) as handle:
            self.assertNotIn('holds copyright', handle.read())

    def test_an_empty_owner_is_refused(self):
        entries = self.base()
        entries[0]['owner'] = '  '
        self.assertRaises(collect_licenses.LicenseError, self.read, entries)


class WriteTest(LicenseTestCase):

    def test_each_licence_text_travels_with_the_component(self):
        coverage = self.scan(self.base())
        collect_licenses.write(self.component, coverage)
        for name in ('ours', 'theirs'):
            path = os.path.join(self.component, collect_licenses.LICENSES_DIR,
                                name + '.txt')
            self.assertTrue(os.path.isfile(path), path)

    def test_the_notice_names_every_part_and_its_licence(self):
        coverage = self.scan(self.base())
        collect_licenses.write(self.component, coverage)
        with open(os.path.join(self.component,
                               collect_licenses.NOTICE_NAME)) as handle:
            notice = handle.read()
        self.assertIn('ours', notice)
        self.assertIn('theirs', notice)
        self.assertIn('BSD-3-Clause', notice)

    def test_a_component_split_across_prefixes_keeps_every_note(self):
        # The C library's headers, its archives and its debug build are all
        # musl, and each entry says something different about its own part.
        entries = self.base()
        entries.append({'prefix': 'include/openssl/deep', 'name': 'theirs',
                        'spdx': 'BSD-3-Clause',
                        'license_file': '//src/THEIRS',
                        'note': 'the deep part'})
        write(os.path.join(self.component, 'include/openssl/deep/x.h'),
              BSD_TEXT)
        coverage = self.scan(entries)
        collect_licenses.write(self.component, coverage)
        with open(os.path.join(self.component,
                               collect_licenses.NOTICE_NAME)) as handle:
            notice = handle.read()
        self.assertIn('a third party', notice)
        self.assertIn('the deep part', notice)

    def test_a_note_that_is_not_ascii_is_written(self):
        # python 2's default codec refuses these, and it would refuse during
        # an export, after the tree is already staged.
        entries = self.base()
        entries[1]['note'] = u'Sanfilippo\u2019s line editor'
        coverage = self.scan(entries)
        collect_licenses.write(self.component, coverage)
        with io.open(os.path.join(self.component,
                                  collect_licenses.NOTICE_NAME),
                     encoding='utf-8') as handle:
            self.assertIn(u'Sanfilippo\u2019s', handle.read())

    def test_the_notice_records_an_election(self):
        entries = self.base()
        entries[1]['spdx'] = 'GPL-2.0-or-later OR BSD-2-Clause'
        entries[1]['elected_spdx'] = 'BSD-2-Clause'
        coverage = self.scan(entries)
        collect_licenses.write(self.component, coverage)
        with open(os.path.join(self.component,
                               collect_licenses.NOTICE_NAME)) as handle:
            notice = handle.read()
        self.assertIn('elected   BSD-2-Clause', notice)


class FileMapTest(unittest.TestCase):
    """The per-file table a customer arrives at with one path in hand.

    NOTICE answers "what is in this delivery and under what". It does not
    answer "this file here -- which licence is that?", and counting files by
    component leaves the reader to re-derive the prefix rules from outside.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='filemap-test-')
        self.component = os.path.join(self.root, 'libddk')
        self.text = os.path.join(self.root, 'A.txt')
        with open(self.text, 'w') as handle:
            handle.write('Permission is hereby granted\n')
        for relative in ('include/a.h', 'lib/b.a', 'third/c.h'):
            path = os.path.join(self.component, relative)
            if not os.path.isdir(os.path.dirname(path)):
                os.makedirs(os.path.dirname(path))
            with open(path, 'w') as handle:
                handle.write('x\n')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def coverage(self, elected=None):
        entries = [
            {'prefix': '', 'name': 'ours', 'spdx': 'BSD-3-Clause',
             'license_file': self.text, 'note': 'ours'},
            {'prefix': 'third', 'name': 'theirs',
             'spdx': 'GPL-2.0-or-later OR BSD-2-Clause',
             'license_file': self.text, 'note': 'theirs'},
        ]
        if elected:
            entries[1]['elected_spdx'] = elected
        for entry in entries:
            entry['license_path'] = self.text
        return collect_licenses.scan(self.component, entries, set())

    def read(self):
        directory = os.path.join(self.component, 'LICENSES')
        if not os.path.isdir(directory):
            os.makedirs(directory)
        collect_licenses.write_file_map(directory, self.coverage())
        with open(os.path.join(directory,
                               collect_licenses.FILE_MAP_NAME)) as handle:
            return handle.read()

    def test_every_delivered_file_is_listed(self):
        text = self.read()
        for relative in ('include/a.h', 'lib/b.a', 'third/c.h'):
            self.assertIn(relative, text)

    def test_a_file_names_the_component_that_claims_it(self):
        rows = [line.split() for line in self.read().splitlines()
                if line.startswith(('include/', 'lib/', 'third/'))]
        found = dict((row[0], row[1]) for row in rows)
        self.assertEqual({'include/a.h': 'ours', 'lib/b.a': 'ours',
                          'third/c.h': 'theirs'}, found)

    def test_an_election_is_shown_beside_the_expression(self):
        # A customer asking about libfdt.a has to see that the permissive
        # half was taken, not only that the file is dual licensed.
        directory = os.path.join(self.component, 'LICENSES')
        os.makedirs(directory)
        collect_licenses.write_file_map(directory,
                                        self.coverage('BSD-2-Clause'))
        with open(os.path.join(directory,
                               collect_licenses.FILE_MAP_NAME)) as handle:
            text = handle.read()
        self.assertIn('elected BSD-2-Clause', text)

    def test_the_licence_directory_is_not_listed_as_content(self):
        # The texts and this table are the notice, not the thing licensed.
        self.assertNotIn('LICENSES/', self.read().split('Not listed:')[1]
                         .split('FILE')[0].replace('LICENSES/ itself', ''))

    def test_it_is_written_by_write(self):
        collect_licenses.write(self.component, self.coverage())
        self.assertTrue(os.path.isfile(os.path.join(
            self.component, 'LICENSES', collect_licenses.FILE_MAP_NAME)))


class VerifyTest(unittest.TestCase):
    """The delivery checked against its own table, reading no declaration.

    The declaration is what produced the delivery; running the same input
    through the same rules would agree with itself whatever it did. This is
    the artefact as shipped, with only what is inside it -- which is what an
    auditor holds and what a customer can be handed to repeat.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='verify-test-')
        self.component = os.path.join(self.root, 'libddk')
        text = os.path.join(self.root, 'A.txt')
        with open(text, 'w') as handle:
            handle.write('Permission is hereby granted\n')
        for relative in ('include/a.h', 'lib/b.a'):
            path = os.path.join(self.component, relative)
            if not os.path.isdir(os.path.dirname(path)):
                os.makedirs(os.path.dirname(path))
            with open(path, 'w') as handle:
                handle.write('x\n')
        entry = {'prefix': '', 'name': 'ours', 'spdx': 'BSD-3-Clause',
                 'license_file': text, 'note': 'ours', 'license_path': text}
        coverage = collect_licenses.scan(self.component, [entry], set())
        collect_licenses.write(self.component, coverage)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_a_complete_delivery_passes(self):
        result = collect_licenses.verify(self.component)
        self.assertEqual(2, result['files'])
        self.assertEqual(1, result['components'])

    def test_a_file_with_no_licence_is_caught(self):
        # The one that matters: something left the building unassigned.
        with open(os.path.join(self.component, 'include/sneaked.h'),
                  'w') as handle:
            handle.write('x\n')
        try:
            collect_licenses.verify(self.component)
            self.fail('an unlicensed file was accepted')
        except collect_licenses.LicenseError as error:
            self.assertIn('include/sneaked.h', str(error))

    def test_a_row_with_no_file_is_caught(self):
        # The table would then describe something other than this delivery.
        os.remove(os.path.join(self.component, 'lib/b.a'))
        self.assertRaises(collect_licenses.LicenseError,
                          collect_licenses.verify, self.component)

    def test_a_component_with_no_text_is_caught(self):
        os.remove(os.path.join(self.component, 'LICENSES', 'ours.txt'))
        self.assertRaises(collect_licenses.LicenseError,
                          collect_licenses.verify, self.component)

    def test_a_delivery_with_no_table_at_all_is_caught(self):
        os.remove(os.path.join(self.component, 'LICENSES',
                               collect_licenses.FILE_MAP_NAME))
        self.assertRaises(collect_licenses.LicenseError,
                          collect_licenses.verify, self.component)

    def test_a_release_stamp_needs_no_licence_row(self):
        # It is the delivery saying which release it is, not content. The
        # set is named once because it was written out by hand in three
        # places and a file added to one was reported unlicensed by the
        # other two.
        with open(os.path.join(self.component, 'RELEASE'), 'w') as handle:
            handle.write('20260909-abc\n')
        collect_licenses.verify(self.component)

    def test_the_notice_is_not_expected_to_have_a_licence(self):
        # It is the notice, not a thing being licensed.
        self.assertTrue(os.path.isfile(os.path.join(self.component,
                                                    'NOTICE')))
        collect_licenses.verify(self.component)


class ProvenanceTest(unittest.TestCase):
    """Texts fetched from upstream, and the record that says they still are."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='provenance-test-')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def provenance_dir(self):
        """A licence directory with one text and a record of where it came
        from."""
        directory = os.path.join(self.root, 'licenses')
        os.makedirs(directory)
        with open(os.path.join(directory, 'LLD.LICENSE.TXT'), 'wb') as handle:
            handle.write(b'University of Illinois/NCSA\n')
        record = {
            'format_version': 1,
            'tag': 'llvmorg-7.0.0',
            'files': {
                'LLD.LICENSE.TXT': {
                    'path': 'lld/LICENSE.TXT',
                    'url': 'https://example.invalid/lld/LICENSE.TXT',
                    # sha256 of the text written above.
                    'sha256': hashlib.sha256(
                        b'University of Illinois/NCSA\n').hexdigest(),
                    'bytes': 27,
                },
            },
        }
        with open(os.path.join(directory, collect_licenses.PROVENANCE_NAME),
                  'w') as handle:
            json.dump(record, handle)
        return directory

    def test_matching_texts_pass(self):
        directory = self.provenance_dir()
        self.assertIsNotNone(collect_licenses.check_provenance(directory))

    def test_an_edited_text_is_caught(self):
        # These are fetched once and then never read again, which is exactly
        # how a licence notice drifts without anyone noticing.
        directory = self.provenance_dir()
        with open(os.path.join(directory, 'LLD.LICENSE.TXT'), 'ab') as handle:
            handle.write(b'\n')
        try:
            collect_licenses.check_provenance(directory)
            self.fail('an edited licence text was accepted')
        except collect_licenses.LicenseError as error:
            # The message has to say where to get the right one back.
            self.assertIn('https://example.invalid', str(error))

    def test_a_missing_text_is_caught(self):
        directory = self.provenance_dir()
        os.remove(os.path.join(directory, 'LLD.LICENSE.TXT'))
        self.assertRaises(collect_licenses.LicenseError,
                          collect_licenses.check_provenance, directory)

    def test_no_record_is_not_an_error(self):
        # Everywhere else in the delivery a licence text lives beside the
        # code it covers and needs no provenance of its own.
        directory = self.provenance_dir()
        os.remove(os.path.join(directory, collect_licenses.PROVENANCE_NAME))
        self.assertIsNone(collect_licenses.check_provenance(directory))


if __name__ == '__main__':
    unittest.main()

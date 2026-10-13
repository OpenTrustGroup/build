#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for trim_ddk.py.

Trimming removes files from a delivery, so the tests are mostly about the
ways it could remove one it should not: a header reached only through an
#ifdef branch this build did not take, a library the linker was pointed at
but took nothing from, and an entry point nothing includes yet.
"""

from __future__ import print_function

import json
import os
import struct
import shutil
import sys
import tempfile
import subprocess
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import trim_ddk  # noqa: E402


def write(path, content):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, 'w') as handle:
        handle.write(content)


class TrimTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='trim-test-')
        self.component = os.path.join(self.root, 'libddk')
        write(os.path.join(self.component, 'include/used.h'),
              '#include <helper.h>\n')
        write(os.path.join(self.component, 'include/helper.h'), '\n')
        write(os.path.join(self.component, 'include/unused.h'), '\n')
        write(os.path.join(self.component, 'include/guarded.h'),
              '#ifdef SOMETHING\n#include <only_when_guarded.h>\n#endif\n')
        write(os.path.join(self.component, 'include/only_when_guarded.h'),
              '\n')
        write(os.path.join(self.component, 'lib/libused.a'), 'ar\n')
        write(os.path.join(self.component, 'lib/libnamed.a'), 'ar\n')
        write(os.path.join(self.component, 'lib/libunused.a'), 'ar\n')
        write(os.path.join(self.component, 'sysroot/include/stdio.h'), '\n')
        self.contents(['include/used.h', 'include/helper.h',
                       'include/unused.h', 'include/guarded.h',
                       'include/only_when_guarded.h', 'lib/libused.a',
                       'lib/libnamed.a', 'lib/libunused.a',
                       'sysroot/include/stdio.h'])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def contents(self, paths):
        write(os.path.join(self.component, trim_ddk.CONTENTS_NAME),
              json.dumps({'component': 'libddk',
                          'files': [{'path': p} for p in paths]}))

    def closure(self, entries, name='closure.json'):
        path = os.path.join(self.root, name)
        write(path, json.dumps({'libddk': entries}))
        return path

    def policy(self, keep):
        path = os.path.join(self.root, 'policy.json')
        write(path, json.dumps({'format_version': 1, 'keep': keep}))
        return path

    def modules(self, libs):
        root = os.path.join(self.root, 'src')
        write(os.path.join(root, 'garnet/x/x.ddk.mk'),
              'MODULE_NAME := x\nMODULE_LIBS := %s\n' % ' '.join(libs))
        return root

    def plan(self, measured, policy=None, module_sources=(), subset=False,
             keep_all=False):
        return trim_ddk.plan(self.component, [self.closure(measured)],
                             trim_ddk.read_policy(policy), module_sources,
                             subset, keep_all)


class SubsetTest(TrimTestCase):
    """One closure, two components: a half builds against both.

    libddk is split -- what both halves need sits beside them, what only one
    half needs travels inside it -- and a half's sealed build measures the
    two together.  Trimming either alone, the files the other answered are
    not this one's to hold, and they are not evidence that the closure
    belongs to some other component either.
    """

    def test_a_file_the_component_does_not_have_is_an_error(self):
        # The check earns its keep on the ordinary case: a closure from
        # another component is how a trim deletes what a build needed.
        try:
            self.plan(['include/used.h', 'include/elsewhere.h'])
            self.fail('a closure naming an absent file was accepted')
        except trim_ddk.TrimError as error:
            self.assertIn('include/elsewhere.h', str(error))

    def test_with_subset_it_belongs_to_the_other_part(self):
        _, kept, _ = self.plan(['include/used.h', 'include/elsewhere.h'],
                               subset=True)
        self.assertEqual(kept['include/used.h'], 'measured')
        self.assertNotIn('include/elsewhere.h', kept)

    def test_subset_still_drops_what_nothing_measured(self):
        _, _, dropped = self.plan(['include/used.h', 'include/elsewhere.h'],
                                  subset=True)
        self.assertIn('include/unused.h', dropped)


class KeepAllTest(TrimTestCase):
    """A component derived as one half's closure has nothing to trim.

    It was planned from what that half reads and links, not scraped from a
    build, and the half's own measurement answers for the component beside
    it as well.  Trimming against that measurement cuts the headers this
    component's own headers include, because the walk starts from the
    measured set.
    """

    def test_everything_is_kept(self):
        files, kept, dropped = self.plan(['include/used.h'], keep_all=True)
        self.assertEqual(sorted(files), sorted(kept))
        self.assertEqual([], dropped)
        self.assertEqual('whole', kept['include/unused.h'])

    def test_it_needs_no_closure_to_be_right(self):
        files, kept, dropped = self.plan([], keep_all=True)
        self.assertEqual(sorted(files), sorted(kept))
        self.assertEqual([], dropped)


class CommandLineTest(TrimTestCase):
    """--keep-all runs without a closure; anything else still demands one."""

    def run_tool(self, *arguments):
        command = [sys.executable,
                   os.path.join(DDK_DIR, 'tools', 'trim_ddk.py'),
                   '--component', self.component,
                   '--report', os.path.join(self.root, 'report.json')]
        process = subprocess.Popen(command + list(arguments),
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        out, err = process.communicate()
        return process.returncode, (out + err).decode('utf-8')

    def test_keep_all_needs_no_closure(self):
        out = os.path.join(self.root, 'kept')
        code, text = self.run_tool('--keep-all', '--out', out)
        self.assertEqual(0, code, text)
        self.assertTrue(os.path.isfile(
            os.path.join(out, 'include', 'unused.h')))

    def test_without_keep_all_a_closure_is_required(self):
        code, text = self.run_tool()
        self.assertNotEqual(0, code)
        self.assertIn('--closure is required', text)


class KeepTest(TrimTestCase):

    def test_measured_files_are_kept(self):
        _, kept, _ = self.plan(['include/used.h', 'lib/libused.a'])
        self.assertEqual(kept['include/used.h'], 'measured')
        self.assertEqual(kept['lib/libused.a'], 'measured')

    def test_unmeasured_files_are_dropped(self):
        _, _, dropped = self.plan(['include/used.h'])
        self.assertIn('include/unused.h', dropped)
        self.assertIn('lib/libunused.a', dropped)

    def test_a_header_a_kept_header_includes_is_kept(self):
        _, kept, dropped = self.plan(['include/used.h'])
        self.assertEqual(kept['include/helper.h'], 'reachable')
        self.assertNotIn('include/helper.h', dropped)

    def test_a_guarded_include_is_kept_even_though_it_was_not_taken(self):
        # The customer's configuration is not ours.  An include the compiler
        # never followed is still an include, and dropping it breaks a build
        # we cannot see.
        _, kept, _ = self.plan(['include/guarded.h'])
        self.assertEqual(kept['include/only_when_guarded.h'], 'reachable')

    def test_a_library_the_linker_took_nothing_from_is_kept_when_declared(
            self):
        # -lnamed fails on a missing file, not on an unused one, so a linker
        # trace is not enough on its own.
        _, kept, _ = self.plan(['include/used.h'],
                               module_sources=[self.modules(['named'])])
        self.assertEqual(kept['lib/libnamed.a'], 'declared')

    def test_policy_keeps_a_file_nothing_reaches(self):
        _, kept, _ = self.plan(
            ['include/used.h'],
            policy=self.policy([{'prefix': 'include/unused.h',
                                 'reason': 'entry point'}]))
        self.assertEqual(kept['include/unused.h'], 'policy:include/unused.h')

    def test_policy_keeps_a_whole_subtree(self):
        _, kept, _ = self.plan(
            ['include/used.h'],
            policy=self.policy([{'prefix': 'sysroot',
                                 'reason': 'the C library, whole'}]))
        self.assertEqual(kept['sysroot/include/stdio.h'], 'policy:sysroot')

    def test_the_manifest_is_always_kept(self):
        _, kept, _ = self.plan(['include/used.h'])
        self.assertIn(trim_ddk.CONTENTS_NAME, kept)


class MetadataTest(TrimTestCase):
    """The notice and the licence texts are not content to be measured."""

    def setUp(self):
        TrimTestCase.setUp(self)
        write(os.path.join(self.component, trim_ddk.NOTICE_NAME), 'NOTICE\n')
        write(os.path.join(self.component, trim_ddk.LICENSES_DIR, 'x.txt'),
              'a licence\n')

    def test_the_notice_and_licences_survive_a_trim(self):
        _, kept, dropped = self.plan(['include/used.h'])
        self.assertEqual(kept[trim_ddk.NOTICE_NAME], 'policy:metadata')
        self.assertEqual(kept[trim_ddk.LICENSES_DIR + '/x.txt'],
                         'policy:metadata')
        self.assertNotIn(trim_ddk.NOTICE_NAME, dropped)


def make_elf(section_names):
    """A minimal 64-bit little-endian ELF carrying the named sections.

    Built by hand so the test does not need a compiler, and so the scanner is
    checked against bytes rather than against whatever a toolchain happened to
    produce.
    """
    names = b'\0'
    offsets = {}
    for name in section_names + ['.shstrtab']:
        offsets[name] = len(names)
        names += name.encode('ascii') + b'\0'

    ordered = section_names + ['.shstrtab']
    shentsize = 64
    header_size = 64
    shoff = header_size
    names_off = shoff + shentsize * len(ordered)

    elf = bytearray()
    elf += b'\x7fELF' + bytes(bytearray([2, 1, 1, 0])) + b'\0' * 8
    elf += struct.pack('<HHI', 1, 183, 1)          # type, aarch64, version
    elf += struct.pack('<QQQ', 0, 0, shoff)        # entry, phoff, shoff
    elf += struct.pack('<IHHH', 0, header_size, 0, 0)
    elf += struct.pack('<HHH', shentsize, len(ordered), len(ordered) - 1)
    assert len(elf) == header_size, len(elf)

    for name in ordered:
        size = len(names) if name == '.shstrtab' else 0
        offset = names_off if name == '.shstrtab' else 0
        elf += struct.pack('<IIQQQQIIQQ', offsets[name], 1, 0, 0, offset,
                           size, 0, 0, 1, 0)
    elf += names
    return bytes(elf)


def make_archive(members):
    """A Unix archive of (name, bytes) members."""
    out = bytearray(b'!<arch>\n')
    for name, body in members:
        header = ('%-16s%-12s%-6s%-6s%-8s%-10d`\n'
                  % (name + '/', '0', '0', '0', '644', len(body)))
        out += header.encode('ascii') + body
        if len(body) % 2:
            out += b'\n'
    return bytes(out)


class DebugSectionTest(TrimTestCase):
    """The gate reads the bytes rather than asking the tool it is checking."""

    def elf(self, name, sections):
        path = os.path.join(self.component, name)
        with open(path, 'wb') as handle:
            handle.write(make_elf(sections))
        return path

    def test_an_elf_with_debug_sections_is_reported(self):
        path = self.elf('lib/x.so', ['.text', '.debug_info', '.debug_line'])
        self.assertEqual(trim_ddk.debug_sections(path),
                         ['.debug_info', '.debug_line'])

    def test_a_stripped_elf_is_clean(self):
        path = self.elf('lib/x.so', ['.text', '.rodata'])
        self.assertEqual(trim_ddk.debug_sections(path), [])

    def test_an_archive_is_read_member_by_member(self):
        path = os.path.join(self.component, 'lib/x.a')
        with open(path, 'wb') as handle:
            handle.write(make_archive([
                ('clean.o', make_elf(['.text'])),
                ('dirty.o', make_elf(['.text', '.debug_str'])),
            ]))
        self.assertEqual(trim_ddk.debug_sections(path), ['.debug_str'])

    def test_a_file_that_is_not_elf_is_not_an_error(self):
        # musl installs linker scripts named libm.so and libpthread.so.
        path = os.path.join(self.component, 'lib/libm.so')
        with open(path, 'w') as handle:
            handle.write('/* a linker script */\nGROUP ( libc.so )\n')
        self.assertEqual(trim_ddk.debug_sections(path), [])
        self.assertIsNone(trim_ddk._binary_kind(path))

    def test_the_gate_names_every_binary_that_still_has_debug_information(
            self):
        self.elf('lib/clean.so', ['.text'])
        self.elf('lib/dirty.so', ['.text', '.debug_info'])
        offenders = trim_ddk.check_stripped(self.component)
        self.assertEqual([path for path, _ in offenders], ['lib/dirty.so'])

    def test_the_gate_passes_a_component_with_nothing_to_strip(self):
        self.elf('lib/clean.so', ['.text'])
        self.assertEqual(trim_ddk.check_stripped(self.component), [])


class RedactionTest(TrimTestCase):
    """Stripping removes DWARF; it does not touch what assert() left behind."""

    def binary(self, name, body):
        path = os.path.join(self.component, name)
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'wb') as handle:
            handle.write(make_elf(['.text']) + body)
        return path

    def read(self, path):
        with open(path, 'rb') as handle:
            return handle.read()

    def test_a_path_is_cut_down_to_its_basename(self):
        path = self.binary('lib/x.a',
                           b'\x00system/ulib/fs/connection.cpp\x00')
        trim_ddk.redact_source_paths(self.component)
        self.assertIn(b'\x00connection.cpp\x00', self.read(path))
        self.assertNotIn(b'system/ulib', self.read(path))

    def test_the_file_does_not_change_size(self):
        # Relocations point at fixed offsets; the string may only get shorter
        # inside the buffer it already has.
        path = self.binary('lib/x.a', b'\x00a/b/c.cpp\x00')
        before = len(self.read(path))
        trim_ddk.redact_source_paths(self.component)
        self.assertEqual(len(self.read(path)), before)

    def test_a_path_laid_down_after_code_is_caught(self):
        # Fourteen protobuf paths survived into the linked binary because they
        # follow an instruction rather than a NUL.
        path = self.binary('lib/x.a', b'\xc0\x03_\xd6a/b/c.cc\x00')
        trim_ddk.redact_source_paths(self.component)
        self.assertNotIn(b'a/b/c.cc', self.read(path))
        self.assertIn(b'c.cc\x00', self.read(path))

    def test_a_message_that_merely_contains_a_path_is_left_alone(self):
        path = self.binary('lib/x.a', b'\x00cannot open dir/file.c\x00')
        trim_ddk.redact_source_paths(self.component)
        self.assertIn(b'cannot open dir/file.c', self.read(path))

    def test_a_bare_filename_is_left_alone(self):
        path = self.binary('lib/x.a', b'\x00config.h\x00')
        trim_ddk.redact_source_paths(self.component)
        self.assertIn(b'\x00config.h\x00', self.read(path))

    def test_the_gate_reports_what_redaction_missed(self):
        self.binary('lib/x.a', b'\x00a/b/c.cpp\x00')
        self.assertEqual([p for p, _ in trim_ddk.check_redacted(
            self.component)], ['lib/x.a'])

    def test_the_gate_passes_a_redacted_component(self):
        self.binary('lib/x.a', b'\x00a/b/c.cpp\x00')
        trim_ddk.redact_source_paths(self.component)
        self.assertEqual(trim_ddk.check_redacted(self.component), [])


class RefusalTest(TrimTestCase):

    def test_a_closure_from_a_different_component_is_refused(self):
        with self.assertRaises(trim_ddk.TrimError) as caught:
            self.plan(['include/used.h', 'include/from_elsewhere.h'])
        self.assertIn('different component', str(caught.exception))

    def test_an_empty_closure_is_refused(self):
        # Trimming against nothing would empty the component and report
        # success.
        self.assertRaises(trim_ddk.TrimError, self.plan, [])

    def test_a_directory_that_is_not_a_component_is_refused(self):
        self.assertRaises(trim_ddk.TrimError, trim_ddk.read_component,
                          self.root)

    def test_a_policy_entry_without_a_reason_is_refused(self):
        path = os.path.join(self.root, 'bad.json')
        write(path, json.dumps({'format_version': 1,
                                'keep': [{'prefix': 'include/unused.h'}]}))
        self.assertRaises(trim_ddk.TrimError, trim_ddk.read_policy, path)

    def test_an_escaping_policy_prefix_is_refused(self):
        path = os.path.join(self.root, 'bad.json')
        write(path, json.dumps({'format_version': 1,
                                'keep': [{'prefix': '../elsewhere',
                                          'reason': 'no'}]}))
        self.assertRaises(trim_ddk.TrimError, trim_ddk.read_policy, path)


class WriteTest(TrimTestCase):

    def test_the_trimmed_tree_holds_exactly_the_keep_set(self):
        files, kept, dropped = self.plan(['include/used.h'])
        out = os.path.join(self.root, 'trimmed')
        trim_ddk.write(self.component, out, kept, dropped,
                       trim_ddk.read_policy(None))
        on_disk = set()
        for current, _, names in os.walk(out):
            for name in names:
                on_disk.add(os.path.relpath(os.path.join(current, name), out)
                            .replace(os.sep, '/'))
        self.assertEqual(on_disk, set(kept))

    def test_the_report_does_not_go_into_the_component(self):
        # It names every file the trim removed, which is a map of what is
        # there and was withheld -- published by the step whose whole purpose
        # is to withhold it.
        files, kept, dropped = self.plan(['include/used.h'])
        out = os.path.join(self.root, 'trimmed')
        report = trim_ddk.write(self.component, out, kept, dropped,
                                trim_ddk.read_policy(None))
        self.assertTrue(report['dropped_files'])
        on_disk = set()
        for current, _, names in os.walk(out):
            on_disk.update(names)
        self.assertNotIn('TRIM_REPORT.json', on_disk)

    def test_the_manifest_loses_the_dropped_entries(self):
        files, kept, dropped = self.plan(['include/used.h'])
        out = os.path.join(self.root, 'trimmed')
        trim_ddk.write(self.component, out, kept, dropped,
                       trim_ddk.read_policy(None))
        with open(os.path.join(out, trim_ddk.CONTENTS_NAME)) as handle:
            contents = json.load(handle)
        listed = {e['path'] for e in contents['files']}
        self.assertNotIn('include/unused.h', listed)
        self.assertIn('include/used.h', listed)
        self.assertTrue(contents['trimmed'])

    def test_the_report_names_every_dropped_file(self):
        files, kept, dropped = self.plan(['include/used.h'])
        out = os.path.join(self.root, 'trimmed')
        report = trim_ddk.write(self.component, out, kept, dropped,
                                trim_ddk.read_policy(None))
        self.assertEqual(sorted(report['dropped_files']), sorted(dropped))
        self.assertEqual(report['dropped'], len(dropped))


if __name__ == '__main__':
    unittest.main()

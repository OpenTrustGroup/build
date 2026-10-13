#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for export_abi.py.

The property that matters is the one a signature digest cannot give you:
reordering two syscalls without touching either declaration is exactly the
change that breaks a prebuilt library, and it must move the fingerprint.
"""

from __future__ import print_function

import json
import os
import shutil
import sys
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import export_abi  # noqa: E402


ABIGEN = """\
# a comment
syscall clock_get
    (clock_id: uint32_t)
    returns (zx_time_t);

syscall nanosleep blocking
    (deadline: zx_time_t)
    returns (zx_status_t);

syscall ticks_get vdsocall
    returns (uint64_t);
"""

NUMBERS = """\
#define ZX_SYS_clock_get 0
#define ZX_SYS_nanosleep 1
#define ZX_SYS_COUNT 2
"""


class AbiTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='abi-test-')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name, content):
        path = os.path.join(self.root, name)
        with open(path, 'w') as handle:
            handle.write(content)
        return path


class ReadTest(AbiTestCase):

    def test_every_declaration_is_read(self):
        syscalls = export_abi.read_abigen(self.write('a', ABIGEN))
        self.assertEqual(sorted(syscalls),
                         ['clock_get', 'nanosleep', 'ticks_get'])

    def test_a_declaration_is_joined_onto_one_line(self):
        syscalls = export_abi.read_abigen(self.write('a', ABIGEN))
        self.assertEqual(syscalls['clock_get'],
                         '(clock_id: uint32_t) returns (zx_time_t)')

    def test_attributes_are_part_of_the_declaration(self):
        syscalls = export_abi.read_abigen(self.write('a', ABIGEN))
        self.assertTrue(syscalls['nanosleep'].startswith('blocking'))

    def test_reformatting_is_not_an_abi_change(self):
        spaced = ABIGEN.replace('    (clock_id', '\n\n        (clock_id')
        first = export_abi.read_abigen(self.write('a', ABIGEN))
        second = export_abi.read_abigen(self.write('b', spaced))
        self.assertEqual(first, second)

    def test_a_declaration_twice_is_refused(self):
        self.assertRaises(export_abi.AbiError, export_abi.read_abigen,
                          self.write('a', ABIGEN + ABIGEN))

    def test_an_unterminated_declaration_is_refused(self):
        self.assertRaises(export_abi.AbiError, export_abi.read_abigen,
                          self.write('a', 'syscall foo\n    (a: int)\n'))

    def test_the_count_is_not_read_as_a_syscall(self):
        numbers = export_abi.read_ordinals(self.write('n', NUMBERS))
        self.assertEqual(numbers, {'clock_get': 0, 'nanosleep': 1})


class FingerprintTest(AbiTestCase):

    def build(self, abigen=ABIGEN, numbers=NUMBERS):
        return export_abi.build(
            export_abi.read_abigen(self.write('a', abigen)),
            export_abi.read_ordinals(self.write('n', numbers)))

    def test_a_vdsocall_is_declared_but_not_numbered(self):
        record, _ = self.build()
        self.assertTrue(record['syscalls']['clock_get']['numbered'])
        self.assertFalse(record['syscalls']['ticks_get']['numbered'])
        self.assertEqual(record['count'], 3)
        self.assertEqual(record['numbered'], 2)

    def test_reordering_moves_the_fingerprint_and_no_digest(self):
        # The change that breaks a prebuilt library and leaves every
        # declaration untouched.
        before_record, before = self.build()
        swapped = NUMBERS.replace('clock_get 0', 'clock_get 1').replace(
            'nanosleep 1', 'nanosleep 0')
        after_record, after = self.build(numbers=swapped)
        self.assertNotEqual(before, after)
        self.assertEqual(
            dict((n, e['digest']) for n, e in before_record['syscalls'].items()),
            dict((n, e['digest']) for n, e in after_record['syscalls'].items()))

    def test_changing_a_declaration_moves_its_digest(self):
        changed = ABIGEN.replace('clock_id: uint32_t', 'clock_id: uint64_t')
        before, _ = self.build()
        after, _ = self.build(abigen=changed)
        self.assertNotEqual(before['syscalls']['clock_get']['digest'],
                            after['syscalls']['clock_get']['digest'])
        self.assertEqual(before['syscalls']['nanosleep']['digest'],
                         after['syscalls']['nanosleep']['digest'])

    def test_the_same_inputs_give_the_same_fingerprint(self):
        self.assertEqual(self.build()[1], self.build()[1])

    def test_a_number_with_no_declaration_is_refused(self):
        # The two inputs would then describe different builds.
        extra = NUMBERS + '#define ZX_SYS_from_elsewhere 9\n'
        with self.assertRaises(export_abi.AbiError) as caught:
            self.build(numbers=extra)
        self.assertIn('from_elsewhere', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

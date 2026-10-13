#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for ddk_version.py.

The comparison exists to answer one question under time pressure -- did the
ABI move, and if so what -- so the tests are about the answers being right and
the noise being absent.
"""

from __future__ import print_function

import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import ddk_version  # noqa: E402


def description(fingerprint='f0', syscalls=None, revisions=None, dirty=None,
                toolchain=None):
    return {
        'format_version': ddk_version.FORMAT_VERSION,
        'from': 'component',
        'origin': '/somewhere',
        'component': 'libddk',
        'abi': {
            'fingerprint': fingerprint,
            'count': len(syscalls or {}),
            'syscalls': syscalls if syscalls is not None else {
                'clock_get': {'digest': 'a', 'numbered': True},
                'nanosleep': {'digest': 'b', 'numbered': True},
            },
        },
        'build': {
            'built_at': '2026-01-01T00:00:00Z',
            'source_date': '2026-01-01T00:00:00Z',
            'dirty': dirty or [],
            'revisions': revisions if revisions is not None
            else {'garnet': 'aaa', 'zircon': 'bbb'},
            'toolchain': toolchain,
        },
    }


class CompareTest(unittest.TestCase):

    def test_identical_descriptions_agree(self):
        result = ddk_version.compare(description(), description())
        self.assertIs(result['abi_same'], True)
        self.assertEqual(result['changed'], [])
        self.assertEqual(result['revisions_moved'], [])

    def test_a_changed_declaration_is_named(self):
        right = description(fingerprint='f1')
        right['abi']['syscalls']['nanosleep']['digest'] = 'changed'
        result = ddk_version.compare(description(), right)
        self.assertIs(result['abi_same'], False)
        self.assertEqual(result['changed'], ['nanosleep'])

    def test_an_added_and_a_removed_syscall_are_separated(self):
        right = description(fingerprint='f1', syscalls={
            'clock_get': {'digest': 'a', 'numbered': True},
            'brand_new': {'digest': 'c', 'numbered': True},
        })
        result = ddk_version.compare(description(), right)
        self.assertEqual(result['added'], ['brand_new'])
        self.assertEqual(result['removed'], ['nanosleep'])

    def test_a_syscall_that_left_the_kernel_is_reported(self):
        right = description(fingerprint='f1')
        right['abi']['syscalls']['nanosleep']['numbered'] = False
        result = ddk_version.compare(description(), right)
        self.assertEqual(result['renumbered'], ['nanosleep'])
        self.assertEqual(result['changed'], [])

    def test_only_the_numbers_moving_leaves_every_digest_alone(self):
        # The case that breaks a prebuilt library and shows nowhere else.
        result = ddk_version.compare(description(), description('f1'))
        self.assertIs(result['abi_same'], False)
        self.assertEqual(result['changed'], [])
        self.assertEqual(result['added'], [])
        self.assertEqual(result['removed'], [])

    def test_a_missing_fingerprint_is_not_an_answer(self):
        result = ddk_version.compare(description(), description(None))
        self.assertIsNone(result['abi_same'])

    def test_only_revisions_both_sides_name_can_have_moved(self):
        left = description(revisions={'garnet': 'aaa', 'zircon': 'bbb'})
        right = description(revisions={'garnet': 'ccc'})
        result = ddk_version.compare(left, right)
        self.assertEqual(result['revisions_moved'],
                         [('garnet', 'aaa', 'ccc')])
        self.assertEqual(result['revisions_unrecorded'], ['zircon'])

    def test_a_dirty_tree_on_either_side_is_reported(self):
        result = ddk_version.compare(description(), description(dirty=['x']))
        self.assertEqual(result['dirty'], ['x'])

    def test_something_that_is_not_a_description_is_refused(self):
        self.assertRaises(ddk_version.VersionError, ddk_version.compare,
                          {'format_version': 99}, description())


class ReportTest(unittest.TestCase):

    def render(self, left, right):
        out = io.StringIO() if str is not bytes else io.BytesIO()
        ddk_version.report(ddk_version.compare(left, right), out)
        value = out.getvalue()
        return value.decode('utf-8') if isinstance(value, bytes) else value

    def test_an_unchanged_abi_says_a_driver_still_runs(self):
        self.assertIn('identical', self.render(description(), description()))

    def test_numbers_moving_alone_points_at_the_abigen(self):
        text = self.render(description(), description('f1'))
        self.assertIn('what moved is the numbers', text)
        self.assertIn('syscalls.abigen', text)

    def test_a_changed_declaration_does_not_point_at_the_abigen(self):
        right = description(fingerprint='f1')
        right['abi']['syscalls']['nanosleep']['digest'] = 'changed'
        text = self.render(description(), right)
        self.assertIn('nanosleep', text)
        self.assertNotIn('what moved is the numbers', text)


class ToolchainCompareTest(unittest.TestCase):
    """A toolchain change is invisible in git: the compiler is downloaded."""

    OLD = {'clang_stamp': '9388aa0d', 'clang_release': '7.0.0'}
    NEW = {'clang_stamp': 'ffffffff', 'clang_release': '18.1.0'}

    def render(self, left, right):
        stream = io.StringIO() if str is not bytes else io.BytesIO()
        ddk_version.report(ddk_version.compare(left, right), stream)
        return stream.getvalue()

    def test_the_same_toolchain_is_not_a_finding(self):
        result = ddk_version.compare(description(toolchain=self.OLD),
                                     description(toolchain=self.OLD))
        self.assertIsNone(result['toolchain_moved'])

    def test_a_moved_toolchain_is_reported(self):
        result = ddk_version.compare(description(toolchain=self.OLD),
                                     description(toolchain=self.NEW))
        self.assertEqual((self.OLD, self.NEW), result['toolchain_moved'])

    def test_the_report_says_it_is_not_an_abi_problem(self):
        text = self.render(description(toolchain=self.OLD),
                           description(toolchain=self.NEW))
        self.assertIn('TOOLCHAIN CHANGED', text)
        self.assertIn('not an ABI problem', text)
        self.assertIn('18.1.0', text)

    def test_an_older_delivery_that_recorded_none_is_not_a_finding(self):
        # The field is newer than some deliveries. Calling a missing record a
        # change would cry wolf on every one of them.
        result = ddk_version.compare(description(toolchain=None),
                                     description(toolchain=self.NEW))
        self.assertIsNone(result['toolchain_moved'])


class DescribeComponentTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='version-test-')
        self.component = os.path.join(self.root, 'libddk')
        os.makedirs(os.path.join(self.component, 'abi'))
        self.write('abi/ABI.fingerprint', 'deadbeef\n')
        self.write('abi/SYSCALLS.json', json.dumps({
            'count': 1,
            'syscalls': {'clock_get': {'digest': 'a', 'numbered': True}}}))
        self.write('BUILD_INFO.json', json.dumps({
            'component': 'libddk', 'built_at': 'x', 'source_date': 'y',
            'dirty': [], 'revisions': {'zircon': {'commit': 'abc'}},
            'toolchain': {'clang_stamp': '9388aa0d',
                          'clang_release': '7.0.0'}}))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relative, content):
        with open(os.path.join(self.component, relative), 'w') as handle:
            handle.write(content)

    def test_a_component_describes_itself(self):
        described = ddk_version.describe_component(self.component)
        self.assertEqual(described['abi']['fingerprint'], 'deadbeef')
        self.assertEqual(described['build']['revisions'], {'zircon': 'abc'})
        self.assertEqual(described['build']['toolchain']['clang_stamp'],
                         '9388aa0d')

    def test_a_component_without_an_abi_record_is_refused(self):
        os.remove(os.path.join(self.component, 'abi/ABI.fingerprint'))
        self.assertRaises(ddk_version.VersionError,
                          ddk_version.describe_component, self.component)

    def test_a_component_without_build_info_is_refused(self):
        os.remove(os.path.join(self.component, 'BUILD_INFO.json'))
        self.assertRaises(ddk_version.VersionError,
                          ddk_version.describe_component, self.component)


if __name__ == '__main__':
    unittest.main()

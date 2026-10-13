#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for build_info.py.

A build stamp is only worth having if it identifies something, so the tests
are about that: the right repositories and no others, a dirty tree that says
so, and a timestamp that can be pinned when a release has to be reproducible.
"""

from __future__ import print_function

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import build_info  # noqa: E402


def git(repository, *arguments):
    subprocess.check_call(['git', '-C', repository] + list(arguments),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class BuildInfoTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='build-info-test-')
        self.repos = {}
        for name in ('used', 'unused', 'third_party/also_used'):
            self.repo(name)
        os.environ.pop('SOURCE_DATE_EPOCH', None)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        os.environ.pop('SOURCE_DATE_EPOCH', None)

    def repo(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(path)
        git(path, 'init', '-q')
        git(path, 'config', 'user.email', 'test@example.com')
        git(path, 'config', 'user.name', 'Test')
        with open(os.path.join(path, 'a.h'), 'w') as handle:
            handle.write('/* a */\n')
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', 'first')
        self.repos[name] = path
        return path

    def sources(self, *names):
        return [os.path.join(self.repos[n], 'a.h') for n in names]


class RepositoryTest(BuildInfoTestCase):

    def test_only_repositories_that_contributed_are_listed(self):
        found = build_info.repositories_for(
            self.root, self.sources('used', 'third_party/also_used'))
        self.assertEqual(found, ['third_party/also_used', 'used'])

    def test_a_repository_nothing_came_from_is_not_listed(self):
        found = build_info.repositories_for(self.root, self.sources('used'))
        self.assertNotIn('unused', found)

    def test_a_file_deep_inside_a_repository_finds_it(self):
        deep = os.path.join(self.repos['used'], 'a/b/c')
        os.makedirs(deep)
        path = os.path.join(deep, 'd.h')
        with open(path, 'w') as handle:
            handle.write('/* d */\n')
        self.assertEqual(build_info.repositories_for(self.root, [path]),
                         ['used'])

    def test_a_file_outside_every_repository_contributes_nothing(self):
        loose = os.path.join(self.root, 'loose.h')
        with open(loose, 'w') as handle:
            handle.write('/* loose */\n')
        self.assertEqual(build_info.repositories_for(self.root, [loose]), [])


class DescribeTest(BuildInfoTestCase):

    def test_the_commit_is_recorded(self):
        described = build_info.describe(self.root, ['used'])
        self.assertEqual(len(described['used']['commit']), 40)
        self.assertFalse(described['used']['dirty'])

    def test_a_dirty_tree_says_so(self):
        with open(os.path.join(self.repos['used'], 'a.h'), 'a') as handle:
            handle.write('/* changed */\n')
        described = build_info.describe(self.root, ['used'])
        self.assertTrue(described['used']['dirty'])

    def test_a_dirty_repository_is_named_at_the_top_level(self):
        with open(os.path.join(self.repos['used'], 'b.h'), 'w') as handle:
            handle.write('/* new */\n')
        info = build_info.build_info('c', self.root, ['used', 'unused'])
        self.assertEqual(info['dirty'], ['used'])


class TimestampTest(BuildInfoTestCase):

    def test_the_clock_is_used_by_default(self):
        info = build_info.build_info('c', self.root, ['used'], now=1000000000)
        self.assertEqual(info['built_at'], '2001-09-09T01:46:40Z')
        self.assertEqual(info['built_at_source'], 'clock')

    def test_source_date_epoch_pins_it(self):
        os.environ['SOURCE_DATE_EPOCH'] = '1000000000'
        info = build_info.build_info('c', self.root, ['used'], now=2000000000)
        self.assertEqual(info['built_at'], '2001-09-09T01:46:40Z')
        self.assertEqual(info['built_at_source'], 'SOURCE_DATE_EPOCH')

    def test_source_date_comes_from_the_newest_commit(self):
        info = build_info.build_info('c', self.root, ['used'], now=0)
        described = build_info.describe(self.root, ['used'])
        self.assertEqual(info['source_date'],
                         build_info._utc(described['used']['committed_at']))

    def test_source_date_does_not_move_between_two_builds(self):
        first = build_info.build_info('c', self.root, ['used'], now=1)
        second = build_info.build_info('c', self.root, ['used'], now=2)
        self.assertEqual(first['source_date'], second['source_date'])
        self.assertNotEqual(first['built_at'], second['built_at'])


class WriteTest(BuildInfoTestCase):

    def test_the_file_is_written_and_parses(self):
        out = os.path.join(self.root, 'component')
        os.makedirs(out)
        build_info.write(out, 'libddk', self.root, ['used'], now=1)
        with open(os.path.join(out, build_info.BUILD_INFO_FILE)) as handle:
            written = json.load(handle)
        self.assertEqual(written['component'], 'libddk')
        self.assertIn('used', written['revisions'])


class ToolchainStampTest(unittest.TestCase):
    """The compiler is downloaded, so no revision records which one it was."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='toolchain-stamp-')
        self.downloads = os.path.join(self.root, 'zircon', 'prebuilt',
                                      'downloads')
        os.makedirs(os.path.join(self.downloads, 'clang', 'lib', 'clang',
                                 '7.0.0'))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_stamp(self, value):
        with open(os.path.join(self.downloads, 'clang.stamp'), 'w') as handle:
            handle.write(value)

    def test_reads_the_stamp_and_the_release(self):
        self.write_stamp('9388aa0d04b123ab73cb4e0824f8dab437ee35ac\n')
        found = build_info.toolchain_stamp(self.root)
        self.assertEqual('9388aa0d04b123ab73cb4e0824f8dab437ee35ac',
                         found['clang_stamp'])
        self.assertEqual('7.0.0', found['clang_release'])

    def test_no_toolchain_at_all_records_nothing(self):
        self.assertIsNone(build_info.toolchain_stamp(
            os.path.join(self.root, 'elsewhere')))

    def test_two_releases_record_no_release(self):
        # Which one a build used is no longer pinned, and writing either into
        # the record would be a guess that reads like a fact.
        self.write_stamp('abc123')
        os.makedirs(os.path.join(self.downloads, 'clang', 'lib', 'clang',
                                 '8.0.0'))
        found = build_info.toolchain_stamp(self.root)
        self.assertEqual('abc123', found['clang_stamp'])
        self.assertIsNone(found['clang_release'])

    def test_the_stamp_reaches_build_info(self):
        self.write_stamp('abc123')
        info = build_info.build_info('libddk', self.root, [], now=1757000000)
        self.assertEqual('abc123', info['toolchain']['clang_stamp'])


if __name__ == '__main__':
    unittest.main()

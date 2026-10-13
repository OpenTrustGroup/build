#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for publish_delivery.py.

A handover is only worth as much as its note: the archive is not in git, so
the id, the revisions and the checksum in that file are the only thing
holding three artefacts together. The tests are about the ways that stops
being true.
"""

from __future__ import print_function

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import publish_delivery  # noqa: E402


def nest(delivery, layout):
    """Moves each part from its own name to where the layout puts it, the
    wrapper directory being the part's old name."""
    for part, where in layout.items():
        flat = os.path.join(delivery, part)
        if not os.path.isdir(flat):
            continue
        aside = os.path.join(delivery, '.' + part + '.aside')
        os.rename(flat, aside)
        target = os.path.join(delivery, *where.split('/'))
        os.makedirs(os.path.dirname(target))
        os.rename(aside, target)


class PublishTestCase(unittest.TestCase):

    RELEASE = '20260909-abcdef123456'

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='publish-test-')
        self.delivery = os.path.join(self.root, 'output')
        self.out = os.path.join(self.root, 'publish')
        for half in ('libddk', 'grt_ddk', 'mtk_ddk'):
            self.write(half + '/RELEASE', self.RELEASE + '\n')
        self.write('libddk/lib/libddk.a', 'ar\n')
        self.write('grt_ddk/src/a.c', 'int a;\n')
        self.write('grt_ddk/Makefile', 'all:\n')
        self.write('mtk_ddk/src/b.c', 'int b;\n')
        self.write('mtk_ddk/Makefile', 'all:\n')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relative, content):
        path = os.path.join(self.delivery, relative)
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, 'w') as handle:
            handle.write(content)

    def repo(self, name):
        return os.path.join(self.root, 'repo-' + name)

    def publish(self, name='mtk_ddk', release=None):
        return publish_delivery.publish_part(
            os.path.join(self.delivery, name), self.repo(name),
            release or self.RELEASE, 'R <r@grt>')


class ReleaseTest(PublishTestCase):

    def test_the_id_comes_from_the_delivery(self):
        self.assertEqual(self.RELEASE,
                         publish_delivery.read_release(self.delivery))

    def test_parts_that_disagree_are_refused(self):
        # Publishing one of these as a release would name two things one
        # thing, which is the failure the id exists to prevent.
        self.write('mtk_ddk/RELEASE', 'other\n')
        self.assertRaises(publish_delivery.PublishError,
                          publish_delivery.read_release, self.delivery)

    def test_a_delivery_with_no_id_is_refused(self):
        for half in ('libddk', 'grt_ddk', 'mtk_ddk'):
            os.remove(os.path.join(self.delivery, half, 'RELEASE'))
        self.assertRaises(publish_delivery.PublishError,
                          publish_delivery.read_release, self.delivery)


class RepositoryTest(PublishTestCase):

    def test_a_half_becomes_a_commit_and_a_tag(self):
        head = self.publish()
        self.assertEqual(
            head,
            publish_delivery._git(self.repo('mtk_ddk'), 'rev-list', '-1',
                                  'release/' + self.RELEASE).strip())

    def test_the_repository_holds_the_half_and_nothing_else(self):
        self.publish()
        found = set()
        for current, dirs, names in os.walk(self.repo('mtk_ddk')):
            dirs[:] = [d for d in dirs if d != '.git']
            for name in names:
                found.add(os.path.relpath(os.path.join(current, name),
                                          self.repo('mtk_ddk')))
        self.assertEqual({'RELEASE', 'Makefile',
                          os.path.join('src', 'b.c')}, found)

    def test_a_file_the_release_dropped_leaves_the_repository(self):
        # Equal, not merged: a repository that is nearly the delivery is a
        # repository nobody checked.
        self.publish()
        os.remove(os.path.join(self.delivery, 'mtk_ddk/src/b.c'))
        self.write('mtk_ddk/src/c.c', 'int c;\n')
        # A new release: the same id over different content is refused, and
        # there is a test below for that.
        self.publish(release='20260910-000000000000')
        self.assertFalse(os.path.exists(
            os.path.join(self.repo('mtk_ddk'), 'src/b.c')))

    def test_publishing_the_same_release_twice_is_fine(self):
        first = self.publish()
        self.assertEqual(first, self.publish())

    def test_the_same_tag_on_something_else_is_refused(self):
        # A release is made once; publishing it again over something else is
        # how two things end up with one name.
        self.publish()
        self.write('mtk_ddk/src/b.c', 'int b2;\n')
        self.assertRaises(publish_delivery.PublishError, self.publish)


class TagInPlaceTest(PublishTestCase):
    """The half MTK work in ships as a repository and is tagged, not stamped."""

    def half(self):
        """mtk_ddk as it really arrives: a repository with its own history."""
        path = os.path.join(self.delivery, 'mtk_ddk')
        os.remove(os.path.join(path, 'RELEASE'))
        publish_delivery._git(path, 'init', '-q')
        publish_delivery._git(path, 'add', '-A')
        publish_delivery._git(path, '-c', 'user.name=T',
                              '-c', 'user.email=t@x', 'commit', '-q',
                              '-m', '[ALPS1] [CX810-1] ufs: theirs')
        return path

    def test_a_clean_half_is_tagged_without_a_commit(self):
        # A `Release <id>` commit is a message MTK's Gerrit refuses, so the
        # release reaches their history as a tag and nothing else.
        half = self.half()
        before = publish_delivery._git(half, 'rev-parse', 'HEAD').strip()
        head = publish_delivery.tag_in_place(half, self.RELEASE, 'R <r@grt>')
        self.assertEqual(before, head)
        self.assertEqual(
            before,
            publish_delivery._git(half, 'rev-list', '-1',
                                  'release/' + self.RELEASE).strip())
        self.assertEqual(
            1, len(publish_delivery._git(half, 'log', '--oneline')
                   .strip().splitlines()))

    def test_something_left_in_the_tree_is_still_committed(self):
        # The tag has to name the tree that shipped: anything outstanding
        # goes in, or the tag points at something else.
        half = self.half()
        with open(os.path.join(half, 'src/c.c'), 'w') as handle:
            handle.write('int c;\n')
        head = publish_delivery.tag_in_place(half, self.RELEASE, 'R <r@grt>')
        self.assertEqual(
            2, len(publish_delivery._git(half, 'log', '--oneline')
                   .strip().splitlines()))
        self.assertEqual('', publish_delivery._git(half, 'status',
                                                   '--porcelain').strip())
        self.assertEqual(
            head,
            publish_delivery._git(half, 'rev-list', '-1',
                                  'release/' + self.RELEASE).strip())


    def test_a_message_given_is_the_one_committed(self):
        # A component's release commit says which tree and compiler built
        # it; the halves keep the plain one. Both go through here.
        half = self.half()
        with open(os.path.join(half, 'src/c.c'), 'w') as handle:
            handle.write('int c;\n')
        publish_delivery.tag_in_place(half, self.RELEASE, 'R <r@grt>',
                                      'Release X\n\ngarnet deadbeef\n')
        text = publish_delivery._git(half, 'log', '-1', '--format=%B')
        self.assertIn('garnet deadbeef', text)


class ReleaseLogTest(PublishTestCase):
    """What a release commit says for a part that is built output."""

    INFO = ('{"built_at": "2026-09-15T09:16:13Z", "component": "libddk",'
            ' "revisions": {"garnet": {"commit": "698153f1e12cca3e",'
            ' "dirty": false}, "zircon": {"commit": "93c5303767b51aa7",'
            ' "dirty": false}},'
            ' "toolchain": {"clang_release": "7.0.0",'
            ' "clang_stamp": "9388aa0d04b123ab"}}')

    def test_it_names_the_tree_and_the_compiler(self):
        # `Release <id>` alone says when but not from what, and the question
        # asked of a component months later is which tree built it.
        self.write('libddk/BUILD_INFO.json', self.INFO)
        text = publish_delivery.release_log(
            os.path.join(self.delivery, 'libddk'), self.RELEASE)
        self.assertIn('Release ' + self.RELEASE, text)
        self.assertIn('698153f1e12c', text)
        self.assertIn('93c5303767b5', text)
        self.assertIn('7.0.0', text)
        self.assertIn('2026-09-15T09:16:13Z', text)

    def test_a_part_without_one_gets_no_log(self):
        # The source halves have no BUILD_INFO: their history is the commits
        # themselves, so there is nothing for this to add.
        self.assertIsNone(publish_delivery.release_log(
            os.path.join(self.delivery, 'mtk_ddk'), self.RELEASE))

    def test_unreadable_build_info_is_not_fatal(self):
        # A release must not fail over the wording of its own commit.
        self.write('libddk/BUILD_INFO.json', 'not json at all')
        self.assertIsNone(publish_delivery.release_log(
            os.path.join(self.delivery, 'libddk'), self.RELEASE))

class NoteTest(PublishTestCase):

    def test_it_names_everything_needed_to_fetch_the_release(self):
        # Nothing in git ties three repositories together, so the note is
        # where the id and every revision are written down.
        revisions = {'mtk_ddk': self.publish()}
        path = publish_delivery.write_note(self.out, self.RELEASE, revisions,
                                           self.delivery)
        with open(path) as handle:
            text = handle.read()
        self.assertIn(self.RELEASE, text)
        self.assertIn(revisions['mtk_ddk'], text)
        self.assertIn('make -C mtk_ddk', text)


class LayoutTest(PublishTestCase):

    LAYOUT = {'libddk': 'libddk/aries_v1', 'grt_ddk': 'grt_ddk/garnet',
              'mtk_ddk': 'mtk_ddk/aries_v1'}

    def setUp(self):
        PublishTestCase.setUp(self)
        nest(self.delivery, self.LAYOUT)

    def test_the_release_is_read_where_the_layout_puts_the_parts(self):
        self.assertEqual(self.RELEASE, publish_delivery.read_release(
            self.delivery, self.LAYOUT))

    def test_the_wrappers_are_not_parts(self):
        # Without the layout, libddk/ is a part with no RELEASE in it.
        with self.assertRaises(publish_delivery.PublishError):
            publish_delivery.read_release(self.delivery)


if __name__ == '__main__':
    unittest.main()

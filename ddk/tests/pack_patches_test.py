#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for pack_patches.py.

What MTK apply is the difference between the release they have and the one
we send.  That only exists if this release's repositories continue the last
ones -- a mirror rebuilt from scratch has a new root commit and no patch
from it applies to anything anybody holds.  The tests are mostly about
noticing when that has stopped being true, because the failure is silent:
the patches are produced, they just do not apply.
"""

from __future__ import print_function

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import pack_patches  # noqa: E402


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


class PackTestCase(unittest.TestCase):

    RELEASE = '20260915-abcdef123456'

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='pack-test-')
        self.new = os.path.join(self.root, 'output')
        self.old = os.path.join(self.root, 'output.last')
        self.out = os.path.join(self.root, 'patches')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- building deliveries -------------------------------------------------

    def write(self, where, relative, content):
        path = os.path.join(where, relative)
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, 'w') as handle:
            handle.write(content)

    def git(self, repo, *arguments):
        full = ('git', '-c', 'user.name=t', '-c', 'user.email=t@grt') + arguments
        process = subprocess.Popen(full, cwd=repo, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        out, _ = process.communicate()
        if process.returncode != 0:
            raise AssertionError('git %s: %s' % (' '.join(arguments),
                                                 out.decode('utf-8', 'replace')))
        return out.decode('utf-8', 'replace')

    def make_repo(self, where, part, message='base'):
        repo = os.path.join(where, part)
        self.git(repo, 'init', '-q')
        self.git(repo, 'add', '-A')
        self.git(repo, 'commit', '-q', '-m', message)
        return repo

    def continue_repo(self, part):
        """This release's repository, continuing the last one's history."""
        shutil.copytree(os.path.join(self.old, part),
                        os.path.join(self.new, part), symlinks=True)
        return os.path.join(self.new, part)

    def commit(self, repo, message):
        self.git(repo, 'add', '-A')
        self.git(repo, 'commit', '-q', '--allow-empty', '-m', message)

    def delivery(self, halves=('grt_ddk', 'mtk_ddk'), component='libddk'):
        """A previous release and this one, the way a release makes them."""
        for part in halves:
            self.write(self.old, '%s/src/a.c' % part, 'int a;\n')
            self.make_repo(self.old, part)
        self.write(self.old, '%s/RELEASE' % component, self.RELEASE + '\n')
        self.write(self.old, '%s/lib/lib.a' % component, 'ar\n')
        for part in halves:
            self.continue_repo(part)
        shutil.copytree(os.path.join(self.old, component),
                        os.path.join(self.new, component), symlinks=True)

    def pack(self, previous=None):
        return pack_patches.pack(
            self.new, self.old if previous is None else previous,
            self.out, self.RELEASE, 'R <r@grt>')

    def bundled(self):
        """The names inside the tarball, which is what actually ships."""
        bundle = os.path.join(self.out, 'ddk-%s.tar.gz' % self.RELEASE)
        archive = tarfile.open(bundle)
        try:
            return sorted(archive.getnames())
        finally:
            archive.close()


class PatchTest(PackTestCase):

    def test_a_commit_since_the_last_release_becomes_a_patch(self):
        # The whole point: MTK hold the last release and apply these.
        self.delivery()
        self.commit(os.path.join(self.new, 'mtk_ddk'),
                    '[ALPS1] [CX810-1] mtk: a change')
        report = self.pack()
        self.assertEqual(1, len(report['repositories']['mtk_ddk']))
        self.assertTrue(any(name.endswith('.patch') and 'mtk_ddk' in name
                            for name in self.bundled()))

    def test_both_halves_are_patched_not_just_mtk(self):
        # grt_ddk carries history too -- MTK are downstream of us and read
        # our commits in it.
        self.delivery()
        self.commit(os.path.join(self.new, 'grt_ddk'),
                    '[ALPS1] [CX810-1] grt: ours')
        self.commit(os.path.join(self.new, 'mtk_ddk'),
                    '[ALPS2] [CX810-2] mtk: theirs')
        report = self.pack()
        self.assertEqual(1, len(report['repositories']['grt_ddk']))
        self.assertEqual(1, len(report['repositories']['mtk_ddk']))

    def test_a_repository_that_did_not_move_produces_no_patch(self):
        self.delivery()
        report = self.pack()
        self.assertEqual([], report['repositories']['mtk_ddk'])
        self.assertIn('mtk_ddk', report['unchanged'])

    def test_a_rebuilt_mirror_is_refused_rather_than_packed(self):
        # The silent failure this file exists for. A mirror made from
        # scratch has its own root commit, so a patch series from the last
        # release to this one describes a history nobody has. Producing it
        # anyway would be found by MTK, not by us.
        self.delivery()
        shutil.rmtree(os.path.join(self.new, 'mtk_ddk', '.git'))
        self.make_repo(self.new, 'mtk_ddk', 'rebuilt from nothing')
        try:
            self.pack()
        except pack_patches.PackError as error:
            self.assertIn('not an ancestor', str(error))
        else:
            self.fail('a rebuilt mirror was packed as if it continued')

    def test_the_subject_survives_into_the_patch(self):
        # MTK read these as commits, and their Commit-Check reads the title.
        self.delivery()
        self.commit(os.path.join(self.new, 'mtk_ddk'),
                    '[ALPS1] [CX810-1] mtk: keep this subject')
        self.pack()
        found = [name for name in self.bundled() if name.endswith('.patch')]
        self.assertEqual(1, len(found))
        archive = tarfile.open(os.path.join(self.out,
                                            'ddk-%s.tar.gz' % self.RELEASE))
        try:
            text = archive.extractfile(found[0]).read().decode('utf-8')
        finally:
            archive.close()
        self.assertIn('[ALPS1] [CX810-1] mtk: keep this subject', text)


class WholeTest(PackTestCase):

    def test_libddk_travels_whole_with_a_list_of_what_changed(self):
        # A diff over a .a reads as nothing, so the component ships entire
        # and what is said about it is which files moved.
        self.delivery()
        self.write(self.new, 'libddk/lib/lib.a', 'different\n')
        report = self.pack()
        self.assertIn('libddk', report['whole'])
        names = self.bundled()
        self.assertTrue(any(name.endswith('libddk/CHANGES') for name in names))
        self.assertTrue(any('libddk-%s.tar.gz' % self.RELEASE in name
                            for name in names))

    def test_an_unchanged_component_is_not_packed_at_all(self):
        # Nothing to apply, so nothing to send: the delivery says so rather
        # than shipping a copy of what they already have.
        self.delivery()
        report = self.pack()
        self.assertIn('libddk', report['unchanged'])
        self.assertNotIn('libddk', report['whole'])

    def test_the_changed_file_is_named(self):
        self.delivery()
        self.write(self.new, 'libddk/lib/lib.a', 'different\n')
        self.pack()
        archive = tarfile.open(os.path.join(self.out,
                                            'ddk-%s.tar.gz' % self.RELEASE))
        try:
            name = [n for n in archive.getnames()
                    if n.endswith('libddk/CHANGES')][0]
            text = archive.extractfile(name).read().decode('utf-8')
        finally:
            archive.close()
        self.assertIn('lib/lib.a', text)
        self.assertIn('changed: 1', text)

    def test_content_decides_not_timestamps(self):
        # A release rebuilds every file, so every mtime differs. Comparing
        # those would call every release a change and ship the component
        # whole every time.
        self.delivery()
        path = os.path.join(self.new, 'libddk/lib/lib.a')
        os.utime(path, (0, 0))
        report = self.pack()
        self.assertIn('libddk', report['unchanged'])


class ComponentTest(PackTestCase):

    def make_component_a_repo(self):
        """libddk as it now ships: a repository, one commit per release."""
        for side in (self.old, self.new):
            self.make_repo(side, 'libddk', 'release')

    def test_the_component_ships_whole_even_as_a_repository(self):
        # It is built output. A patch over a .a describes nothing a reviewer
        # can read, so being a repository must not turn it into a series.
        self.delivery()
        self.make_component_a_repo()
        self.write(self.new, 'libddk/lib/lib.a', 'different\n')
        report = self.pack()
        self.assertIn('libddk', report['whole'])
        self.assertNotIn('libddk', report['repositories'])
        self.assertTrue(any('libddk-%s.tar.gz' % self.RELEASE in name
                            for name in self.bundled()))

    def test_its_own_history_does_not_ship_inside_the_tarball(self):
        self.delivery()
        self.make_component_a_repo()
        self.write(self.new, 'libddk/lib/lib.a', 'different\n')
        self.pack()
        archive = tarfile.open(os.path.join(self.out,
                                            'ddk-%s.tar.gz' % self.RELEASE))
        try:
            inner = [n for n in archive.getnames()
                     if n.endswith('libddk-%s.tar.gz' % self.RELEASE)][0]
            extracted = os.path.join(self.root, 'libddk.tar.gz')
            with open(extracted, 'wb') as handle:
                handle.write(archive.extractfile(inner).read())
        finally:
            archive.close()
        inner_archive = tarfile.open(extracted)
        try:
            names = inner_archive.getnames()
        finally:
            inner_archive.close()
        self.assertTrue(names)
        self.assertEqual([], [n for n in names if '/.git/' in n + '/'])

    def test_an_unchanged_component_is_still_skipped_as_a_repository(self):
        # Being a repository must not make an identical component look
        # changed: the commit differs every release, the delivery does not.
        self.delivery()
        self.make_component_a_repo()
        report = self.pack()
        self.assertIn('libddk', report['unchanged'])
        self.assertNotIn('libddk', report['whole'])


class FirstReleaseTest(PackTestCase):

    def test_without_a_previous_release_everything_travels_whole(self):
        self.delivery()
        self.commit(os.path.join(self.new, 'mtk_ddk'), 'a change')
        report = self.pack(previous=os.path.join(self.root, 'nothing-here'))
        self.assertEqual({}, report['repositories'])
        self.assertEqual(['grt_ddk', 'libddk', 'mtk_ddk'],
                         sorted(report['whole']))
        self.assertIn('no previous release',
                      '\n'.join(report['notes']))

    def test_a_half_that_is_not_yet_a_repository_travels_whole(self):
        # The release before repositories existed, or a part that failed to
        # mirror: it still has to reach MTK.
        self.delivery()
        shutil.rmtree(os.path.join(self.new, 'grt_ddk', '.git'))
        self.write(self.new, 'grt_ddk/src/a.c', 'int a; /* moved on */\n')
        report = self.pack()
        self.assertIn('grt_ddk', report['whole'])
        self.assertNotIn('grt_ddk', report['repositories'])


class AccountingTest(PackTestCase):

    def test_every_part_is_accounted_for(self):
        # A part nobody packs is a part MTK cannot move forward, and the
        # note is where that would be noticed.
        self.delivery()
        self.commit(os.path.join(self.new, 'mtk_ddk'), 'a change')
        self.write(self.new, 'libddk/lib/lib.a', 'different\n')
        report = self.pack()
        named = set(report['repositories']) | set(report['whole']) \
            | set(report['unchanged'])
        self.assertEqual({'grt_ddk', 'libddk', 'mtk_ddk'}, named)
        text = '\n'.join(report['notes'])
        for part in ('grt_ddk', 'libddk', 'mtk_ddk'):
            self.assertIn(part, text)

    def test_the_git_directory_does_not_ship_inside_a_whole_part(self):
        # It is the mirror's own history, megabytes of it, and the part that
        # ships whole ships as a tree rather than as a repository.
        self.delivery()
        shutil.rmtree(os.path.join(self.new, 'grt_ddk', '.git'))
        self.write(self.new, 'grt_ddk/src/a.c', 'int a; /* moved on */\n')
        self.make_repo(self.new, 'grt_ddk', 'unrelated')
        shutil.rmtree(os.path.join(self.old, 'grt_ddk', '.git'))
        self.pack()
        archive = tarfile.open(os.path.join(self.out,
                                            'ddk-%s.tar.gz' % self.RELEASE))
        try:
            inner = [n for n in archive.getnames()
                     if n.endswith('grt_ddk-%s.tar.gz' % self.RELEASE)][0]
            extracted = os.path.join(self.root, 'inner.tar.gz')
            with open(extracted, 'wb') as handle:
                handle.write(archive.extractfile(inner).read())
        finally:
            archive.close()
        inner_archive = tarfile.open(extracted)
        try:
            names = inner_archive.getnames()
        finally:
            inner_archive.close()
        self.assertTrue(names)
        self.assertEqual([], [n for n in names if '/.git/' in n + '/'])

    def test_a_directory_in_a_checkout_is_not_taken_for_a_repository(self):
        # `git -C somewhere` walks up until it finds a repository, so in a
        # checkout every directory answers yes. Asked of the directory, only
        # a real one does.
        self.delivery()
        plain = os.path.join(self.new, 'grt_ddk', 'src')
        self.assertTrue(pack_patches.is_repository(
            os.path.join(self.new, 'grt_ddk')))
        self.assertFalse(pack_patches.is_repository(plain))


class LayoutTest(PackTestCase):

    LAYOUT = {'libddk': 'libddk/aries_v1', 'grt_ddk': 'grt_ddk/garnet',
              'mtk_ddk': 'mtk_ddk/aries_v1'}

    def nest(self, delivery):
        nest(delivery, self.LAYOUT)

    def test_parts_are_packed_from_where_the_layout_puts_them(self):
        self.delivery()
        self.commit(os.path.join(self.new, 'grt_ddk'), 'one more')
        self.nest(self.new)
        self.nest(self.old)
        report = pack_patches.pack(self.new, self.old, self.out, self.RELEASE,
                                   'R <r@grt>', self.LAYOUT)
        self.assertEqual(['grt_ddk', 'mtk_ddk'],
                         sorted(report['repositories']))
        self.assertEqual(1, len(report['repositories']['grt_ddk']))
        self.assertEqual([], report['repositories']['mtk_ddk'])

    def test_a_whole_part_is_archived_at_its_place(self):
        self.delivery()
        self.nest(self.new)
        pack_patches.pack(self.new, None, self.out, self.RELEASE, 'R <r@grt>',
                          self.LAYOUT)
        # The bundle holds a tarball per part; inside that one, the part is
        # at its place in the layout, so it unpacks where the manifest looks.
        bundle = tarfile.open(os.path.join(self.out,
                                           'ddk-%s.tar.gz' % self.RELEASE))
        try:
            inner = bundle.extractfile('%s/libddk/libddk-%s.tar.gz'
                                       % (self.RELEASE, self.RELEASE))
            part = tarfile.open(fileobj=inner)
            try:
                names = sorted(part.getnames())
            finally:
                part.close()
        finally:
            bundle.close()
        self.assertIn('libddk/aries_v1/RELEASE', names)
        self.assertFalse([n for n in names
                          if not n.startswith('libddk/aries_v1')], names)


if __name__ == '__main__':
    unittest.main()

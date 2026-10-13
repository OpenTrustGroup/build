#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for deploy_delivery.py.

Gerrit is stood in for by bare repositories on disk and a fake that answers
`open_changes` with whatever the test says is up for review.  garnet is a
small repository of its own, so that replayed commits name real sources.
Everything else -- fetching, replaying, rewriting, checking the tree,
pushing -- runs for real against those.
"""

from __future__ import print_function

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'tools'))

import deploy_delivery  # noqa: E402

IDENTITY = 'Release Bot <release@grt>'
RD = 'Wang RD <wangrd@grt>'
CR_MESSAGE = ('[ALPS100] [INT78-1] swa: %s\n\n[Description]\nd\n\n'
              '[Resolution]\nr\n\n[Test]\nt\n\nCR-Id: ALPS100\nFeature: swa\n'
              'Signed-off-by: Wang RD <wangrd@grt>\n')
A, B, C = ('20260901-aaaaaaaaaaaa', '20260902-bbbbbbbbbbbb',
           '20260903-cccccccccccc')
IDS = ('ALPS100', 'INT78-1', 'swa')


def git(repo, *arguments):
    full = ['git', '-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@grt']
    full += list(arguments)
    process = subprocess.Popen(full, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    out, _ = process.communicate()
    if process.returncode != 0:
        raise AssertionError('git %s: %s' % (' '.join(arguments),
                                             out.decode('utf-8', 'replace')))
    return out.decode('utf-8', 'replace')


def write(path, content):
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, 'w') as handle:
        handle.write(content)


class FakeGerrit(object):
    """Projects are bare repositories under a root; open changes are told."""

    def __init__(self, root):
        self.root = root
        self.changes = {}

    def project_url(self, project):
        return os.path.join(self.root, project + '.git')

    def open_changes(self, project, branch, topic):
        return self.changes.get(project, [])

    def create(self, project):
        path = self.project_url(project)
        os.makedirs(path)
        git(path, 'init', '-q', '--bare')
        return path


class DeployTestCase(unittest.TestCase):

    BRANCH = 'release'
    TOPIC = 'ddk'
    PROJECT = 'yocto/src/hypervisor/mtk_ddk/aries_v1'

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='deploy-test-')
        self.gerrit = FakeGerrit(os.path.join(self.root, 'gerrit'))
        self.work = os.path.join(self.root, 'work')
        self.out = os.path.join(self.root, 'out')
        self.remote = self.gerrit.create(self.PROJECT)
        # garnet: the RD's work, the source of everything replayed.
        self.garnet = os.path.join(self.root, 'garnet')
        os.makedirs(self.garnet)
        git(self.garnet, 'init', '-q')
        self.garnet_at = {}
        self.rd_commit('base')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- the RD's work, and a delivered half the way the mirror makes it ----

    def rd_commit(self, subject):
        write(os.path.join(self.garnet, 'drivers/%s.c'
                           % subject.replace(' ', '_')), '/* %s */\n' % subject)
        git(self.garnet, 'add', '-A')
        git(self.garnet, 'commit', '-q', '-m', CR_MESSAGE % subject,
            '--author', RD)
        revision = git(self.garnet, 'rev-parse', 'HEAD').strip()
        self.garnet_at[subject] = revision
        return revision

    def half(self, release, commits=(), name='mtk_ddk'):
        """A half as a delivery holds it: the RD's commits replayed with
        Mirrored-From, the generated-files commit, and the release stamp.
        No tags: nothing here relies on them."""
        path = os.path.join(self.root, 'output', name)
        fresh = not os.path.isdir(path)
        if fresh:
            os.makedirs(path)
            git(path, 'init', '-q')
        for subject in (['base'] if fresh else []) + list(commits):
            revision = self.garnet_at.get(subject) or self.rd_commit(subject)
            write(os.path.join(path, 'src/drivers/%s.c'
                               % subject.replace(' ', '_')),
                  '/* %s */\n' % subject)
            git(path, 'add', '-A')
            git(path, 'commit', '-q', '--author', RD, '-m',
                CR_MESSAGE % subject + 'Mirrored-From: %s\n' % revision)
        write(os.path.join(path, 'src/a.ddk.mk'), 'MODULE := a %s\n' % release)
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', deploy_delivery.GENERATED_SUBJECT
            + '\n\ngenerated\n')
        write(os.path.join(path, 'RELEASE'), release + '\n')
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', 'Release %s' % release)
        self.garnet_at[release] = git(self.garnet, 'rev-parse',
                                      'HEAD').strip()
        return path

    def libddk(self, release):
        """The component: built output, one commit per release, a build
        record naming the garnet revision."""
        path = os.path.join(self.root, 'output', 'libddk')
        if not os.path.isdir(path):
            os.makedirs(path)
            git(path, 'init', '-q')
        write(os.path.join(path, 'lib/libddk.a'), 'ar %s\n' % release)
        write(os.path.join(path, 'BUILD_INFO.json'),
              '{"revisions": {"garnet": {"commit": "%s"}}}\n'
              % git(self.garnet, 'rev-parse', 'HEAD').strip())
        write(os.path.join(path, 'RELEASE'), release + '\n')
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', 'Release %s' % release)
        return path

    def deploy(self, part_dir, part='mtk_ddk', push=True, garnet=None,
               context=None):
        return deploy_delivery.deploy_part(
            part, part_dir, self.PROJECT, self.gerrit, self.BRANCH, self.TOPIC,
            self.work, IDENTITY, push, self.out,
            self.garnet if garnet is None else garnet,
            {'ids': IDS} if context is None else context)

    # -- what is on the remote ---------------------------------------------

    def pushed(self):
        """The ref the push landed on.  A bare repository has no Gerrit in
        front of it, so the magic refspec is stored as the ref's name."""
        refs = [line.split()[-1] for line in git(
            self.remote, 'for-each-ref', 'refs/for').split('\n') if line]
        self.assertEqual(1, len(refs), refs)
        return refs[0]

    def remote_log(self, ref=None):
        return git(self.remote, 'log', '--format=%s',
                   ref or self.pushed()).split('\n')

    def remote_tree(self, ref=None):
        return sorted(git(self.remote, 'ls-tree', '-r', '--name-only',
                          ref or self.pushed()).split())

    def seed_history(self, part_dir):
        """The branch carries the delivery's history: an earlier deployment
        replayed it, so its commits name their sources."""
        git(part_dir, 'push', '-q', self.remote,
            'HEAD:refs/heads/%s' % self.BRANCH)

    def seed_import(self, part_dir, release):
        """The branch carries the delivery imported whole, the way the first
        deployments did: one commit, no sources named."""
        seed = os.path.join(self.root, 'seed')
        os.makedirs(seed)
        git(seed, 'init', '-q')
        deploy_delivery._copy_tree(part_dir, seed)
        git(seed, 'add', '-A')
        git(seed, 'commit', '-q', '-m',
            '[ALPS1] [X-1] ddk: import mtk_ddk %s\n\nimported\n' % release)
        git(seed, 'push', '-q', self.remote, 'HEAD:refs/heads/' + self.BRANCH)
        shutil.rmtree(seed)


    def rewrite_branch(self, paths):
        """The branch moves on from the import: each of `paths` rewritten,
        so no RD commit's change is on it any more."""
        seed = os.path.join(self.root, 'seed')
        git(self.root, 'clone', '-q', '-b', self.BRANCH, self.remote, seed)
        for path in paths:
            write(os.path.join(seed, path), '/* rewritten */\n')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-q', '-m', 'swa: rewritten on the branch')
        git(seed, 'push', '-q', self.remote, 'HEAD:refs/heads/' + self.BRANCH)
        shutil.rmtree(seed)


class ImportTest(DeployTestCase):

    def test_an_empty_project_receives_the_whole_tree(self):
        seed = os.path.join(self.root, 'seed')
        os.makedirs(seed)
        git(seed, 'init', '-q')
        git(seed, 'commit', '-q', '--allow-empty', '-m',
            'Initial empty repository')
        git(seed, 'push', '-q', self.remote, 'HEAD:refs/heads/' + self.BRANCH)
        part = self.half(A, ['one'])
        report = self.deploy(part)
        self.assertEqual('import', report['mode'])
        self.assertIsNone(report['deployed'])
        self.assertEqual(1, len(report['commits']))
        self.assertTrue(report['commits'][0]['subject'].startswith(
            '[ALPS100] [INT78-1] ddk: import mtk_ddk ' + A))
        self.assertIn('RELEASE', self.remote_tree())
        self.assertIn('src/drivers/one.c', self.remote_tree())

    def test_a_base_nothing_can_read_is_replaced_whole(self):
        # Imported whole, no garnet revision known for it, the delivery,
        # rebuilt from scratch, has no stamp of A, and the branch has moved
        # on from what the RD commits wrote: the only thing to do is import
        # again.
        first = self.half(A, ['one'])
        self.seed_import(first, A)
        self.rewrite_branch(['src/drivers/base.c', 'src/drivers/one.c'])
        shutil.rmtree(os.path.join(self.root, 'output', 'mtk_ddk'))
        second = self.half(B, ['one', 'two'])
        report = self.deploy(second, garnet='', context={'ids': IDS})
        self.assertEqual('import', report['mode'])
        self.assertEqual(A, report['deployed'])
        self.assertIn('ddk: import mtk_ddk ' + B, self.remote_log()[0])

    def test_a_branch_carrying_something_else_is_emptied_first(self):
        # The GN-based tree the DDK used to be built from: removed in a
        # change of its own, ahead of the import.
        seed = os.path.join(self.root, 'seed')
        write(os.path.join(seed, 'drivers/old.c'), 'int old;\n')
        write(os.path.join(seed, 'build_all.sh'), 'make\n')
        git(seed, 'init', '-q')
        git(seed, 'add', '-A')
        git(seed, 'commit', '-q', '-m', 'headers: sync zircon headers')
        git(seed, 'push', '-q', self.remote, 'HEAD:refs/heads/' + self.BRANCH)
        part = self.half(A, ['one'])
        report = self.deploy(part, garnet='')
        self.assertEqual('import', report['mode'])
        self.assertEqual(['[ALPS100] [INT78-1] ddk: remove the previous mtk_ddk tree',
                          '[ALPS100] [INT78-1] ddk: import mtk_ddk ' + A],
                         [c['subject'] for c in report['commits']])
        landed = self.pushed()
        self.assertEqual('', git(self.remote, 'ls-tree', landed + '~1'))
        self.assertEqual(['[ALPS100] [INT78-1] ddk: import mtk_ddk ' + A,
                          '[ALPS100] [INT78-1] ddk: remove the previous mtk_ddk tree',
                          'headers: sync zircon headers'], self.remote_log()[:3])

    def test_the_import_change_id_is_stable(self):
        first = self.half(A, ['one'])
        self.seed_import(first, A)
        second = self.half(B, ['two'])
        self.deploy(second, garnet='')
        body = git(self.remote, 'log', '-1', '--format=%B', self.pushed())
        # A bare repository is not Gerrit: the ref has to go before the
        # same refspec can land again.
        git(self.remote, 'update-ref', '-d', self.pushed())
        self.deploy(second, garnet='')
        again = git(self.remote, 'log', '-1', '--format=%B', self.pushed())
        self.assertEqual(body.split('Change-Id: ')[1],
                         again.split('Change-Id: ')[1])


class SeriesTest(DeployTestCase):

    def test_the_rd_commits_the_base_holds_are_read_from_its_history(self):
        first = self.half(A, ['one'])
        self.seed_history(first)
        second = self.half(B, ['two', 'three'])
        report = self.deploy(second)
        self.assertEqual('series', report['mode'])
        self.assertEqual(A, report['deployed'])
        # Newest first: the release, three, two, then what was there.
        self.assertEqual([
            '[ALPS100] [INT78-1] ddk: release ' + B,
            '[ALPS100] [INT78-1] swa: three',
            '[ALPS100] [INT78-1] swa: two',
            'Release ' + A,
        ], self.remote_log()[:4])
        authors = git(self.remote, 'log', '-3', '--format=%an <%ae>',
                      self.pushed()).split('\n')
        self.assertEqual([IDENTITY, RD, RD], authors[:3])
        self.assertEqual(['wangrd@grt'], report['reviewers'])
        self.assertIn('r=wangrd@grt', report['refspec'])

    def test_a_base_imported_whole_holds_what_its_garnet_revision_reaches(
            self):
        # No trailer on the branch, but libddk's build record said which
        # garnet revision the import was built from, and the garnet checkout
        # can tell which RD commits came after it.
        first = self.half(A, ['one'])
        self.seed_import(first, A)
        second = self.half(B, ['two'])
        report = self.deploy(second, context={
            'ids': IDS, 'garnet_base': self.garnet_at[A], 'deployed': A})
        self.assertEqual('series', report['mode'])
        self.assertEqual(['[ALPS100] [INT78-1] ddk: release ' + B,
                          '[ALPS100] [INT78-1] swa: two',
                          '[ALPS1] [X-1] ddk: import mtk_ddk ' + A],
                         self.remote_log()[:3])

    def test_a_commit_pushed_again_under_another_title_is_held(self):
        # The base was built from 'one'; the delivery was built from the
        # same change pushed again under another title -- another commit,
        # the same patch.  Only 'two' is new.
        first = self.half(A, ['one'])
        self.seed_import(first, A)
        garnet_base = self.garnet_at[A]
        git(self.garnet, 'checkout', '-q', '-b', 'retitled',
            self.garnet_at['base'])
        git(self.garnet, 'cherry-pick', '-n', self.garnet_at['one'])
        git(self.garnet, 'commit', '-q', '--author', RD, '-m',
            CR_MESSAGE % 'one, retitled')
        self.garnet_at['one'] = git(self.garnet, 'rev-parse', 'HEAD').strip()
        shutil.rmtree(os.path.join(self.root, 'output', 'mtk_ddk'))
        second = self.half(B, ['one', 'two'])
        report = self.deploy(second, context={
            'ids': IDS, 'garnet_base': garnet_base, 'deployed': A})
        self.assertEqual('series', report['mode'])
        self.assertEqual(['[ALPS100] [INT78-1] ddk: release ' + B,
                          '[ALPS100] [INT78-1] swa: two',
                          '[ALPS1] [X-1] ddk: import mtk_ddk ' + A],
                         self.remote_log()[:3])

    def test_a_garnet_base_the_checkout_lacks_is_named(self):
        first = self.half(A, ['one'])
        self.seed_import(first, A)
        # Rebuilt from scratch: the delivery no longer has A's stamp.
        shutil.rmtree(os.path.join(self.root, 'output', 'mtk_ddk'))
        second = self.half(B, ['one', 'two'])
        with self.assertRaises(deploy_delivery.DeployError) as caught:
            self.deploy(second, context={
                'ids': IDS, 'garnet_base': '0' * 40, 'deployed': A})
        self.assertIn('does not have', str(caught.exception))

    def test_a_garnet_revision_of_another_release_is_not_trusted(self):
        # libddk on the branch is at a different release from the half, so
        # its garnet revision says nothing about the half, and the delivery,
        # rebuilt from scratch, has no stamp of A either.  The base's tree
        # still carries 'one', so only 'two' is new.
        first = self.half(A, ['one'])
        self.seed_import(first, A)
        shutil.rmtree(os.path.join(self.root, 'output', 'mtk_ddk'))
        second = self.half(B, ['one', 'two'])
        report = self.deploy(second, context={
            'ids': IDS, 'garnet_base': self.garnet_at[A],
            'deployed': 'other'})
        self.assertEqual('series', report['mode'])
        self.assertEqual(['[ALPS100] [INT78-1] ddk: release ' + B,
                          '[ALPS100] [INT78-1] swa: two',
                          '[ALPS1] [X-1] ddk: import mtk_ddk ' + A],
                         self.remote_log()[:3])

    def test_a_base_imported_whole_holds_what_its_release_stamp_had(self):
        # libddk on the branch is at another release, so its garnet
        # revision is no help, but the delivery still has the stamp of the
        # release the base holds: only what came after it is new.
        first = self.half(A, ['one'])
        self.seed_import(first, A)
        second = self.half(B, ['two'])
        report = self.deploy(second, context={
            'ids': IDS, 'garnet_base': self.garnet_at[A],
            'deployed': 'other'})
        self.assertEqual('series', report['mode'])
        self.assertEqual(['[ALPS100] [INT78-1] ddk: release ' + B,
                          '[ALPS100] [INT78-1] swa: two',
                          '[ALPS1] [X-1] ddk: import mtk_ddk ' + A],
                         self.remote_log()[:3])

    def test_a_rebuilt_delivery_repository_reads_the_same(self):
        # The delivery's repository is made from scratch -- out/ddk wiped --
        # and replays the same sources with the same trailers.
        first = self.half(A, ['one'])
        self.seed_history(first)
        shutil.rmtree(os.path.join(self.root, 'output'))
        second = self.half(B, ['one', 'two'])
        report = self.deploy(second)
        self.assertEqual('series', report['mode'])
        self.assertEqual(['[ALPS100] [INT78-1] ddk: release ' + B,
                          '[ALPS100] [INT78-1] swa: two'],
                         self.remote_log()[:2])

    def test_the_generated_files_go_up_once_after_the_rd_commits(self):
        first = self.half(A, ['one'])
        self.seed_history(first)
        second = self.half(B, ['two'])
        report = self.deploy(second)
        subjects = [c['subject'] for c in report['commits']]
        self.assertEqual(2, len(subjects))
        self.assertTrue(subjects[-1].endswith('ddk: release ' + B))
        self.assertNotIn(deploy_delivery.GENERATED_SUBJECT,
                         self.remote_log()[:2])
        body = git(self.remote, 'log', '-1', '--format=%B', self.pushed())
        self.assertIn('CR-Id: ALPS100', body)
        self.assertIn('Signed-off-by: ' + IDENTITY, body)

    def test_every_change_carries_one_change_id(self):
        first = self.half(A, ['one'])
        self.seed_history(first)
        second = self.half(B, ['two'])
        self.deploy(second)
        bodies = git(self.remote, 'log', '-2', '--format=%B%x00',
                     self.pushed()).split('\x00')
        for body in bodies[:2]:
            self.assertEqual(1, body.count('Change-Id: I'), body)

    def test_the_tree_pushed_is_the_delivery(self):
        first = self.half(A, ['one'])
        self.seed_history(first)
        second = self.half(B, ['two'])
        self.deploy(second)
        expected = sorted(name for name in git(second, 'ls-files').split()
                          if name)
        self.assertEqual(expected, self.remote_tree())

    def test_an_open_change_is_the_base_not_the_branch(self):
        first = self.half(A, ['one'])
        self.seed_history(first)
        second = self.half(B, ['two'])
        self.deploy(second)
        landed = self.pushed()
        tip = git(self.remote, 'rev-parse', landed).strip()
        parent = git(self.remote, 'rev-parse', landed + '~1').strip()
        git(self.remote, 'update-ref', 'refs/changes/01/1/1', tip)
        git(self.remote, 'update-ref', '-d', landed)
        self.gerrit.changes[self.PROJECT] = [
            {'number': 1, 'subject': 'x', 'url': '', 'revision': tip,
             'ref': 'refs/changes/01/1/1', 'parents': [parent]}]
        third = self.half(C, ['four'])
        report = self.deploy(third)
        self.assertEqual('series', report['mode'])
        self.assertEqual(B, report['deployed'])
        self.assertTrue(report['base_label'].startswith('change 1'))
        self.assertEqual('[ALPS100] [INT78-1] swa: four', self.remote_log()[1])

    def test_a_refused_push_is_reported_not_retried(self):
        first = self.half(A, ['one'])
        self.seed_history(first)
        hook = os.path.join(self.remote, 'hooks', 'pre-receive')
        write(hook, '#!/bin/sh\necho "missing Commit-Check" >&2\nexit 1\n')
        os.chmod(hook, 0o755)
        second = self.half(B, ['two'])
        report = self.deploy(second)
        self.assertFalse(report['pushed'])
        self.assertIn('missing Commit-Check', report['push_error'])
        self.assertEqual([], report['urls'])
        self.assertEqual(['wangrd@grt'], report['reviewers'])
        self.assertEqual('', git(self.remote, 'for-each-ref', 'refs/for'))
        # The patches are there for whoever takes it up.
        self.assertEqual(2, len(report['patches']))

    def test_a_rebuild_of_the_same_tree_is_up_to_date(self):
        # Same sources, built again: a new stamp, a new build date, the
        # checksums that follow -- and nothing for anybody to review.
        first = self.half(A, ['one'])
        write(os.path.join(first, 'libddk/BUILD_INFO.json'),
              '{"built_at": "1"}\n')
        write(os.path.join(first, 'libddk/SDK_CONTENTS.json'), '{"a": 1}\n')
        git(first, 'add', '-A')
        git(first, 'commit', '-q', '-m', 'Release ' + A)
        self.seed_history(first)
        write(os.path.join(first, 'libddk/BUILD_INFO.json'),
              '{"built_at": "2"}\n')
        write(os.path.join(first, 'libddk/SDK_CONTENTS.json'), '{"a": 2}\n')
        write(os.path.join(first, 'RELEASE'), B + '\n')
        git(first, 'add', '-A')
        git(first, 'commit', '-q', '-m', 'Release ' + B)
        report = self.deploy(first)
        self.assertEqual('up to date', report['mode'])
        self.assertEqual(A, report['deployed'])
        self.assertEqual(B, report['release'])
        self.assertEqual('', git(self.remote, 'for-each-ref', 'refs/for'))

    def test_up_to_date_pushes_nothing(self):
        first = self.half(A, ['one'])
        self.seed_history(first)
        report = self.deploy(first)
        self.assertEqual('up to date', report['mode'])
        self.assertEqual([], report['commits'])

    def test_a_dry_run_writes_patches_and_pushes_nothing(self):
        first = self.half(A, ['one'])
        self.seed_history(first)
        second = self.half(B, ['two'])
        report = self.deploy(second, push=False)
        self.assertEqual(2, len(report['patches']))
        self.assertEqual([], report['urls'])
        self.assertEqual('', git(self.remote, 'for-each-ref', 'refs/for'))

    def test_two_open_chains_are_refused(self):
        with self.assertRaises(deploy_delivery.DeployError):
            deploy_delivery.chain_tip([
                {'number': 1, 'revision': 'a', 'parents': ['z'],
                 'subject': 'a', 'ref': '', 'url': ''},
                {'number': 2, 'revision': 'b', 'parents': ['z'],
                 'subject': 'b', 'ref': '', 'url': ''}])


class NoRdWorkTest(DeployTestCase):

    def test_a_half_nothing_touched_goes_up_as_the_release_alone(self):
        # The mirror began after the last RD commit to this half: its
        # history names no source, and the release is the whole change.
        first = self.half(A)
        self.seed_history(first)
        path = os.path.join(self.root, 'output', 'mtk_ddk')
        # Strip the trailers, as a history with no RD work looks.
        git(path, 'filter-branch', '-f', '--msg-filter',
            'grep -v Mirrored-From', 'HEAD')
        second = self.half(B)
        git(second, 'filter-branch', '-f', '--msg-filter',
            'grep -v Mirrored-From', 'HEAD')
        report = self.deploy(second)
        self.assertEqual('series', report['mode'])
        self.assertEqual(['[ALPS100] [INT78-1] ddk: release ' + B],
                         [c['subject'] for c in report['commits']])


class ComponentTest(DeployTestCase):

    PROJECT = 'yocto/src/hypervisor/libddk/aries_v1'

    def test_libddk_goes_up_as_one_release_under_the_halves_ids(self):
        first = self.libddk(A)
        git(first, 'push', '-q', self.remote, 'HEAD:refs/heads/' + self.BRANCH)
        second = self.libddk(B)
        context = {'ids': IDS}
        report = self.deploy(second, part='libddk', context=context)
        self.assertEqual('series', report['mode'])
        self.assertEqual(['[ALPS100] [INT78-1] ddk: release ' + B,
                          'Release ' + A], self.remote_log()[:2])
        # And it leaves the garnet revision of its base for the halves.
        self.assertEqual(self.garnet_at['base'], context['garnet_base'])
        self.assertEqual(A, context['deployed'])

    def test_libddk_up_to_date_still_leaves_its_garnet_revision(self):
        # A release that changes nothing in libddk is the common case; the
        # halves imported whole still need its base's garnet revision, or
        # they cannot replay their RD commits and are imported again.
        first = self.libddk(A)
        git(first, 'push', '-q', self.remote, 'HEAD:refs/heads/' + self.BRANCH)
        context = {'ids': IDS}
        report = self.deploy(first, part='libddk', context=context)
        self.assertEqual('up to date', report['mode'])
        self.assertEqual(self.garnet_at['base'], context['garnet_base'])
        self.assertEqual(A, context['deployed'])


class MessageTest(unittest.TestCase):

    def test_the_review_format_is_read(self):
        parsed = deploy_delivery.parse_message(CR_MESSAGE % 'thing')
        self.assertEqual(('ALPS100', 'INT78-1', 'swa', 'swa'),
                         (parsed['cr'], parsed['ones'], parsed['module'],
                          parsed['feature']))
        self.assertFalse(parsed['own'])

    def test_the_pipelines_own_commits_are_recognised(self):
        self.assertTrue(deploy_delivery.parse_message(
            deploy_delivery.GENERATED_SUBJECT + '\n')['own'])
        self.assertTrue(deploy_delivery.parse_message(
            'Release 20260901-aaaaaaaaaaaa\n')['own'])

    def test_own_commits_take_the_ids_of_the_newest_rd_commit(self):
        series = [CR_MESSAGE % 'one',
                  '[ALPS200] [INT78-2] ufs: two\n\nCR-Id: ALPS200\n'
                  'Feature: ufs\n',
                  deploy_delivery.GENERATED_SUBJECT + '\n']
        self.assertEqual(('ALPS200', 'INT78-2', 'ufs'),
                         deploy_delivery.ids_for_own_commits(series, []))
        self.assertEqual(IDS, deploy_delivery.ids_for_own_commits(
            [], [CR_MESSAGE % 'old']))
        self.assertIsNone(deploy_delivery.ids_for_own_commits([], []))

    def test_the_source_a_replayed_commit_names(self):
        self.assertEqual('a' * 40, deploy_delivery.mirrored_from(
            'subject\n\nMirrored-From: %s\n' % ('a' * 40)))
        self.assertIsNone(deploy_delivery.mirrored_from('subject\n'))

    def test_the_release_a_message_names_is_the_new_one(self):
        named = deploy_delivery.release_named_by
        self.assertEqual(B, named('swa: update libddk\n\nThe tree here is '
                                  'release %s. Release %s replaces it.\n'
                                  % (A, B)))
        self.assertEqual(C, named('[A] [B] ddk: import mtk_ddk %s\n\nover '
                                  '%s\n' % (C, A)))
        self.assertIsNone(named('nothing here\n'))

    def test_a_change_id_replaces_any_that_was_there(self):
        text = deploy_delivery.with_change_id(
            'subject\n\nbody\nChange-Id: Iold\n', 'Inew')
        self.assertEqual('subject\n\nbody\nChange-Id: Inew\n', text)

    def test_the_ssh_user_is_the_local_part_of_the_git_identity(self):
        self.assertEqual('niyc', deploy_delivery.user_from_git_config(
            'niyc@goldenrivertek.com'))
        self.assertIsNone(deploy_delivery.user_from_git_config('nobody'))
        self.assertIsNone(deploy_delivery.user_from_git_config(''))

    def test_a_gerrit_url_is_read(self):
        gerrit = deploy_delivery.Gerrit('ssh://host:29420', 'me')
        self.assertEqual('ssh://me@host:29420/a/b', gerrit.project_url('a/b'))
        gerrit = deploy_delivery.Gerrit('ssh://you@host')
        self.assertEqual('ssh://you@host:29418/p', gerrit.project_url('p'))


if __name__ == '__main__':
    unittest.main()

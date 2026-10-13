#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for sync_mirror.py and import_from_mirror.py.

The mirror is the interchange format: our commits go out through it and MTK's
come back through it. What the tests are about is the two ways that stops
being true -- the mirror drifting from what was delivered, and a message or a
path meaning something different on the other side.
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

import import_from_mirror  # noqa: E402
import partition  # noqa: E402
import sync_mirror  # noqa: E402

RENAME = {'from': 'garnet', 'to': 'src', 'reason': 'because'}


class MessageTest(unittest.TestCase):
    """What MTK reads, and what comes back."""

    def test_change_id_is_dropped(self):
        # Two commits sharing one is a collision Gerrit resolves by guessing,
        # and a change that goes out and comes back would produce exactly
        # that.
        text = 'subject\n\nbody\n\nChange-Id: I0123456789abcdef\n'
        self.assertNotIn('Change-Id',
                         sync_mirror.outbound_message(text, RENAME))

    def test_a_path_is_rewritten(self):
        # A message naming garnet/... points at a directory MTK's tree does
        # not have: it misleads rather than discloses.
        out = sync_mirror.outbound_message('see garnet/drivers/i2c/x.c\n',
                                           RENAME)
        self.assertIn('src/drivers/i2c/x.c', out)

    def test_a_module_tag_is_not_a_path(self):
        # `garnet:` is how our own log reads, and MTK reads the same log.
        text = 'garnet: prune MT8678 dependencies\n'
        self.assertEqual(text, sync_mirror.outbound_message(text, RENAME))

    def test_nothing_else_is_touched(self):
        text = ('[ALPS11390861] [CX810-61] ddk: subject\n\n'
                '[Description]\nwhy\n\nSigned-off-by: a <a@b>\n')
        self.assertEqual(text, sync_mirror.outbound_message(text, RENAME))

    def test_the_rewrite_comes_back(self):
        original = 'see garnet/drivers/i2c/x.c and garnet/lib/y.h\n'
        out = sync_mirror.outbound_message(original, RENAME)
        self.assertEqual(original,
                         import_from_mirror.inbound_message(out, RENAME))


class MirrorTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='mirror-test-')
        self.repo = os.path.join(self.root, 'garnet')
        self.mirror = os.path.join(self.root, 'mirror')
        self.delivery = os.path.join(self.root, 'delivery')
        self.rules = {
            'format_version': partition.FORMAT_VERSION,
            'parts': {'grt_ddk': 'ours', 'mtk_ddk': 'theirs'},
            'default_part': 'grt_ddk',
            'ignore': [],
            'rules': [{'prefix': 'drivers/mtk', 'part': 'mtk_ddk',
                       'reason': 'theirs'}],
            'rename': dict(RENAME),
        }

        os.makedirs(os.path.join(self.repo, 'drivers/mtk'))
        self.git('init', '-q')
        self.git('config', 'user.name', 'us')
        self.git('config', 'user.email', 'us@grt')
        self.write('drivers/mtk/d.c', '#include "garnet/lib/a.h"\nint d;\n')
        self.write('drivers/mtk/BUILD.gn', 'source_set("d") {}\n')
        self.commit('first\n\nChange-Id: I0123456789abcdef\n')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *arguments):
        return sync_mirror._git(self.repo, *arguments)

    def write(self, relative, content):
        path = os.path.join(self.repo, relative)
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, 'w') as handle:
            handle.write(content)

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD').strip()

    # What a build makes, which is not everything the repository holds: a
    # BUILD.gn is in the tree and in no delivery.  Both halves are named
    # here, and one is staged only when the test wrote it, so a test that
    # adds a file gets it delivered without saying so twice.
    DELIVERED = ('drivers/mtk/d.c', 'lib/a.h')

    def make_delivery(self, extra=None, part='mtk_ddk'):
        """What a release ships: the rendered half, plus what a build makes."""
        shutil.rmtree(self.delivery, ignore_errors=True)
        staging = os.path.join(self.root, 'staging')
        shutil.rmtree(staging, ignore_errors=True)
        tree = os.path.join(staging, 'garnet')
        os.makedirs(tree)
        for relative in self.DELIVERED:
            source = os.path.join(self.repo, relative)
            if not os.path.isfile(source):
                continue
            target = os.path.join(tree, relative)
            if not os.path.isdir(os.path.dirname(target)):
                os.makedirs(os.path.dirname(target))
            shutil.copyfile(source, target)
        parts = os.path.join(self.root, 'parts')
        shutil.rmtree(parts, ignore_errors=True)
        partition.split(staging, self.rules, parts, 'garnet', True)
        shutil.copytree(os.path.join(parts, part), self.delivery)
        if part == 'mtk_ddk':
            with open(os.path.join(self.delivery,
                                   'src/drivers/mtk/d.ddk.mk'), 'w') as handle:
                handle.write('MODULE_SRCS := %s\n'
                             % (extra or 'src/drivers/mtk/d.c'))
        return self.delivery

    def sync(self, part='mtk_ddk', **kwargs):
        kwargs.setdefault('until', 'HEAD')
        return sync_mirror.sync(self.repo, self.rules, part,
                                self.make_delivery(part=part), self.mirror,
                                identity='R <r@grt>', scope='garnet',
                                **kwargs)


class SyncTest(MirrorTestCase):

    def test_the_mirror_becomes_the_delivery(self):
        # Everything downstream is only as good as this being true.
        self.sync()
        self.assertTrue(os.path.isfile(
            os.path.join(self.mirror, 'src/drivers/mtk/d.c')))
        self.assertTrue(os.path.isfile(
            os.path.join(self.mirror, 'src/drivers/mtk/d.ddk.mk')))

    def test_a_file_the_delivery_does_not_have_is_not_mirrored(self):
        # A commit carries every file under the prefixes it touches -- the
        # BUILD.gn here, seventeen files in the real half -- and the delivery
        # carries fewer. Mirroring the prefix would hand MTK files their
        # delivery does not have and patches that do not apply to it.
        self.sync()
        self.assertFalse(os.path.exists(
            os.path.join(self.mirror, 'src/drivers/mtk/BUILD.gn')))

    def test_our_commits_arrive_as_theirs_to_apply(self):
        self.sync()
        self.write('drivers/mtk/d.c', '#include "garnet/lib/a.h"\nint d2;\n')
        self.commit('[ALPS1] mtk: change d\n\nSee garnet/drivers/mtk/d.c\n')
        result = self.sync()
        self.assertEqual(1, len(result['replayed']))
        log = sync_mirror._git(self.mirror, 'log', '--format=%B')
        self.assertIn('src/drivers/mtk/d.c', log)
        self.assertNotIn('garnet/drivers/mtk/d.c', log)

    def test_a_replayed_commit_names_where_it_came_from(self):
        self.sync()
        self.write('drivers/mtk/d.c', 'int d4;\n')
        self.commit('[ALPS1] mtk: d4\n\nChange-Id: I0123456789abcdef\n')
        source = self.git('rev-parse', 'HEAD').strip()
        self.sync()
        body = sync_mirror._git(self.mirror, 'log', '-1', '--format=%B')
        self.assertIn('Mirrored-From: %s' % source, body)
        self.assertNotIn('Change-Id', body)

    def test_who_wrote_it_survives(self):
        self.sync()
        self.write('drivers/mtk/d.c', 'int d3;\n')
        self.git('add', '-A')
        self.git('commit', '-q', '--author', 'W <w@grt>', '-m', 'theirs')
        self.sync()
        self.assertIn('w@grt', sync_mirror._git(self.mirror, 'log',
                                                '--format=%ae'))

    def test_generated_files_arrive_in_their_own_commit(self):
        # They come from BUILD.gn through the build, so they belong to no
        # commit above.
        self.sync()
        subjects = sync_mirror._git(self.mirror, 'log', '--format=%s')
        self.assertIn('Release: what the build produces', subjects)

    def test_a_commit_that_changes_nothing_delivered_is_skipped(self):
        self.sync()
        self.write('drivers/mtk/BUILD.gn', 'source_set("d") { x = 1 }\n')
        self.commit('touches only an undelivered file')
        result = self.sync()
        self.assertEqual([], result['replayed'])

    def test_a_mirror_from_other_rules_is_refused(self):
        # Its patches are laid out the old way and ours would not sit on top.
        self.sync()
        # Reuse the delivery this mirror was built from: what changed is
        # the ownership, not what shipped.
        self.rules['rules'] = [{'prefix': 'drivers/other', 'part': 'mtk_ddk',
                                'reason': 'moved'}]
        self.assertRaises(
            sync_mirror.MirrorError, sync_mirror.sync, self.repo, self.rules,
            'mtk_ddk', self.delivery, self.mirror, 'HEAD')

    def test_the_default_part_names_no_prefix_to_extract_by(self):
        # It is everything the rules do not assign elsewhere, so there is no
        # set of paths to limit `git archive` to: the whole scope comes out
        # and the split decides what the part owns.
        self.assertEqual([], sync_mirror.part_prefixes(self.rules, 'grt_ddk'))
        self.assertEqual(['drivers/mtk'],
                         sync_mirror.part_prefixes(self.rules, 'mtk_ddk'))

    def test_a_part_owning_no_prefix_at_all_is_still_refused(self):
        self.assertRaises(sync_mirror.MirrorError,
                          sync_mirror.part_prefixes, self.rules, 'nobody')

    def test_a_prefix_the_commit_does_not_have_yet_is_not_asked_for(self):
        # A part owns the prefixes it owns now, and a commit old enough need
        # not have them all.  `git archive` fails the whole command on a
        # pathspec that matches nothing, so without narrowing them to what
        # the commit has, every commit older than the newest prefix takes
        # the mirror down with it.
        self.rules['rules'].append({'prefix': 'drivers/later',
                                    'part': 'mtk_ddk', 'reason': 'theirs'})
        self.write('drivers/later/e.c', 'int e;\n')
        self.commit('[ALPS1] [CX810-1] later: arrives after the first\n')
        report = self.sync()
        self.assertTrue(report['replayed'])

    def test_a_commit_holding_none_of_the_prefixes_renders_nothing(self):
        # `rev-list -- <prefix>` hands back the commit that removed the last
        # of them as well.  There the narrowing leaves nothing, and `git
        # archive <rev> --` with no pathspec is a request for the whole tree:
        # the half would arrive holding the other one.
        first = sync_mirror._git(self.repo, 'rev-list', '--max-parents=0',
                                 'HEAD').strip()
        self.rules['rules'] = [{'prefix': 'drivers/absent',
                                'part': 'mtk_ddk', 'reason': 'gone'}]
        out = os.path.join(self.root, 'rendered')
        self.assertEqual(0, sync_mirror.render_commit(
            self.repo, first, self.rules, 'mtk_ddk', 'garnet', out))

    def test_the_default_part_is_mirrored_too(self):
        # GRT's half is the default part, and it ships with its history as
        # well: MTK are downstream of us and read our commits in it.  What
        # limits `git archive` for the other half is absent here, so the
        # whole scope is read and the split says what this part owns.
        self.write('lib/a.h', 'int a;\n')
        ours = self.commit('[ALPS1] [CX810-1] lib: ours\n')
        report = self.sync(part='grt_ddk')
        self.assertEqual(1, len(report['replayed']))
        subjects = sync_mirror._git(self.mirror, 'log', '--format=%s')
        self.assertIn('lib: ours', subjects)
        # The other half's commit is not in it.
        self.assertNotIn('first', subjects.splitlines()[-1:] or [''])

    def test_a_commit_only_the_other_half_owns_is_not_replayed(self):
        # Without prefixes `rev-list` would hand back every commit in range,
        # including the ones that only ever touched MTK's directories.
        self.write('drivers/mtk/e.c', 'int e;\n')
        self.commit('[ALPS1] [CX810-1] mtk: theirs\n')
        report = self.sync(part='grt_ddk')
        subjects = sync_mirror._git(self.mirror, 'log', '--format=%s')
        self.assertNotIn('mtk: theirs', subjects)

    def test_a_mirror_that_does_not_match_is_an_error(self):
        self.sync()
        with open(os.path.join(self.delivery, 'src/extra.h'), 'w') as handle:
            handle.write('x\n')
        problems = sync_mirror.verify(
            self.delivery, self.mirror,
            sync_mirror.delivered_files(self.delivery))
        self.assertTrue(problems)


class ImportTest(MirrorTestCase):

    def test_their_commit_comes_back_in_our_layout(self):
        self.sync()
        base = sync_mirror._git(self.mirror, 'rev-parse', 'HEAD').strip()
        path = os.path.join(self.mirror, 'src/drivers/mtk/d.c')
        with open(path, 'w') as handle:
            handle.write('#include "src/lib/a.h"\nint mtk;\n')
        sync_mirror._git(self.mirror, 'add', '-A')
        sync_mirror._git(self.mirror, 'commit', '-q', '--author',
                         'M <m@mediatek.com>', '-m',
                         '[ALPS1] i2c: theirs\n\nsee src/drivers/mtk/d.c\n')

        out = os.path.join(self.root, 'inbound')
        made = import_from_mirror.import_commits(
            self.mirror, self.rules, base, 'HEAD', out, 'I <i@grt>')
        self.assertEqual(1, len(made))
        patches = sorted(os.listdir(out))
        with open(os.path.join(out, patches[0])) as handle:
            text = handle.read()
        self.assertIn('garnet/drivers/mtk/d.c', text)
        self.assertNotIn('src/drivers/mtk/d.c', text)
        self.assertIn('m@mediatek.com', text)
        self.assertIn('int mtk;', text)

    def test_a_module_they_declared_is_called_out(self):
        # The description is generated here from a BUILD.gn target; one that
        # comes back needs a target adding, or the next build overwrites it.
        self.sync()
        base = sync_mirror._git(self.mirror, 'rev-parse', 'HEAD').strip()
        path = os.path.join(self.mirror, 'src/drivers/new.ddk.mk')
        with open(path, 'w') as handle:
            handle.write('MODULE_SRCS := src/drivers/new.c\n')
        sync_mirror._git(self.mirror, 'add', '-A')
        sync_mirror._git(self.mirror, 'commit', '-q', '-m', 'theirs: a module')

        out = os.path.join(self.root, 'inbound')
        import_from_mirror.import_commits(self.mirror, self.rules, base,
                                          'HEAD', out, 'I <i@grt>')
        self.assertEqual(['garnet/drivers/new.ddk.mk'],
                         import_from_mirror.declared_in(out))

    def test_an_ordinary_file_is_not_called_out(self):
        self.sync()
        base = sync_mirror._git(self.mirror, 'rev-parse', 'HEAD').strip()
        with open(os.path.join(self.mirror, 'src/drivers/new.c'),
                  'w') as handle:
            handle.write('int added;\n')
        sync_mirror._git(self.mirror, 'add', '-A')
        sync_mirror._git(self.mirror, 'commit', '-q', '-m', 'theirs: a file')
        out = os.path.join(self.root, 'inbound')
        import_from_mirror.import_commits(self.mirror, self.rules, base,
                                          'HEAD', out, 'I <i@grt>')
        self.assertEqual([], import_from_mirror.declared_in(out))

    def test_a_return_that_changed_nothing_imports_nothing(self):
        self.sync()
        base = sync_mirror._git(self.mirror, 'rev-parse', 'HEAD').strip()
        out = os.path.join(self.root, 'inbound')
        self.assertEqual([], import_from_mirror.import_commits(
            self.mirror, self.rules, base, 'HEAD', out))

    def test_a_tree_with_no_manifest_cannot_be_reversed(self):
        # The manifest is what says how the layout was made; guessing would
        # produce a tree that is neither.
        self.sync()
        bare = os.path.join(self.root, 'bare')
        os.makedirs(bare)
        self.assertRaises(sync_mirror.MirrorError,
                          import_from_mirror.reverse_tree, bare, self.rules,
                          os.path.join(self.root, 'out'))


if __name__ == '__main__':
    unittest.main()

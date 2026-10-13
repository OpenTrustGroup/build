#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for partition.py.

The three properties the delivery rests on -- total, disjoint, reversible --
are each tested directly, and so are the ways a partition stops being those
things: a stale rule, a part merged with a part from different rules, and a
path claimed twice.
"""

from __future__ import print_function

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import partition  # noqa: E402


RULES = {
    'format_version': 1,
    'parts': {
        'grt': 'GRT half.',
        'mtk': 'MTK half.',
    },
    'default_part': 'grt',
    'ignore': ['.git'],
    'rules': [
        {'prefix': 'drivers/spi', 'part': 'mtk', 'reason': 'MTK SPI.'},
    ],
}


def write(path, content, mode=0o644):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, 'w') as handle:
        handle.write(content)
    os.chmod(path, mode)


class PartitionTestCase(unittest.TestCase):

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix='partition-test-')
        self.tree = os.path.join(self.workspace, 'tree')
        write(os.path.join(self.tree, 'lib/a.cc'), 'a\n')
        write(os.path.join(self.tree, 'drivers/spi/s.c'), 's\n')
        write(os.path.join(self.tree, 'drivers/i2c/i.c'), 'i\n')
        write(os.path.join(self.tree, 'tool.sh'), '#!/bin/sh\n', mode=0o755)
        os.makedirs(os.path.join(self.tree, 'empty/deeper'))
        os.symlink('a.cc', os.path.join(self.tree, 'lib/link.cc'))
        self.rules = json.loads(json.dumps(RULES))

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def rules_file(self, rules=None):
        path = os.path.join(self.workspace, 'rules.json')
        with open(path, 'w') as handle:
            json.dump(rules if rules is not None else self.rules, handle)
        return path


class TotalityTest(PartitionTestCase):
    """Every entry lands in exactly one part."""

    def test_every_entry_is_assigned_once(self):
        entries = partition.scan_tree(self.tree, self.rules['ignore'])
        assignment, _, _ = partition.partition(entries, self.rules)
        assigned = [r for part in assignment.values() for r in part]
        self.assertEqual(sorted(assigned), sorted(entries))
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_empty_directory_is_carried(self):
        entries = partition.scan_tree(self.tree, self.rules['ignore'])
        self.assertIn('empty/deeper', entries)
        self.assertEqual(entries['empty/deeper']['kind'], 'dir')

    def test_symlink_is_carried_as_a_link(self):
        entries = partition.scan_tree(self.tree, self.rules['ignore'])
        self.assertEqual(entries['lib/link.cc'],
                         {'kind': 'link', 'target': 'a.cc'})

    def test_ignored_paths_are_not_entries(self):
        write(os.path.join(self.tree, '.git/config'), 'x\n')
        entries = partition.scan_tree(self.tree, self.rules['ignore'])
        self.assertFalse([e for e in entries if e.startswith('.git')])


class OwnershipTest(PartitionTestCase):

    def test_longest_prefix_wins_regardless_of_order(self):
        self.rules['rules'] = [
            {'prefix': 'drivers', 'part': 'grt', 'reason': 'x'},
            {'prefix': 'drivers/spi', 'part': 'mtk', 'reason': 'y'},
        ]
        forward = partition.owner_of('drivers/spi/s.c', self.rules)
        self.rules['rules'].reverse()
        backward = partition.owner_of('drivers/spi/s.c', self.rules)
        self.assertEqual(forward, backward)
        self.assertEqual(forward[0], 'mtk')

    def test_prefix_matches_on_path_segments_only(self):
        self.rules['rules'] = [
            {'prefix': 'drivers/spi', 'part': 'mtk', 'reason': 'x'},
        ]
        part, _ = partition.owner_of('drivers/spidev/s.c', self.rules)
        self.assertEqual(part, 'grt')

    def test_unmatched_path_falls_to_the_default(self):
        part, prefix = partition.owner_of('lib/a.cc', self.rules)
        self.assertEqual(part, 'grt')
        self.assertIsNone(prefix)


class StaleRuleTest(PartitionTestCase):
    """A rule that matches nothing is how a partition drifts unnoticed."""

    def test_stale_rule_is_rejected(self):
        self.rules['rules'].append(
            {'prefix': 'drivers/gone', 'part': 'mtk', 'reason': 'moved away'})
        with self.assertRaises(partition.PartitionError) as caught:
            partition.check(self.tree, self.rules)
        self.assertIn('drivers/gone', str(caught.exception))

    def test_live_rules_are_accepted(self):
        _, assignment, matched = partition.check(self.tree, self.rules)
        self.assertEqual(matched['drivers/spi'], 1)
        self.assertEqual(assignment['mtk'], ['drivers/spi/s.c'])


class RoundTripTest(PartitionTestCase):
    """The invariant the whole delivery rests on."""

    def round_trip(self):
        parts = os.path.join(self.workspace, 'parts')
        merged = os.path.join(self.workspace, 'merged')
        written = partition.split(self.tree, self.rules, parts)
        partition.merge([os.path.join(parts, p) for p in sorted(written)],
                        merged)
        return merged, partition.verify(self.tree, merged,
                                        self.rules['ignore'])

    def test_merge_of_a_split_is_the_original(self):
        _, problems = self.round_trip()
        self.assertEqual(problems, [])

    def test_file_mode_survives(self):
        merged, _ = self.round_trip()
        mode = stat.S_IMODE(os.lstat(os.path.join(merged, 'tool.sh')).st_mode)
        self.assertEqual(mode, 0o755)

    def test_a_change_on_one_side_is_the_whole_diff(self):
        """What the split is for: tell exactly what the other side changed."""
        parts = os.path.join(self.workspace, 'parts')
        merged = os.path.join(self.workspace, 'merged')
        partition.split(self.tree, self.rules, parts)
        write(os.path.join(parts, 'mtk/drivers/spi/s.c'), 's changed\n')
        partition.merge([os.path.join(parts, 'grt'),
                         os.path.join(parts, 'mtk')], merged)
        problems = partition.verify(self.tree, merged, self.rules['ignore'])
        self.assertEqual(problems, ['drivers/spi/s.c: content differs'])

    def test_a_file_added_on_one_side_appears_in_the_merge(self):
        parts = os.path.join(self.workspace, 'parts')
        merged = os.path.join(self.workspace, 'merged')
        partition.split(self.tree, self.rules, parts)
        added = 'drivers/spi/new.c'
        write(os.path.join(parts, 'mtk', added), 'new\n')
        manifest_path = os.path.join(parts, 'mtk', partition.MANIFEST_NAME)
        with open(manifest_path) as handle:
            manifest = json.load(handle)
        manifest['entries'][added] = {'kind': 'file', 'mode': 0o644}
        with open(manifest_path, 'w') as handle:
            json.dump(manifest, handle)
        partition.merge([os.path.join(parts, 'grt'),
                         os.path.join(parts, 'mtk')], merged)
        self.assertTrue(os.path.isfile(os.path.join(merged, added)))
        self.assertEqual(partition.verify(self.tree, merged,
                                          self.rules['ignore']),
                         ['appeared in merge: %s' % added])


class MergeRefusalTest(PartitionTestCase):

    def test_parts_from_different_rules_are_refused(self):
        parts = os.path.join(self.workspace, 'parts')
        partition.split(self.tree, self.rules, parts)
        path = os.path.join(parts, 'mtk', partition.MANIFEST_NAME)
        with open(path) as handle:
            manifest = json.load(handle)
        manifest['rules_digest'] = 'a different decision'
        with open(path, 'w') as handle:
            json.dump(manifest, handle)
        with self.assertRaises(partition.PartitionError) as caught:
            partition.merge([os.path.join(parts, 'grt'),
                             os.path.join(parts, 'mtk')],
                            os.path.join(self.workspace, 'merged'))
        self.assertIn('different ownership rules', str(caught.exception))

    def test_a_path_claimed_twice_is_refused(self):
        parts = os.path.join(self.workspace, 'parts')
        partition.split(self.tree, self.rules, parts)
        claimed = 'drivers/spi/s.c'
        write(os.path.join(parts, 'grt', claimed), 's\n')
        path = os.path.join(parts, 'grt', partition.MANIFEST_NAME)
        with open(path) as handle:
            manifest = json.load(handle)
        manifest['entries'][claimed] = {'kind': 'file', 'mode': 0o644}
        with open(path, 'w') as handle:
            json.dump(manifest, handle)
        with self.assertRaises(partition.PartitionError) as caught:
            partition.merge([os.path.join(parts, 'grt'),
                             os.path.join(parts, 'mtk')],
                            os.path.join(self.workspace, 'merged'))
        self.assertIn('claimed by both', str(caught.exception))

    def test_a_directory_that_is_not_a_part_is_refused(self):
        with self.assertRaises(partition.PartitionError) as caught:
            partition.merge([self.tree],
                            os.path.join(self.workspace, 'merged'))
        self.assertIn('not a partition part', str(caught.exception))


class RulesValidationTest(PartitionTestCase):

    def reject(self, mutate, expected):
        rules = json.loads(json.dumps(RULES))
        mutate(rules)
        with self.assertRaises(partition.PartitionError) as caught:
            partition.read_rules(self.rules_file(rules))
        self.assertIn(expected, str(caught.exception))

    def test_default_part_must_be_declared(self):
        def mutate(rules):
            rules['default_part'] = 'nobody'
        self.reject(mutate, 'default_part')

    def test_rule_must_name_a_declared_part(self):
        def mutate(rules):
            rules['rules'][0]['part'] = 'nobody'
        self.reject(mutate, 'undeclared part')

    def test_rule_needs_a_reason(self):
        def mutate(rules):
            del rules['rules'][0]['reason']
        self.reject(mutate, 'needs a reason')

    def test_duplicate_rule_is_refused(self):
        def mutate(rules):
            rules['rules'].append(dict(rules['rules'][0]))
        self.reject(mutate, 'duplicate rule')

    def test_escaping_prefix_is_refused(self):
        def mutate(rules):
            rules['rules'][0]['prefix'] = '../elsewhere'
        self.reject(mutate, 'invalid rule prefix')

    def test_valid_rules_are_accepted(self):
        loaded = partition.read_rules(self.rules_file())
        self.assertEqual(loaded['default_part'], 'grt')

    def test_the_digest_ignores_reasons_but_not_ownership(self):
        base = partition.rules_digest(json.loads(json.dumps(RULES)))
        reworded = json.loads(json.dumps(RULES))
        reworded['rules'][0]['reason'] = 'reworded'
        self.assertEqual(partition.rules_digest(reworded), base)
        moved = json.loads(json.dumps(RULES))
        moved['rules'][0]['part'] = 'grt'
        self.assertNotEqual(partition.rules_digest(moved), base)


class RenameTest(unittest.TestCase):
    """A delivered half laid out under a different top-level name.

    `garnet` on the top line of a directory listing says which upstream this
    derives from and where every file sat in it, before anybody opens a file.
    The merge has to put it back exactly, or a half MTK returns cannot be
    diffed against ours.
    """

    RULES = {
        'format_version': partition.FORMAT_VERSION,
        'parts': {'a': 'ours', 'b': 'theirs'},
        'default_part': 'a',
        'ignore': [],
        'rules': [{'prefix': 'drivers/mtk', 'part': 'b', 'reason': 'theirs'}],
        'rename': {'from': 'garnet', 'to': 'src', 'reason': 'because'},
    }

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='rename-test-')
        self.tree = os.path.join(self.root, 'tree')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relative, content):
        path = os.path.join(self.tree, relative)
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'w') as handle:
            handle.write(content)

    def roundtrip(self, rules=None):
        rules = rules or self.RULES
        parts = os.path.join(self.root, 'parts')
        merged = os.path.join(self.root, 'merged')
        partition.split(self.tree, rules, parts, 'garnet', True)
        partition.merge([os.path.join(parts, p) for p in sorted(rules['parts'])],
                        merged, rules)
        return parts, merged

    def read(self, root, relative):
        with open(os.path.join(root, relative)) as handle:
            return handle.read()

    # -- the rewriters -----------------------------------------------------

    def test_an_include_is_rewritten(self):
        self.assertEqual('#include "src/lib/a.h"\n',
                         partition.rewrite('#include "garnet/lib/a.h"\n',
                                           'source', 'garnet', 'src'))

    def test_prose_is_not_rewritten(self):
        # "clear its src/dst registers" is a comment in this tree, and a
        # rewrite that touched it could not be undone without corrupting it.
        text = '/* disable DMA + clear src/dst */\n'
        self.assertEqual(text, partition.rewrite(text, 'source', 'src',
                                                 'garnet'))

    def test_a_make_path_field_is_rewritten(self):
        self.assertEqual('MODULE_SRCS := src/a.c src/b.c',
                         partition.rewrite('MODULE_SRCS := garnet/a.c '
                                           'garnet/b.c', 'make', 'garnet',
                                           'src'))

    def test_an_unrelated_make_field_is_not(self):
        text = 'MODULE_CFLAGS := -DX=garnet/a.c'
        self.assertEqual(text, partition.rewrite(text, 'make', 'garnet',
                                                 'src'))

    def test_a_bare_name_is_not_a_path(self):
        # The rules file itself says "to": "src"; a rewriter that swapped
        # every string equal to the name would undo itself into "garnet".
        self.assertEqual('"to": "src"',
                         partition.rewrite('"to": "src"', 'json', 'src',
                                           'garnet'))

    # -- invertibility -----------------------------------------------------

    def test_a_file_that_cannot_be_undone_is_refused(self):
        # #include <src/lauxlib.h> was real in this tree: no leading-component
        # rule can tell it from a path that was renamed.
        self.write('garnet/a.c', '#include <src/lauxlib.h>\n')
        self.assertRaises(partition.PartitionError, self.roundtrip)

    def test_the_check_is_on_the_bytes(self):
        self.assertIsNone(partition.rename_bytes(
            b'#include <src/x.h>\n', 'source', 'garnet', 'src'))
        self.assertIsNotNone(partition.rename_bytes(
            b'#include <garnet/x.h>\n', 'source', 'garnet', 'src'))

    # -- the delivery ------------------------------------------------------

    def test_the_half_carries_the_new_name_only(self):
        self.write('garnet/drivers/mtk/d.c', '#include "garnet/lib/a.h"\n')
        self.write('garnet/drivers/mtk/d.ddk.mk',
                   'MODULE_SRCS := garnet/drivers/mtk/d.c\n')
        self.write('garnet/lib/a.h', '#pragma once\n')
        parts, _ = self.roundtrip()
        half = os.path.join(parts, 'b')
        self.assertTrue(os.path.isdir(os.path.join(half, 'src/drivers/mtk')))
        self.assertFalse(os.path.exists(os.path.join(half, 'garnet')))
        self.assertIn('#include "src/lib/a.h"',
                      self.read(half, 'src/drivers/mtk/d.c'))
        self.assertIn('MODULE_SRCS := src/drivers/mtk/d.c',
                      self.read(half, 'src/drivers/mtk/d.ddk.mk'))

    def test_the_manifest_does_not_name_the_old_directory(self):
        # It travels inside the part, so writing "from": "garnet" in it would
        # put the name back on the one line the rename exists to clear.
        self.write('garnet/drivers/mtk/d.c', 'int x;\n')
        parts, _ = self.roundtrip()
        with open(os.path.join(parts, 'b', partition.MANIFEST_NAME)) as h:
            text = h.read()
        self.assertNotIn('garnet', text)
        self.assertIn('"renamed": true', text)

    def test_the_merge_puts_everything_back(self):
        self.write('garnet/drivers/mtk/d.c', '#include "garnet/lib/a.h"\n')
        self.write('garnet/lib/a.h', '#pragma once\n')
        self.write('garnet/lib/a.ddk.mk',
                   'MODULE_INCLUDE_DIRS := garnet/lib\n')
        _, merged = self.roundtrip()
        self.assertEqual([], partition.verify(self.tree, merged, []))

    def test_merging_without_the_rules_is_refused(self):
        # The part says a rename happened and not what it was.
        self.write('garnet/drivers/mtk/d.c', 'int x;\n')
        parts = os.path.join(self.root, 'parts')
        partition.split(self.tree, self.RULES, parts, 'garnet', True)
        self.assertRaises(
            partition.PartitionError, partition.merge,
            [os.path.join(parts, 'a'), os.path.join(parts, 'b')],
            os.path.join(self.root, 'out'))

    def test_a_kept_path_is_left_alone(self):
        # libddk publishes these; renaming the spelling would point the
        # driver at a directory the delivery does not have.
        rules = dict(self.RULES)
        rules['rename'] = dict(self.RULES['rename'],
                               keep=['garnet/bin/guest/proto'])
        self.write('garnet/drivers/mtk/d.c',
                   '#include "garnet/bin/guest/proto/vm.pb.h"\n'
                   '#include "garnet/lib/a.h"\n')
        parts, merged = self.roundtrip(rules)
        delivered = self.read(os.path.join(parts, 'b'), 'src/drivers/mtk/d.c')
        self.assertIn('"garnet/bin/guest/proto/vm.pb.h"', delivered)
        self.assertIn('"src/lib/a.h"', delivered)
        self.assertEqual([], partition.verify(self.tree, merged, []))

    def test_render_lays_a_whole_tree_out_without_splitting_it(self):
        # The whole-tree self-test and the trim build this form, because
        # libddk publishes its headers under the delivered name.
        self.write('garnet/lib/a.h', '#pragma once\n')
        self.write('garnet/drivers/mtk/d.c', '#include "garnet/lib/a.h"\n')
        out = os.path.join(self.root, 'rendered')
        total = partition.render(self.tree, out, self.RULES['rename'])
        self.assertEqual(2, total)
        self.assertTrue(os.path.isfile(os.path.join(out, 'src/lib/a.h')))
        self.assertFalse(os.path.exists(os.path.join(out, 'garnet')))
        self.assertIn('#include "src/lib/a.h"',
                      self.read(out, 'src/drivers/mtk/d.c'))

    def test_rendering_twice_is_refused(self):
        # It is not idempotent and must not look like it: undoing the second
        # pass would turn the first pass's paths back into `garnet`.
        self.write('garnet/drivers/mtk/d.c', '#include "garnet/lib/a.h"\n')
        once = os.path.join(self.root, 'once')
        partition.render(self.tree, once, self.RULES['rename'])
        self.assertRaises(partition.PartitionError, partition.render,
                          once, os.path.join(self.root, 'twice'),
                          self.RULES['rename'])

    def test_a_file_the_sender_added_comes_back(self):
        """The whole point of the split is that MTK change their half.

        Merging the manifest instead of the tree dropped a file they added
        without a word -- a whole driver, reported as "merged 645 entries"
        and no complaint. The manifest says what we sent; a half comes back
        with what they did to it.
        """
        self.write('garnet/drivers/mtk/d.c', 'int d;\n')
        parts = os.path.join(self.root, 'parts')
        partition.split(self.tree, self.RULES, parts, 'garnet', True)
        with open(os.path.join(parts, 'b', 'src/drivers/mtk/new.c'),
                  'w') as handle:
            handle.write('int added;\n')
        merged = os.path.join(self.root, 'merged')
        result = partition.merge(
            [os.path.join(parts, p) for p in ('a', 'b')], merged, self.RULES)
        self.assertEqual(1, len(result['added']))
        self.assertTrue(os.path.isfile(
            os.path.join(merged, 'garnet/drivers/mtk/new.c')))

    def test_the_build_a_half_carries_is_not_the_senders_work(self):
        """A half ships with the Makefile that builds it.

        It is installed after the split, so the part manifest does not list
        it; without this it would be read as a file MTK added and merged
        back into garnet, which generates it in the first place.
        """
        self.write('garnet/drivers/mtk/d.c', 'int d;\n')
        parts = os.path.join(self.root, 'parts')
        partition.split(self.tree, self.RULES, parts, 'garnet', True)
        for part in ('a', 'b'):
            with open(os.path.join(parts, part, 'Makefile'), 'w') as handle:
                handle.write('all:\n')
        merged = os.path.join(self.root, 'merged')
        result = partition.merge(
            [os.path.join(parts, p) for p in ('a', 'b')], merged, self.RULES)
        self.assertEqual(0, len(result['added']))
        self.assertFalse(os.path.exists(os.path.join(merged, 'Makefile')))

    def test_a_file_the_sender_removed_is_reported(self):
        self.write('garnet/drivers/mtk/d.c', 'int d;\n')
        self.write('garnet/drivers/mtk/e.c', 'int e;\n')
        parts = os.path.join(self.root, 'parts')
        partition.split(self.tree, self.RULES, parts, 'garnet', True)
        os.remove(os.path.join(parts, 'b', 'src/drivers/mtk/e.c'))
        merged = os.path.join(self.root, 'merged')
        result = partition.merge(
            [os.path.join(parts, p) for p in ('a', 'b')], merged, self.RULES)
        self.assertEqual(1, result['removed'])
        self.assertFalse(os.path.exists(
            os.path.join(merged, 'garnet/drivers/mtk/e.c')))

    def test_the_halfs_own_bookkeeping_does_not_merge(self):
        # .partition.json and RELEASE describe the half; they are not in the
        # tree it came from and must not appear in the tree it goes back to.
        self.write('garnet/drivers/mtk/d.c', 'int d;\n')
        parts = os.path.join(self.root, 'parts')
        partition.split(self.tree, self.RULES, parts, 'garnet', True)
        with open(os.path.join(parts, 'b', 'RELEASE'), 'w') as handle:
            handle.write('r1\n')
        merged = os.path.join(self.root, 'merged')
        result = partition.merge(
            [os.path.join(parts, p) for p in ('a', 'b')], merged, self.RULES)
        self.assertEqual(0, len(result['added']))
        self.assertFalse(os.path.exists(os.path.join(merged, 'RELEASE')))
        self.assertFalse(os.path.exists(
            os.path.join(merged, partition.MANIFEST_NAME)))

    def test_the_generated_licences_do_not_merge(self):
        # LICENSES/ and NOTICE are generated for each half out of a
        # declaration that lives in garnet. Merging them back would add a
        # generated tree to the source it was generated from, and the next
        # release would report it as something the other side wrote.
        self.write('garnet/drivers/mtk/d.c', 'int d;\n')
        parts = os.path.join(self.root, 'parts')
        partition.split(self.tree, self.RULES, parts, 'garnet', True)
        half = os.path.join(parts, 'b')
        os.makedirs(os.path.join(half, 'LICENSES'))
        with open(os.path.join(half, 'LICENSES', 'FILES'), 'w') as handle:
            handle.write('FILE  COMPONENT  LICENCE\n')
        with open(os.path.join(half, 'NOTICE'), 'w') as handle:
            handle.write('NOTICE\n')
        merged = os.path.join(self.root, 'merged')
        result = partition.merge(
            [os.path.join(parts, p) for p in ('a', 'b')], merged, self.RULES)
        self.assertEqual(0, len(result['added']))
        self.assertFalse(os.path.exists(os.path.join(merged, 'NOTICE')))
        self.assertFalse(os.path.isdir(os.path.join(merged, 'LICENSES')))

    def test_a_notice_in_the_tree_is_not_the_generated_one(self):
        """garnet has a NOTICE of its own at its root.

        Skipping the name unconditionally drops it from the merge -- and
        does it quietly, in a round trip whose whole point is that it
        reproduces the tree exactly. The generated notice is written by the
        pass that writes LICENSES/FILES, so that file is what tells them
        apart.
        """
        self.write('NOTICE', 'the tree has one of these\n')
        self.write('garnet/drivers/mtk/d.c', 'int d;\n')
        parts = os.path.join(self.root, 'parts')
        partition.split(self.tree, self.RULES, parts, 'garnet', True)
        # It lands at a part's root, which is where a generated one would be.
        self.assertTrue(os.path.isfile(os.path.join(parts, 'a', 'NOTICE')))
        merged = os.path.join(self.root, 'merged')
        partition.merge([os.path.join(parts, p) for p in ('a', 'b')],
                        merged, self.RULES)
        self.assertTrue(os.path.isfile(os.path.join(merged, 'NOTICE')))

    def test_a_delivered_half_carries_a_git_directory(self):
        # mtk_ddk ships as a repository, so a half coming back has one.
        self.write('garnet/drivers/mtk/d.c', 'int d;\n')
        parts = os.path.join(self.root, 'parts')
        partition.split(self.tree, self.RULES, parts, 'garnet', True)
        os.makedirs(os.path.join(parts, 'b', '.git', 'refs'))
        with open(os.path.join(parts, 'b', '.git', 'HEAD'), 'w') as handle:
            handle.write('ref: refs/heads/main\n')
        merged = os.path.join(self.root, 'merged')
        result = partition.merge(
            [os.path.join(parts, p) for p in ('a', 'b')], merged, self.RULES)
        self.assertEqual(0, len(result['added']))
        self.assertFalse(os.path.exists(os.path.join(merged, '.git')))

    def test_a_module_the_sender_declared_is_called_out(self):
        """A description is derived here, from a product target we ship.

        One that comes back is a module they added and our build does not
        make: committing the file leaves it to be overwritten by the next
        release, and the module then leaves the delivery with nothing said.
        """
        self.write('garnet/drivers/mtk/d.c', 'int d;\n')
        parts = os.path.join(self.root, 'parts')
        partition.split(self.tree, self.RULES, parts, 'garnet', True)
        with open(os.path.join(parts, 'b', 'src/drivers/mtk/new.ddk.mk'),
                  'w') as handle:
            handle.write('MODULE_SRCS := src/drivers/mtk/new.c\n')
        result = partition.merge(
            [os.path.join(parts, p) for p in ('a', 'b')],
            os.path.join(self.root, 'merged'), self.RULES)
        self.assertEqual(['garnet/drivers/mtk/new.ddk.mk'],
                         result['declared'])
        said = '\n'.join(partition.describe_declarations(result['declared']))
        self.assertIn('policy/delivery.json', said)
        self.assertIn('new.ddk.mk', said)

    def test_an_ordinary_file_is_not_called_out(self):
        self.write('garnet/drivers/mtk/d.c', 'int d;\n')
        parts = os.path.join(self.root, 'parts')
        partition.split(self.tree, self.RULES, parts, 'garnet', True)
        with open(os.path.join(parts, 'b', 'src/drivers/mtk/new.c'),
                  'w') as handle:
            handle.write('int added;\n')
        result = partition.merge(
            [os.path.join(parts, p) for p in ('a', 'b')],
            os.path.join(self.root, 'merged'), self.RULES)
        self.assertEqual([], result['declared'])
        self.assertEqual([], partition.describe_declarations([]))

    def test_the_digest_covers_the_rename(self):
        # Parts split with and without it describe different layouts and must
        # not merge together.
        without = dict(self.RULES)
        without.pop('rename')
        self.assertNotEqual(partition.rules_digest(self.RULES),
                            partition.rules_digest(without))


if __name__ == '__main__':
    unittest.main()

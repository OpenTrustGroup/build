#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for combine_builds.py.

The delivery's second tier rests on three claims, so each is tested, and so
is each way of breaking it: a half that did not build what it declared, two
halves producing the same artifact, and a dependency crossing the halves,
which would make combining a link rather than a union.
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

import combine_builds  # noqa: E402


def write(path, content, mode=0o644):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, 'w') as handle:
        handle.write(content)
    os.chmod(path, mode)


class CombineTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='combine-test-')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def half(self, part, modules, built=None):
        """A built half: module descriptions in src/, artifacts in build/."""
        sandbox = os.path.join(self.root, 'build-' + part)
        for module, kind in modules.items():
            write(os.path.join(sandbox, 'src', 'garnet', module,
                               module + combine_builds.MODULE_SUFFIX),
                  'MODULE_NAME := %s\nMODULE_TYPE := %s\nMODULE_DEPS :=\n'
                  % (module, kind))
        for module, kind in (modules if built is None else built).items():
            pattern = combine_builds.ARTIFACTS[kind][0]
            write(os.path.join(sandbox, 'build', pattern % module),
                  'artifact of %s\n' % module,
                  mode=0o755 if kind in ('driver', 'executable') else 0o644)
        return sandbox


class ExportTest(CombineTestCase):

    def test_a_half_exports_its_artifacts_and_no_source(self):
        sandbox = self.half('grt', {'machina': 'shared_library',
                                    'guest': 'executable',
                                    'block': 'driver'})
        out = os.path.join(self.root, 'bundle')
        entries = combine_builds.export(sandbox, 'grt', out)
        self.assertEqual(sorted(entries),
                         ['bin/guest', 'drivers/block.so',
                          'lib/libmachina.so'])
        for current, _, files in os.walk(out):
            for name in files:
                self.assertFalse(
                    name.endswith(('.c', '.cc', '.h', '.ddk.mk')),
                    'source reached the bundle: %s' % name)

    def test_the_bundle_records_where_each_artifact_came_from(self):
        sandbox = self.half('grt', {'block': 'driver'})
        out = os.path.join(self.root, 'bundle')
        combine_builds.export(sandbox, 'grt', out)
        with open(os.path.join(out, combine_builds.MANIFEST_NAME)) as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest['part'], 'grt')
        self.assertEqual(manifest['entries']['drivers/block.so']['module'],
                         'block')

    def test_mode_survives(self):
        sandbox = self.half('grt', {'guest': 'executable'})
        out = os.path.join(self.root, 'bundle')
        combine_builds.export(sandbox, 'grt', out)
        mode = stat.S_IMODE(os.lstat(os.path.join(out, 'bin/guest')).st_mode)
        self.assertEqual(mode, 0o755)

    def test_a_half_that_did_not_build_what_it_declared_is_refused(self):
        # Declared three, built two.  Exporting the two silently would ship a
        # bundle that is short a driver and says nothing about it.
        sandbox = self.half('grt',
                            {'block': 'driver', 'gpt': 'driver',
                             'ramdisk': 'driver'},
                            built={'block': 'driver', 'gpt': 'driver'})
        with self.assertRaises(combine_builds.CombineError) as caught:
            combine_builds.export(sandbox, 'grt',
                                  os.path.join(self.root, 'bundle'))
        self.assertIn('ramdisk', str(caught.exception))

    def test_an_unbuilt_half_is_refused(self):
        sandbox = os.path.join(self.root, 'never-built')
        os.makedirs(os.path.join(sandbox, 'src'))
        self.assertRaises(combine_builds.CombineError, combine_builds.export,
                          sandbox, 'grt', os.path.join(self.root, 'bundle'))


class CombineTest(CombineTestCase):

    def bundles(self):
        grt = self.half('grt', {'machina': 'shared_library',
                                'block': 'driver'})
        mtk = self.half('mtk', {'mt8678_ufs': 'driver'})
        out_grt = os.path.join(self.root, 'bundles/grt')
        out_mtk = os.path.join(self.root, 'bundles/mtk')
        combine_builds.export(grt, 'grt', out_grt)
        combine_builds.export(mtk, 'mtk', out_mtk)
        return out_grt, out_mtk

    def test_the_halves_combine_into_one_tree(self):
        out_grt, out_mtk = self.bundles()
        delivery = os.path.join(self.root, 'delivery')
        combined = combine_builds.combine([out_grt, out_mtk], delivery)
        self.assertEqual(sorted(combined),
                         ['drivers/block.so', 'drivers/mt8678_ufs.so',
                          'lib/libmachina.so'])
        for destination in combined:
            self.assertTrue(os.path.isfile(
                os.path.join(delivery, destination)))

    def test_every_artifact_says_which_half_built_it(self):
        out_grt, out_mtk = self.bundles()
        delivery = os.path.join(self.root, 'delivery')
        combined = combine_builds.combine([out_grt, out_mtk], delivery)
        self.assertEqual(combined['drivers/mt8678_ufs.so']['part'], 'mtk')
        self.assertEqual(combined['drivers/block.so']['part'], 'grt')

    def test_an_artifact_built_by_both_halves_is_refused(self):
        # One half quietly overwriting the other's driver is the failure the
        # whole split exists to make impossible.
        out_grt, _ = self.bundles()
        clash = self.half('mtk', {'block': 'driver'})
        out_clash = os.path.join(self.root, 'bundles/clash')
        combine_builds.export(clash, 'mtk', out_clash)
        with self.assertRaises(combine_builds.CombineError) as caught:
            combine_builds.combine([out_grt, out_clash],
                                   os.path.join(self.root, 'delivery'))
        self.assertIn('produced by both', str(caught.exception))

    def test_a_directory_that_is_not_a_bundle_is_refused(self):
        self.assertRaises(combine_builds.CombineError, combine_builds.combine,
                          [self.root], os.path.join(self.root, 'delivery'))


class CrossHalfTest(CombineTestCase):
    """Combining is a union only while nothing crosses the halves."""

    def sources(self, grt_modules, mtk_modules):
        roots = {}
        for part, modules in (('grt', grt_modules), ('mtk', mtk_modules)):
            root = os.path.join(self.root, part)
            for module, deps in modules.items():
                write(os.path.join(root, 'garnet', module,
                                   module + combine_builds.MODULE_SUFFIX),
                      'MODULE_NAME := %s\nMODULE_TYPE := driver\n'
                      'MODULE_DEPS := %s\n' % (module, ' '.join(deps)))
            roots[part] = root
        return roots

    def test_independent_halves_have_no_crossings(self):
        roots = self.sources({'machina': []}, {'mt8678_ufs': []})
        self.assertEqual(combine_builds.cross_half_edges(roots), [])

    def test_a_crossing_is_reported(self):
        roots = self.sources({'machina': ['mt8678_ufs']}, {'mt8678_ufs': []})
        crossings = combine_builds.cross_half_edges(roots)
        self.assertEqual(crossings,
                         ['grt/machina depends on mtk/mt8678_ufs'])

    def test_a_dependency_on_something_outside_both_halves_is_not_a_crossing(
            self):
        # libddk's libraries are named in MODULE_LIBS, not MODULE_DEPS, but a
        # dependency on a module neither half declares must not be mistaken
        # for one crossing between them.
        roots = self.sources({'machina': ['nowhere']}, {'mt8678_ufs': []})
        self.assertEqual(combine_builds.cross_half_edges(roots), [])


class ModuleReadingTest(CombineTestCase):

    def test_two_descriptions_with_one_name_are_refused(self):
        root = os.path.join(self.root, 'src')
        for where in ('a', 'b'):
            write(os.path.join(root, where, 'block.ddk.mk'),
                  'MODULE_NAME := block\nMODULE_TYPE := driver\n')
        self.assertRaises(combine_builds.CombineError,
                          combine_builds.read_modules, root)

    def test_an_unsupported_type_is_refused(self):
        root = os.path.join(self.root, 'src')
        write(os.path.join(root, 'x.ddk.mk'),
              'MODULE_NAME := x\nMODULE_TYPE := firmware\n')
        self.assertRaises(combine_builds.CombineError,
                          combine_builds.read_modules, root)

    def test_the_default_type_is_driver(self):
        root = os.path.join(self.root, 'src')
        write(os.path.join(root, 'x.ddk.mk'), 'MODULE_NAME := x\n')
        self.assertEqual(combine_builds.read_modules(root)['x']['type'],
                         'driver')


if __name__ == '__main__':
    unittest.main()

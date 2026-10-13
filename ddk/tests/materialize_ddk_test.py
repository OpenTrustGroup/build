#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for materialize_ddk.py."""

import json
import os
import shutil
import sys
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import materialize_ddk


def make_layout(atoms, roots=None, component='libddk', content=None):
    roots = roots if roots is not None else ['include', 'lib', 'sysroot']
    return {
        'format_version': 1,
        'component': component,
        'roots': roots,
        'content': content if content is not None
                   else dict((root, ['header', 'binary']) for root in roots),
        'closed_source_roots': ['zircon'],
        'atoms': atoms,
    }


def make_atom(domain, name, files, label=None):
    return {
        'id': {'domain': domain, 'name': name},
        'gn-label': label or '//fake:%s' % name,
        'tags': {'domain': domain, 'type': 'sources'},
        'deps': [],
        'package-deps': [],
        'files': files,
    }


def make_manifest(atoms):
    return {
        'ids': [atom['id'] for atom in atoms],
        'atoms': atoms,
        'meta': {
            'target-arch': 'arm64',
            'host-arch': 'x86_64-linux-gnu',
            'source-locator-schema': '1',
        },
    }


class MaterializeTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='ddk-materialize-test-')
        self.out_root = os.path.join(self.root, 'out')
        self.source_root = os.path.join(self.root, 'src')
        self.dest = os.path.join(self.root, 'libddk')
        os.makedirs(self.out_root)
        os.makedirs(self.source_root)
        self.roots = {'out': self.out_root, 'source': self.source_root}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_source(self, root_id, relative, content='x'):
        base = self.out_root if root_id == 'out' else self.source_root
        path = os.path.join(base, relative)
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'w') as handle:
            handle.write(content)
        return '%s://%s' % (root_id, relative)

    def plan(self, manifest, layout):
        return materialize_ddk.plan_tree(manifest, layout, self.roots)

    # -- locator resolution --------------------------------------------------

    def test_resolves_both_roots(self):
        out_locator = self.write_source('out', 'arm64-shared/libzx.a')
        src_locator = self.write_source('source', 'garnet/public/lib/grt-i2c.h')
        self.assertTrue(materialize_ddk.resolve_locator(
            out_locator, self.roots).startswith(self.out_root))
        self.assertTrue(materialize_ddk.resolve_locator(
            src_locator, self.roots).startswith(self.source_root))

    def test_rejects_unknown_root(self):
        self.assertRaises(materialize_ddk.MaterializeError,
                          materialize_ddk.resolve_locator,
                          'yocto://a/b', self.roots)

    def test_rejects_absolute_and_traversing_locators(self):
        for locator in ('out:///etc/passwd', 'out://../escape', 'out://'):
            self.assertRaises(materialize_ddk.MaterializeError,
                              materialize_ddk.resolve_locator,
                              locator, self.roots)

    def test_rejects_missing_source(self):
        self.assertRaises(materialize_ddk.MaterializeError,
                          materialize_ddk.resolve_locator,
                          'out://not/here.h', self.roots)

    # -- layout coverage -----------------------------------------------------

    def test_undeclared_atom_is_an_error(self):
        locator = self.write_source('source', 'a.h')
        manifest = make_manifest([
            make_atom('cpp', 'known', [{'source': locator,
                                        'destination': 'include/a.h',
                                        'packaged': False}]),
            make_atom('cpp', 'surprise', [{'source': locator,
                                           'destination': 'include/b.h',
                                           'packaged': False}]),
        ])
        layout = make_layout({'cpp/known': {'prefix': '', 'reason': 'test'}})
        try:
            self.plan(manifest, layout)
            self.fail('expected an undeclared-atom failure')
        except materialize_ddk.MaterializeError as error:
            self.assertIn('cpp/surprise', str(error))

    def test_stale_layout_entry_is_an_error(self):
        locator = self.write_source('source', 'a.h')
        manifest = make_manifest([
            make_atom('cpp', 'known', [{'source': locator,
                                        'destination': 'include/a.h',
                                        'packaged': False}]),
        ])
        layout = make_layout({
            'cpp/known': {'prefix': '', 'reason': 'test'},
            'cpp/gone': {'prefix': '', 'reason': 'test'},
        })
        try:
            self.plan(manifest, layout)
            self.fail('expected a stale-layout failure')
        except materialize_ddk.MaterializeError as error:
            self.assertIn('cpp/gone', str(error))

    def test_optional_layout_entry_may_be_absent(self):
        locator = self.write_source('source', 'a.h')
        manifest = make_manifest([
            make_atom('cpp', 'known', [{'source': locator,
                                        'destination': 'include/a.h',
                                        'packaged': False}]),
        ])
        layout = make_layout({
            'cpp/known': {'prefix': '', 'reason': 'test'},
            'cpp/gone': {'prefix': '', 'reason': 'test', 'optional': True},
        })
        plan, _ = self.plan(manifest, layout)
        self.assertEqual(sorted(plan), ['include/a.h'])

    # -- drop lists ----------------------------------------------------------

    def test_drop_removes_named_file_and_directory(self):
        glue = self.write_source('out', 'obj/ddk/BUILD.bazel')
        header = self.write_source('source', 'ddk/driver.h')
        debug = self.write_source('out', 'lib.unstripped/libddk.so')
        manifest = make_manifest([
            make_atom('cpp', 'ddk', [
                {'source': glue, 'destination': 'BUILD.bazel',
                 'packaged': False},
                {'source': header, 'destination': 'include/ddk/driver.h',
                 'packaged': False},
                {'source': debug, 'destination': 'debug/libddk.so',
                 'packaged': False},
            ]),
        ])
        layout = make_layout({
            'cpp/ddk': {'prefix': '', 'reason': 'r',
                        'drop': ['BUILD.bazel', 'debug/']},
        })
        plan, _ = self.plan(manifest, layout)
        self.assertEqual(sorted(plan), ['include/ddk/driver.h'])

    def test_drop_does_not_clobber_the_file_being_examined(self):
        # A Python 2 comprehension leaks its variable; if the drop filter
        # reuses the loop name the next file is read as a string.
        first = self.write_source('out', 'obj/BUILD.bazel')
        second = self.write_source('source', 'a.h')
        third = self.write_source('source', 'b.h')
        manifest = make_manifest([
            make_atom('cpp', 'x', [
                {'source': first, 'destination': 'BUILD.bazel',
                 'packaged': False},
                {'source': second, 'destination': 'include/a.h',
                 'packaged': False},
                {'source': third, 'destination': 'include/b.h',
                 'packaged': False},
            ]),
        ])
        layout = make_layout({
            'cpp/x': {'prefix': '', 'reason': 'r', 'drop': ['BUILD.bazel']},
        })
        plan, _ = self.plan(manifest, layout)
        self.assertEqual(sorted(plan), ['include/a.h', 'include/b.h'])

    def test_drop_pattern_matching_nothing_is_an_error(self):
        header = self.write_source('source', 'a.h')
        manifest = make_manifest([
            make_atom('cpp', 'x', [{'source': header,
                                    'destination': 'include/a.h',
                                    'packaged': False}]),
        ])
        layout = make_layout({
            'cpp/x': {'prefix': '', 'reason': 'r', 'drop': ['BUILD.bazel']},
        })
        try:
            self.plan(manifest, layout)
            self.fail('expected a stale-drop failure')
        except materialize_ddk.MaterializeError as error:
            self.assertIn('BUILD.bazel', str(error))

    # -- layout policy -------------------------------------------------------

    def test_prefix_is_applied(self):
        locator = self.write_source('out', 'sysroot/lib/libc.so')
        manifest = make_manifest([
            make_atom('cpp', 'system', [{'source': locator,
                                         'destination': 'lib/libc.so',
                                         'packaged': False}]),
        ])
        layout = make_layout({
            'cpp/system': {'prefix': 'sysroot', 'reason': 'ships whole'},
        })
        plan, provenance = self.plan(manifest, layout)
        self.assertEqual(sorted(plan), ['sysroot/lib/libc.so'])
        self.assertEqual(provenance['sysroot/lib/libc.so'], 'cpp/system')

    def test_destination_outside_roots_is_an_error(self):
        locator = self.write_source('out', 'x.a')
        manifest = make_manifest([
            make_atom('cpp', 'stray', [{'source': locator,
                                        'destination': 'obj/x.a',
                                        'packaged': False}]),
        ])
        layout = make_layout({'cpp/stray': {'prefix': '', 'reason': 'test'}})
        try:
            self.plan(manifest, layout)
            self.fail('expected a root-policy failure')
        except materialize_ddk.MaterializeError as error:
            self.assertIn('outside the declared roots', str(error))

    def test_producer_paths_do_not_leak_into_destinations(self):
        # The source lives at a build-machine-shaped path; the destination is
        # whatever the declaration says, and nothing else.
        locator = self.write_source(
            'out', 'arm64-shared/obj/zircon/public/lib/zx/libzx.a')
        manifest = make_manifest([
            make_atom('cpp', 'zx_ddk', [{'source': locator,
                                         'destination': 'lib/libzx.a',
                                         'packaged': False}]),
        ])
        layout = make_layout({'cpp/zx_ddk': {'prefix': '', 'reason': 'test'}})
        plan, _ = self.plan(manifest, layout)
        self.assertEqual(sorted(plan), ['lib/libzx.a'])

    def test_collision_between_atoms_is_an_error(self):
        first = self.write_source('source', 'one.h', 'first')
        second = self.write_source('source', 'two.h', 'second')
        manifest = make_manifest([
            make_atom('cpp', 'a', [{'source': first,
                                    'destination': 'include/dup.h',
                                    'packaged': False}]),
            make_atom('cpp', 'b', [{'source': second,
                                    'destination': 'include/dup.h',
                                    'packaged': False}]),
        ])
        layout = make_layout({
            'cpp/a': {'prefix': '', 'reason': 'test'},
            'cpp/b': {'prefix': '', 'reason': 'test'},
        })
        try:
            self.plan(manifest, layout)
            self.fail('expected a collision failure')
        except materialize_ddk.MaterializeError as error:
            self.assertIn('claimed twice', str(error))

    def test_identical_duplicate_is_tolerated(self):
        shared = self.write_source('source', 'same.h')
        manifest = make_manifest([
            make_atom('cpp', 'a', [{'source': shared,
                                    'destination': 'include/same.h',
                                    'packaged': False}]),
            make_atom('cpp', 'b', [{'source': shared,
                                    'destination': 'include/same.h',
                                    'packaged': False}]),
        ])
        layout = make_layout({
            'cpp/a': {'prefix': '', 'reason': 'test'},
            'cpp/b': {'prefix': '', 'reason': 'test'},
        })
        plan, _ = self.plan(manifest, layout)
        self.assertEqual(sorted(plan), ['include/same.h'])

    # -- content policy ------------------------------------------------------

    def test_source_may_not_ship_in_a_delivered_component(self):
        # A component is an interface plus the binaries implementing it; the
        # Zircon sysroot otherwise brings its Rust syscall definitions along.
        rust = self.write_source('out', 'sysroot/definitions.rs')
        manifest = make_manifest([
            make_atom('cpp', 'system', [{'source': rust,
                                         'destination': 'include/defs.rs',
                                         'packaged': False}]),
        ])
        layout = make_layout({'cpp/system': {'prefix': '', 'reason': 'r'}},
                             content={'include': ['header'],
                                      'lib': ['binary'],
                                      'sysroot': ['header', 'binary']})
        try:
            self.plan(manifest, layout)
            self.fail('expected a content-policy failure')
        except materialize_ddk.MaterializeError as error:
            self.assertIn('include/defs.rs', str(error))

    def test_a_binary_may_not_ship_where_only_headers_belong(self):
        archive = self.write_source('out', 'libz.a')
        manifest = make_manifest([
            make_atom('cpp', 'x', [{'source': archive,
                                    'destination': 'include/libz.a',
                                    'packaged': False}]),
        ])
        layout = make_layout({'cpp/x': {'prefix': '', 'reason': 'r'}},
                             content={'include': ['header'],
                                      'lib': ['binary'],
                                      'sysroot': ['header', 'binary']})
        self.assertRaises(materialize_ddk.MaterializeError,
                          self.plan, manifest, layout)

    def test_a_source_set_carries_whatever_the_tree_carries(self):
        code = self.write_source('source', 'garnet/drivers/x/y.c')
        manifest = make_manifest([
            make_atom('cpp', 'x', [{'source': code,
                                    'destination': 'garnet/drivers/x/y.c',
                                    'packaged': False}]),
        ])
        layout = make_layout({'cpp/x': {'prefix': '', 'reason': 'r'}},
                             roots=['garnet'], content={'garnet': ['source']})
        plan, _ = self.plan(manifest, layout)
        self.assertEqual(sorted(plan), ['garnet/drivers/x/y.c'])

    def test_content_must_cover_every_root(self):
        path = os.path.join(self.root, 'layout.json')
        layout = make_layout({'cpp/a': {'prefix': '', 'reason': 'r'}})
        del layout['content']['lib']
        with open(path, 'w') as handle:
            json.dump(layout, handle)
        self.assertRaises(materialize_ddk.MaterializeError,
                          materialize_ddk.read_layout, path)

    def test_an_excluded_atom_contributes_nothing(self):
        # Arrives through another atom's package_deps and has no place in the
        # component; naming it is still required so it cannot slip in later.
        code = self.write_source('source', 'zircon/system/ulib/zx/vmo.cpp')
        header = self.write_source('source', 'a.h')
        manifest = make_manifest([
            make_atom('cpp', 'zx', [{'source': code, 'destination': 'vmo.cpp',
                                     'packaged': False}]),
            make_atom('cpp', 'keep', [{'source': header,
                                       'destination': 'include/a.h',
                                       'packaged': False}]),
        ])
        layout = make_layout({
            'cpp/zx': {'prefix': '', 'exclude': True, 'reason': 'source form'},
            'cpp/keep': {'prefix': '', 'reason': 'r'},
        })
        plan, _ = self.plan(manifest, layout)
        self.assertEqual(sorted(plan), ['include/a.h'])

    def test_exclude_and_drop_are_mutually_exclusive(self):
        path = os.path.join(self.root, 'layout.json')
        with open(path, 'w') as handle:
            json.dump(make_layout({'cpp/a': {'prefix': '', 'reason': 'r',
                                             'exclude': True,
                                             'drop': ['x.h']}}), handle)
        self.assertRaises(materialize_ddk.MaterializeError,
                          materialize_ddk.read_layout, path)

    # -- closed source ---------------------------------------------------------

    def test_closed_source_may_ship_headers(self):
        header = self.write_source('source', 'zircon/system/public/zx/types.h')
        manifest = make_manifest([
            make_atom('cpp', 'z', [{'source': header,
                                    'destination': 'include/zircon/types.h',
                                    'packaged': False}]),
        ])
        layout = make_layout({'cpp/z': {'prefix': '', 'reason': 'r'}})
        plan, _ = self.plan(manifest, layout)
        self.assertEqual(sorted(plan), ['include/zircon/types.h'])

    def test_closed_source_may_not_ship_implementation(self):
        code = self.write_source('source', 'zircon/system/ulib/zx/vmo.cpp')
        manifest = make_manifest([
            make_atom('cpp', 'z', [{'source': code,
                                    'destination': 'garnet/vmo.cpp',
                                    'packaged': False}]),
        ])
        layout = make_layout({'cpp/z': {'prefix': '', 'reason': 'r'}},
                             roots=['garnet'], content={'garnet': ['source']})
        try:
            self.plan(manifest, layout)
            self.fail('expected a closed-source failure')
        except materialize_ddk.MaterializeError as error:
            self.assertIn('zircon/system/ulib/zx/vmo.cpp', str(error))
            self.assertIn('closed source', str(error))

    def test_a_source_set_may_still_carry_our_own_sources(self):
        code = self.write_source('source', 'garnet/drivers/x/y.c')
        manifest = make_manifest([
            make_atom('cpp', 'x', [{'source': code,
                                    'destination': 'garnet/drivers/x/y.c',
                                    'packaged': False}]),
        ])
        layout = make_layout({'cpp/x': {'prefix': '', 'reason': 'r'}},
                             roots=['garnet'], content={'garnet': ['source']})
        plan, _ = self.plan(manifest, layout)
        self.assertEqual(sorted(plan), ['garnet/drivers/x/y.c'])

    # -- layout validation ---------------------------------------------------

    def test_layout_requires_a_reason_per_atom(self):
        path = os.path.join(self.root, 'layout.json')
        with open(path, 'w') as handle:
            json.dump(make_layout({'cpp/a': {'prefix': ''}}), handle)
        self.assertRaises(materialize_ddk.MaterializeError,
                          materialize_ddk.read_layout, path)

    def test_layout_rejects_unknown_format(self):
        path = os.path.join(self.root, 'layout.json')
        layout = make_layout({'cpp/a': {'prefix': '', 'reason': 'r'}})
        layout['format_version'] = 99
        with open(path, 'w') as handle:
            json.dump(layout, handle)
        self.assertRaises(materialize_ddk.MaterializeError,
                          materialize_ddk.read_layout, path)

    def test_layout_rejects_traversing_prefix(self):
        path = os.path.join(self.root, 'layout.json')
        with open(path, 'w') as handle:
            json.dump(make_layout(
                {'cpp/a': {'prefix': '../oops', 'reason': 'r'}}), handle)
        self.assertRaises(materialize_ddk.MaterializeError,
                          materialize_ddk.read_layout, path)

    def test_layout_round_trip(self):
        path = os.path.join(self.root, 'layout.json')
        with open(path, 'w') as handle:
            json.dump(make_layout(
                {'cpp/a': {'prefix': 'sysroot', 'reason': 'r'}}), handle)
        layout = materialize_ddk.read_layout(path)
        self.assertEqual(layout['component'], 'libddk')
        self.assertEqual(layout['atoms']['cpp/a']['prefix'], 'sysroot')

    # -- manifest validation -------------------------------------------------

    def test_non_portable_manifest_is_rejected(self):
        path = os.path.join(self.root, 'manifest.json')
        manifest = make_manifest([])
        del manifest['meta']['source-locator-schema']
        with open(path, 'w') as handle:
            json.dump(manifest, handle)
        try:
            materialize_ddk.read_manifest(path)
            self.fail('expected a portability failure')
        except materialize_ddk.MaterializeError as error:
            self.assertIn('not portable', str(error))

    # -- tree writing --------------------------------------------------------

    def test_writes_tree_and_contents(self):
        header = self.write_source('source', 'garnet/public/lib/grt-i2c.h',
                                   '#pragma once\n')
        archive = self.write_source('out', 'arm64-shared/libzx.a', 'ar\n')
        manifest = make_manifest([
            make_atom('cpp', 'grt_headers',
                      [{'source': header,
                        'destination': 'include/grt/grt-i2c.h',
                        'packaged': False}]),
            make_atom('cpp', 'zx_ddk',
                      [{'source': archive, 'destination': 'lib/libzx.a',
                        'packaged': False}]),
        ])
        layout = make_layout({
            'cpp/grt_headers': {'prefix': '', 'reason': 'public API'},
            'cpp/zx_ddk': {'prefix': '', 'reason': 'DDK archive'},
        })
        plan, provenance = self.plan(manifest, layout)
        contents = materialize_ddk.write_tree(
            self.dest, plan, provenance, 'libddk', manifest['meta'],
            self.root)

        self.assertTrue(os.path.isfile(
            os.path.join(self.dest, 'include/grt/grt-i2c.h')))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, 'lib/libzx.a')))
        self.assertEqual(contents['target_arch'], 'arm64')
        paths = [item['path'] for item in contents['files']]
        # BUILD_INFO.json is written by write_tree and listed like anything
        # else: an export that cannot say which build it came from is one
        # nobody can answer a question about later.
        self.assertEqual(paths, ['include/grt/grt-i2c.h', 'lib/libzx.a',
                                 'BUILD_INFO.json'])
        self.assertEqual(contents['files'][0]['atom'], 'cpp/grt_headers')

        with open(os.path.join(self.dest,
                               materialize_ddk.CONTENTS_FILE)) as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk['files'], contents['files'])

    def test_rewrite_removes_files_that_are_no_longer_declared(self):
        first = self.write_source('source', 'a.h')
        second = self.write_source('source', 'b.h')
        layout_two = make_layout({
            'cpp/a': {'prefix': '', 'reason': 'r'},
            'cpp/b': {'prefix': '', 'reason': 'r'},
        })
        manifest_two = make_manifest([
            make_atom('cpp', 'a', [{'source': first,
                                    'destination': 'include/a.h',
                                    'packaged': False}]),
            make_atom('cpp', 'b', [{'source': second,
                                    'destination': 'include/b.h',
                                    'packaged': False}]),
        ])
        plan, provenance = self.plan(manifest_two, layout_two)
        materialize_ddk.write_tree(self.dest, plan, provenance, 'libddk',
                                   manifest_two['meta'], self.root)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, 'include/b.h')))

        layout_one = make_layout({'cpp/a': {'prefix': '', 'reason': 'r'}})
        manifest_one = make_manifest([
            make_atom('cpp', 'a', [{'source': first,
                                    'destination': 'include/a.h',
                                    'packaged': False}]),
        ])
        plan, provenance = self.plan(manifest_one, layout_one)
        materialize_ddk.write_tree(self.dest, plan, provenance, 'libddk',
                                   manifest_one['meta'], self.root)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, 'include/a.h')))
        self.assertFalse(os.path.exists(os.path.join(self.dest, 'include/b.h')))

    def test_depfile_lists_every_declared_source(self):
        header = self.write_source('source', 'a.h')
        path = os.path.join(self.root, 'ddk.d')
        materialize_ddk.write_depfile(
            path, 'ddk.stamp',
            [os.path.join(self.source_root, 'a.h'), '/tmp/layout.json'])
        with open(path) as handle:
            line = handle.read().strip()
        self.assertTrue(line.startswith('ddk.stamp: '))
        self.assertIn('a.h', line)
        self.assertIn('layout.json', line)
        self.assertTrue(header)



class NormalizeDestinationTest(unittest.TestCase):
    """A `.` segment is a spelling, not an error."""

    def test_dot_segment_is_dropped(self):
        self.assertEqual(
            materialize_ddk._normalize_destination('include/./nbl_fwk/a.h'),
            'include/nbl_fwk/a.h')

    def test_double_slash_is_collapsed(self):
        self.assertEqual(
            materialize_ddk._normalize_destination('lib//libfoo.a'),
            'lib/libfoo.a')

    def test_parent_segment_is_left_for_the_check_to_reject(self):
        self.assertFalse(_is_clean(
            materialize_ddk._normalize_destination('include/../etc/passwd')))


def _is_clean(path):
    return materialize_ddk._is_clean_relative(path)



class LicenceInstallationTest(unittest.TestCase):
    """A component that carries no licence text is not deliverable."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='materialize-licence-test-')
        self.out = os.path.join(self.root, 'component')
        self.source = os.path.join(self.root, 'a.h')
        with open(self.source, 'w') as handle:
            handle.write('/* BSD-3-Clause */\n')
        self.declaration = os.path.join(self.root, 'licences.json')
        with open(os.path.join(self.root, 'OURS'), 'w') as handle:
            handle.write('our licence\n')
        with open(self.declaration, 'w') as handle:
            json.dump({'format_version': 1, 'components': [
                {'prefix': '', 'name': 'ours', 'spdx': 'BSD-3-Clause',
                 'license_file': '//OURS', 'note': 'everything'}]}, handle)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def materialize(self):
        return materialize_ddk.write_tree(
            self.out, {'include/a.h': self.source},
            {'include/a.h': 'cpp/a'}, 'libddk', {},
            self.root, self.declaration)

    def test_the_licence_texts_and_notice_are_installed(self):
        self.materialize()
        self.assertTrue(os.path.isfile(
            os.path.join(self.out, 'NOTICE')))
        self.assertTrue(os.path.isfile(
            os.path.join(self.out, 'LICENSES', 'ours.txt')))

    def test_the_manifest_lists_them(self):
        contents = self.materialize()
        listed = dict((entry['path'], entry) for entry in contents['files'])
        self.assertIn('NOTICE', listed)
        self.assertIn('LICENSES/ours.txt', listed)
        self.assertEqual(listed['NOTICE']['atom'], 'licence')

    def test_copyleft_the_declaration_does_not_admit_stops_the_export(self):
        with open(self.source, 'w') as handle:
            handle.write('/* GNU General Public License version 2 */\n')
        self.assertRaises(materialize_ddk.MaterializeError, self.materialize)

    def test_without_a_declaration_nothing_is_installed(self):
        materialize_ddk.write_tree(self.out, {'include/a.h': self.source},
                                   {'include/a.h': 'cpp/a'}, 'libddk', {},
                                   self.root)
        self.assertFalse(os.path.exists(os.path.join(self.out, 'NOTICE')))



class SealTest(unittest.TestCase):
    """The manifest has to describe the component that is actually there."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='seal-test-')
        self.component = os.path.join(self.root, 'c')
        os.makedirs(os.path.join(self.component, 'lib'))
        self.write('lib/a.a', 'a')
        with open(os.path.join(self.component, 'SDK_CONTENTS.json'),
                  'w') as handle:
            json.dump({'format_version': 1, 'component': 'libddk',
                       'files': [{'path': 'lib/a.a', 'atom': 'cpp/a',
                                  'executable': False, 'sha256': 'stale'}]},
                      handle)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relative, content):
        path = os.path.join(self.component, relative)
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'w') as handle:
            handle.write(content)

    def listed(self):
        with open(os.path.join(self.component,
                               'SDK_CONTENTS.json')) as handle:
            return sorted(e['path'] for e in json.load(handle)['files'])

    def test_a_file_added_after_the_manifest_is_picked_up(self):
        # NOTICE and TRIM_REPORT.json arrive after the manifest is written.
        self.write('NOTICE', 'notice\n')
        materialize_ddk.seal(self.component)
        self.assertEqual(self.listed(), ['NOTICE', 'lib/a.a'])

    def test_a_file_removed_after_the_manifest_is_dropped(self):
        # The licence step rewrites LICENSES/ and two texts stop applying.
        os.remove(os.path.join(self.component, 'lib/a.a'))
        materialize_ddk.seal(self.component)
        self.assertEqual(self.listed(), [])

    def test_the_atom_of_a_file_that_was_already_listed_survives(self):
        materialize_ddk.seal(self.component)
        with open(os.path.join(self.component,
                               'SDK_CONTENTS.json')) as handle:
            entry = json.load(handle)['files'][0]
        self.assertEqual(entry['atom'], 'cpp/a')
        self.assertNotEqual(entry['sha256'], 'stale')

    def test_check_reports_both_directions(self):
        self.write('NOTICE', 'notice\n')
        os.remove(os.path.join(self.component, 'lib/a.a'))
        missing, extra = materialize_ddk.check_manifest(self.component)
        self.assertEqual(missing, ['lib/a.a'])
        self.assertEqual(extra, ['NOTICE'])

    def test_a_sealed_component_agrees_with_its_tree(self):
        self.write('NOTICE', 'notice\n')
        materialize_ddk.seal(self.component)
        self.assertEqual(materialize_ddk.check_manifest(self.component),
                         ([], []))

    def test_a_directory_without_a_manifest_is_refused(self):
        self.assertRaises(materialize_ddk.MaterializeError,
                          materialize_ddk.seal, self.root)


if __name__ == '__main__':
    unittest.main()

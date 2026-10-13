#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for make_delivery.py.

What a delivery contains is the last thing decided and the only thing the
customer sees, so the tests here are about what must not be in it: a fifth
entry, a symlink, a half with nothing to build, a component with no compiler.
Each of those produces a directory that looks like a delivery and is not one.
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

import make_delivery  # noqa: E402


class DeliveryTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='delivery-test-')
        self.out = os.path.join(self.root, 'output')
        self.libddk = os.path.join(self.root, 'libddk-release')
        self.makefile = os.path.join(self.root, 'selftest.mk')

        self.write(self.libddk, 'include/ddk/driver.h', '#pragma once\n')
        self.write(self.libddk, 'toolchain/bin/clang', '#!/bin/sh\n')
        self.licence(self.libddk)
        self.write(self.makefile, None, 'all:\n')
        for half, module in (('grt_ddk', 'a'), ('mtk_ddk', 'b')):
            root = os.path.join(self.root, half)
            self.write(root, 'src/drivers/%s.ddk.mk' % module,
                       'MODULE_SRCS := src/drivers/%s.c\n' % module)
            self.licence(root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def licence(self, root):
        """What collect_licenses leaves behind, which every part must carry."""
        self.write(root, 'LICENSES/FILES', 'FILE  COMPONENT  LICENCE\n')
        self.write(root, 'LICENSES/nebula.txt', 'BSD-3-Clause\n')
        self.write(root, 'NOTICE', 'NOTICE\n')

    def write(self, root, relative, content):
        path = root if relative is None else os.path.join(root, relative)
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'w') as handle:
            handle.write(content)
        return path

    def halves(self, *names):
        return [os.path.join(self.root, name) for name in names]

    def build(self, halves=('grt_ddk', 'mtk_ddk'), **kwargs):
        contents, self.identifier = make_delivery.build(
            self.libddk, self.halves(*halves), self.makefile, self.out,
            **kwargs)
        return contents

    # -- what it writes ----------------------------------------------------

    def test_a_delivery_is_directories_and_nothing_else(self):
        # Every part is a repo project, and repo manages directories: a file
        # beside them belongs to nobody.
        self.build()
        self.assertEqual(['grt_ddk', 'libddk', 'mtk_ddk'],
                         sorted(os.listdir(self.out)))

    def test_a_customer_gets_one_half(self):
        self.build(halves=('mtk_ddk',))
        self.assertEqual(['libddk', 'mtk_ddk'], sorted(os.listdir(self.out)))

    def test_every_half_carries_the_build_that_makes_it(self):
        # A half is built from inside itself, so the Makefile travels in it.
        self.build()
        for half in ('grt_ddk', 'mtk_ddk'):
            with open(os.path.join(self.out, half, 'Makefile')) as handle:
                self.assertEqual('all:\n', handle.read())

    def test_a_half_carries_a_component_of_its_own(self):
        # What only one half needs travels inside it, so the customer who
        # receives the other half never sees those headers.
        private = os.path.join(self.root, 'private')
        self.write(private, 'include/fxl/log.h', '#pragma once\n')
        self.write(private, 'lib/libfxl.a', 'ar\n')
        self.build(components=['grt_ddk=' + private])
        self.assertTrue(os.path.isfile(os.path.join(
            self.out, 'grt_ddk/libddk/include/fxl/log.h')))
        self.assertFalse(os.path.exists(os.path.join(
            self.out, 'mtk_ddk/libddk')))
        self.assertFalse(os.path.exists(os.path.join(
            self.out, 'libddk/include/fxl/log.h')))

    def test_it_replaces_the_copy_the_half_arrived_with(self):
        """The half carries the component as built, for its licences and its
        mirror; what ships is the stripped one, and keeping the larger would
        hand over every source path the trim removes."""
        self.write(os.path.join(self.root, 'grt_ddk'),
                   'libddk/lib/libfxl.a', 'unstripped, with DWARF\n')
        private = os.path.join(self.root, 'private')
        self.write(private, 'lib/libfxl.a', 'stripped\n')
        self.build(components=['grt_ddk=' + private])
        with open(os.path.join(self.out,
                               'grt_ddk/libddk/lib/libfxl.a')) as handle:
            self.assertEqual('stripped\n', handle.read())

    def test_a_component_for_a_half_that_is_not_there_is_refused(self):
        private = os.path.join(self.root, 'private')
        self.write(private, 'include/fxl/log.h', 'x')
        try:
            self.build(components=['nobody=' + private])
            self.fail('a component for an absent half was accepted')
        except make_delivery.DeliveryError as error:
            self.assertIn('nobody', str(error))

    def test_a_half_without_the_build_is_refused(self):
        self.build()
        os.remove(os.path.join(self.out, 'mtk_ddk', 'Makefile'))
        try:
            make_delivery.check(self.out, ['grt_ddk', 'mtk_ddk'])
            self.fail('a half with no Makefile was accepted')
        except make_delivery.DeliveryError as error:
            self.assertIn('mtk_ddk', str(error))

    def test_a_half_can_be_renamed_on_the_way_in(self):
        make_delivery.build(self.libddk,
                            ['mtk=' + os.path.join(self.root, 'mtk_ddk')],
                            self.makefile, self.out)
        self.assertIn('mtk', os.listdir(self.out))

    def test_writing_again_replaces_rather_than_adds(self):
        self.build()
        with open(os.path.join(self.out, 'stale.txt'), 'w') as handle:
            handle.write('x')
        self.build()
        self.assertNotIn('stale.txt', os.listdir(self.out))

    # -- what it refuses ---------------------------------------------------

    def test_a_fifth_entry_is_refused(self):
        # The likely one is a build/ left by testing the layout, and a
        # delivery is read as everything in the directory.
        self.build()
        os.makedirs(os.path.join(self.out, 'build'))
        self.assertRaises(make_delivery.DeliveryError, make_delivery.check,
                          self.out, ['grt_ddk', 'mtk_ddk'])

    def test_a_half_without_licences_is_refused(self):
        # A half ships on its own, under repo, with nothing beside it. The
        # component's LICENSES/ is in a tar the recipient may not have
        # unpacked yet, and is not about this half's source anyway.
        shutil.rmtree(os.path.join(self.root, 'mtk_ddk', 'LICENSES'))
        self.assertRaises(make_delivery.DeliveryError, self.build)

    def test_a_half_without_a_notice_is_refused(self):
        os.remove(os.path.join(self.root, 'grt_ddk', 'NOTICE'))
        self.assertRaises(make_delivery.DeliveryError, self.build)

    def test_the_component_without_licences_is_refused(self):
        shutil.rmtree(os.path.join(self.libddk, 'LICENSES'))
        self.assertRaises(make_delivery.DeliveryError, self.build)

    def test_a_symlink_out_of_a_half_is_refused(self):
        # A path back to the machine that built the delivery.
        os.symlink('/etc/hostname',
                   os.path.join(self.root, 'mtk_ddk', 'src', 'link'))
        self.assertRaises(make_delivery.DeliveryError, self.build)

    def test_a_symlink_out_of_the_component_is_refused(self):
        os.symlink('/usr/bin/clang',
                   os.path.join(self.libddk, 'toolchain', 'bin', 'clang++'))
        self.assertRaises(make_delivery.DeliveryError, self.build)

    def test_a_relative_link_that_escapes_is_refused(self):
        # It resolves outside once the tree has moved, which is the same
        # thing said less obviously.
        os.symlink('../../../../etc/hostname',
                   os.path.join(self.libddk, 'toolchain', 'bin', 'clang++'))
        self.assertRaises(make_delivery.DeliveryError, self.build)

    def test_a_link_inside_the_delivery_survives_as_a_link(self):
        # The compiler ships four of these -- clang++ and clang-7.0 beside
        # clang, lld beside ld.lld -- and copying them would add 234 MB of
        # the same bytes.
        os.symlink('clang',
                   os.path.join(self.libddk, 'toolchain', 'bin', 'clang++'))
        self.build()
        landed = os.path.join(self.out, 'libddk/toolchain/bin/clang++')
        self.assertTrue(os.path.islink(landed))
        self.assertEqual('clang', os.readlink(landed))
        self.assertTrue(os.path.isfile(landed))

    def test_a_component_without_a_compiler_is_refused(self):
        os.remove(os.path.join(self.libddk, 'toolchain/bin/clang'))
        self.assertRaises(make_delivery.DeliveryError, self.build)

    def test_a_missing_compiler_can_be_allowed_deliberately(self):
        os.remove(os.path.join(self.libddk, 'toolchain/bin/clang'))
        self.build(require_toolchain=False)
        self.assertIn('libddk', os.listdir(self.out))

    def test_a_half_with_nothing_to_build_is_refused(self):
        empty = os.path.join(self.root, 'empty_ddk')
        os.makedirs(os.path.join(empty, 'src'))
        self.assertRaises(
            make_delivery.DeliveryError, make_delivery.build,
            self.libddk, [empty], self.makefile, self.out)

    def test_two_halves_with_one_name_are_refused(self):
        self.assertRaises(
            make_delivery.DeliveryError, make_delivery.build,
            self.libddk, self.halves('mtk_ddk', 'mtk_ddk'),
            self.makefile, self.out)

    def test_a_half_may_not_be_called_libddk(self):
        self.assertRaises(make_delivery.DeliveryError,
                          make_delivery.split_half, 'libddk=/somewhere')

    def test_no_half_at_all_is_refused(self):
        self.assertRaises(
            make_delivery.DeliveryError, make_delivery.build,
            self.libddk, [], self.makefile, self.out)

    def test_a_missing_makefile_is_refused(self):
        self.assertRaises(
            make_delivery.DeliveryError, make_delivery.build,
            self.libddk, self.halves('mtk_ddk'),
            os.path.join(self.root, 'no-such-makefile'), self.out)


class ReleaseTest(DeliveryTest):
    """The one identifier that ties three parts travelling separately.

    Two git repositories and a tar: `repo sync` updates the sources and
    leaves the component where it was, and the build then links against the
    previous ABI. This is what git used to do for free.
    """

    def stamps(self):
        found = {}
        for name in os.listdir(self.out):
            path = os.path.join(self.out, name,
                                make_delivery.RELEASE_NAME)
            if os.path.isfile(path):
                with open(path) as handle:
                    found[name] = handle.read().strip()
        return found

    def test_every_part_carries_the_same_one(self):
        self.build()
        stamps = self.stamps()
        self.assertEqual(['grt_ddk', 'libddk', 'mtk_ddk'], sorted(stamps))
        self.assertEqual(1, len(set(stamps.values())))

    def test_a_half_that_is_a_repository_is_stamped_too(self):
        # The stamp is the one anchor everything downstream reads, and a
        # change pushed for review carries no tag to read instead.  What
        # deploys the delivery rewrites the commit that carries it.
        subprocess.check_call(['git', 'init', '-q',
                               os.path.join(self.root, 'mtk_ddk')])
        self.build()
        stamps = self.stamps()
        self.assertEqual(['grt_ddk', 'libddk', 'mtk_ddk'], sorted(stamps))
        self.assertEqual(1, len(set(stamps.values())))

    def test_the_component_carries_one_even_as_a_repository(self):
        # libddk is a repository too, so that its releases have an order.
        # It is stamped anyway: nobody reviews it, and the file is the one
        # anchor the rest of the delivery reads -- publish takes the id from
        # it, the tag check compares against it, and selftest.mk names a
        # component and a half from different releases by it.
        subprocess.check_call(['git', 'init', '-q', self.libddk])
        self.build()
        stamps = self.stamps()
        self.assertIn('libddk', stamps)
        self.assertEqual(1, len(set(stamps.values())))

    def test_the_same_delivery_gets_the_same_one(self):
        self.build()
        first = self.identifier
        self.build()
        self.assertEqual(first, self.identifier)

    def test_one_file_different_changes_it(self):
        self.build()
        first = self.identifier
        self.write(os.path.join(self.root, 'mtk_ddk'),
                   'src/drivers/b.c', 'int b2;\n')
        self.build()
        self.assertNotEqual(first, self.identifier)

    def test_it_does_not_depend_on_itself(self):
        # The stamp is written after the digest, so a delivery rebuilt over
        # one that was already stamped has to reach the same answer.
        self.build()
        first = self.identifier
        shutil.copytree(self.out, self.out + '-kept')
        self.build()
        self.assertEqual(first, self.identifier)

    def test_it_reads_as_a_date(self):
        self.build()
        day = self.identifier.split('-')[0]
        self.assertTrue(day == 'unknown' or day.isdigit(), self.identifier)


class LayoutTest(DeliveryTest):
    """Each part where the layout says, and nothing else anywhere."""

    LAYOUT = {'libddk': 'libddk/aries_v1', 'grt_ddk': 'grt_ddk/garnet',
              'mtk_ddk': 'mtk_ddk/aries_v1'}

    def test_each_part_sits_one_level_down(self):
        self.build(layout=self.LAYOUT)
        self.assertEqual(['grt_ddk', 'libddk', 'mtk_ddk'],
                         sorted(os.listdir(self.out)))
        self.assertEqual(['garnet'], os.listdir(os.path.join(self.out,
                                                             'grt_ddk')))
        self.assertTrue(os.path.isfile(os.path.join(
            self.out, 'libddk', 'aries_v1', 'include', 'ddk', 'driver.h')))
        self.assertTrue(os.path.isfile(os.path.join(
            self.out, 'mtk_ddk', 'aries_v1', 'Makefile')))
        self.assertTrue(os.path.isfile(os.path.join(
            self.out, 'libddk', 'aries_v1', 'RELEASE')))

    def test_the_id_does_not_depend_on_the_layout(self):
        self.build()
        flat = self.identifier
        self.build(layout=self.LAYOUT)
        self.assertEqual(flat, self.identifier)

    def test_a_stray_entry_in_a_wrapper_is_refused(self):
        self.build(layout=self.LAYOUT)
        os.makedirs(os.path.join(self.out, 'libddk', 'build'))
        with self.assertRaises(make_delivery.DeliveryError) as caught:
            make_delivery.check(self.out, ['grt_ddk', 'mtk_ddk'],
                                self.LAYOUT)
        self.assertIn('libddk/build', str(caught.exception))

    def test_a_missing_part_is_named_by_its_place(self):
        self.build(layout=self.LAYOUT)
        shutil.rmtree(os.path.join(self.out, 'mtk_ddk', 'aries_v1'))
        with self.assertRaises(make_delivery.DeliveryError) as caught:
            make_delivery.check(self.out, ['grt_ddk', 'mtk_ddk'],
                                self.LAYOUT)
        self.assertIn('mtk_ddk/aries_v1', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

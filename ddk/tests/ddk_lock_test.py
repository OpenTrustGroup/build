#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for ddk_lock.py.

A release is held to the last one, and these say exactly where it is held:
what a consumer could break on stops it, and what they could not does not.
llvm-nm is replaced by a script that prints the symbols a test assigned to
each library, so the rules are tested without a toolchain.
"""

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import ddk_lock  # noqa: E402

FAKE_NM = u'''#!%s
import json, os, sys
table = json.load(open(os.path.join(os.path.dirname(sys.argv[0]), 'symbols.json')))
path = sys.argv[-1]
for symbol in table.get(os.path.basename(path), []):
    print('%%s T 0 0' %% symbol)
'''


def write(path, content, binary=False):
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, 'wb') as handle:
        handle.write(content if binary else content.encode('utf-8'))


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


class LockTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='ddk-lock-test-')
        self.delivery = os.path.join(self.root, 'output')
        self.nm = os.path.join(self.root, 'nm')
        with io.open(self.nm, 'w', encoding='utf-8') as handle:
            handle.write(FAKE_NM % sys.executable)
        os.chmod(self.nm, os.stat(self.nm).st_mode | stat.S_IEXEC)
        self.symbols({'libfdt.a': ['fdt_check_header', 'fdt_size_cells']})

        lib = os.path.join(self.delivery, 'libddk')
        write(os.path.join(lib, 'include/ddk/driver.h'), '#pragma once\n')
        write(os.path.join(lib, 'lib/libfdt.a'), b'!<arch>\nmembers', True)
        write(os.path.join(lib, 'sysroot/lib/libdl.so'), '/* a linker script */')
        write(os.path.join(lib, 'abi/ABI.fingerprint'), 'aaaa\n')
        write(os.path.join(lib, 'LICENSES/FILES'), 'not the surface\n')
        write(os.path.join(lib, 'RELEASE'), 'r1\n')
        self.module('mtk_ddk', 'garnet/drivers/i2c', 'mt8678_i2c', 'driver',
                    'garnet/drivers/i2c/i2c.c')
        self.module('grt_ddk', 'garnet/public/lib/fsl/io', 'fsl_io',
                    'static_library', 'garnet/public/lib/fsl/io/fd.cc')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def symbols(self, table):
        with open(os.path.join(self.root, 'symbols.json'), 'w') as handle:
            json.dump(table, handle)

    def module(self, half, directory, name, kind, source):
        write(os.path.join(self.delivery, half, directory, name + '.ddk.mk'),
              'MODULE_NAME := %s\nMODULE_TYPE := %s\nMODULE_SRCS := %s\n'
              % (name, kind, source))

    def lock(self):
        """Accept the delivery as it is now, and read the lock back."""
        lock_path = os.path.join(self.root, 'libddk.lock.json')
        symbols_path = os.path.join(self.root, 'libddk.symbols')
        lock, symbols = ddk_lock.snapshot(self.delivery, self.nm)
        ddk_lock.write_lock(lock, symbols, lock_path, symbols_path)
        return ddk_lock.read_lock(lock_path, symbols_path)

    def check(self):
        old, old_symbols = self.lock_before
        new, new_symbols = ddk_lock.snapshot(self.delivery, self.nm)
        changes = ddk_lock.compare(old, old_symbols, new, new_symbols)
        return changes, ddk_lock.blocking(changes)


class SnapshotTest(LockTestCase):

    def test_the_surface_is_what_a_consumer_builds_against(self):
        lock, _ = ddk_lock.snapshot(self.delivery, self.nm)
        self.assertEqual(['abi/ABI.fingerprint', 'include/ddk/driver.h',
                          'lib/libfdt.a', 'sysroot/lib/libdl.so'],
                         sorted(lock['files']))

    def test_a_linker_script_is_a_file_without_symbols(self):
        # musl's libdl.so is four lines of text pointing at libc.so.
        lock, symbols = ddk_lock.snapshot(self.delivery, self.nm)
        self.assertIn('sysroot/lib/libdl.so', lock['files'])
        self.assertNotIn('sysroot/lib/libdl.so', symbols)
        self.assertEqual(['fdt_check_header', 'fdt_size_cells'],
                         symbols['lib/libfdt.a'])

    def test_modules_are_read_from_both_halves(self):
        lock, _ = ddk_lock.snapshot(self.delivery, self.nm)
        self.assertEqual('mtk_ddk', lock['modules']['mt8678_i2c']['half'])
        self.assertEqual('static_library', lock['modules']['fsl_io']['type'])

    def test_a_lock_edited_by_hand_is_refused(self):
        self.lock()
        with io.open(os.path.join(self.root, 'libddk.symbols'), 'a',
                     encoding='utf-8') as handle:
            handle.write(u'lib/libfdt.a\tsomething_extra\n')
        self.assertRaises(ddk_lock.LockError, ddk_lock.read_lock,
                          os.path.join(self.root, 'libddk.lock.json'),
                          os.path.join(self.root, 'libddk.symbols'))

    def test_the_same_delivery_is_identical(self):
        self.lock_before = self.lock()
        changes, stops = self.check()
        self.assertEqual([], stops)
        self.assertFalse(any(changes[k] for k in changes))


class AllowedTest(LockTestCase):

    def setUp(self):
        LockTestCase.setUp(self)
        self.lock_before = self.lock()

    def test_a_new_header_is_reported(self):
        write(os.path.join(self.delivery, 'libddk/include/ddk/new.h'), 'x')
        changes, stops = self.check()
        self.assertEqual(['include/ddk/new.h'], changes['files_added'])
        self.assertEqual([], stops)

    def test_an_edited_header_is_reported(self):
        write(os.path.join(self.delivery, 'libddk/include/ddk/driver.h'),
              '#pragma once\n// edited\n')
        changes, stops = self.check()
        self.assertEqual(['include/ddk/driver.h'], changes['files_changed'])
        self.assertEqual([], stops)

    def test_a_library_gaining_symbols_is_reported(self):
        self.symbols({'libfdt.a': ['fdt_check_header', 'fdt_size_cells',
                                   'fdt_new']})
        changes, stops = self.check()
        self.assertEqual({'lib/libfdt.a': ['fdt_new']},
                         changes['symbols_added'])
        self.assertEqual([], stops)

    def test_a_new_module_is_reported(self):
        self.module('grt_ddk', 'garnet/lib/extra', 'extra', 'static_library',
                    'garnet/lib/extra/x.c')
        changes, stops = self.check()
        self.assertEqual(['extra'], changes['modules_added'])
        self.assertEqual([], stops)


class HalfComponentTest(LockTestCase):
    """A half carries a component of its own, and it is a surface too."""

    def setUp(self):
        LockTestCase.setUp(self)
        self.symbols({'libfdt.a': ['fdt_check_header', 'fdt_size_cells'],
                      'libfxl.a': ['fxl_log', 'fxl_set_level']})
        write(os.path.join(self.delivery, 'grt_ddk/libddk/include/fxl/log.h'),
              '#pragma once\n')
        write(os.path.join(self.delivery, 'grt_ddk/libddk/lib/libfxl.a'),
              b'!<arch>\nmembers', True)
        self.lock_before = self.lock()

    def test_it_is_recorded_under_the_half_that_carries_it(self):
        lock, _ = ddk_lock.snapshot(self.delivery, self.nm)
        self.assertIn('include/fxl/log.h',
                      lock['half_components']['grt_ddk'])
        self.assertNotIn('include/fxl/log.h', lock['files'])

    def test_a_header_gone_from_it_stops_the_release(self):
        # GRT's half builds against these exactly as MTK's builds against
        # libddk: a header gone is a compile that fails at the consumer.
        os.remove(os.path.join(self.delivery,
                               'grt_ddk/libddk/include/fxl/log.h'))
        changes, stops = self.check()
        self.assertEqual(['grt_ddk/include/fxl/log.h'],
                         changes['component_files_removed'])
        self.assertIn('component_files_removed', stops)

    def test_a_symbol_it_stops_exporting_stops_the_release(self):
        self.symbols({'libfdt.a': ['fdt_check_header', 'fdt_size_cells'],
                      'libfxl.a': ['fxl_log']})
        changes, stops = self.check()
        self.assertEqual({'grt_ddk/libddk/lib/libfxl.a': ['fxl_set_level']},
                         changes['symbols_removed'])
        self.assertEqual(['symbols_removed'], stops)

    def test_a_header_added_to_it_is_reported_and_allowed(self):
        write(os.path.join(self.delivery,
                           'grt_ddk/libddk/include/fxl/new.h'), 'x')
        changes, stops = self.check()
        self.assertEqual(['grt_ddk/include/fxl/new.h'],
                         changes['component_files_added'])
        self.assertEqual([], stops)


class HalfFilesTest(LockTestCase):

    def setUp(self):
        LockTestCase.setUp(self)
        write(os.path.join(self.delivery, 'grt_ddk/garnet/lib/view.h'), 'x')
        write(os.path.join(self.delivery, 'grt_ddk/RELEASE'), 'r1\n')
        write(os.path.join(self.delivery, 'grt_ddk/LICENSES/FILES'), 'x')
        self.lock_before = self.lock()

    def test_a_halfs_files_are_recorded_without_its_bookkeeping(self):
        lock, _ = ddk_lock.snapshot(self.delivery, self.nm)
        self.assertIn('garnet/lib/view.h', lock['halves']['grt_ddk'])
        self.assertNotIn('RELEASE', lock['halves']['grt_ddk'])
        self.assertFalse([f for f in lock['halves']['grt_ddk']
                          if f.startswith('LICENSES/')])

    def test_a_file_gone_from_a_half_is_reported_and_does_not_stop(self):
        # Nobody links against a source file.
        os.remove(os.path.join(self.delivery, 'grt_ddk/garnet/lib/view.h'))
        changes, stops = self.check()
        self.assertEqual(['grt_ddk/garnet/lib/view.h'],
                         changes['half_files_removed'])
        self.assertEqual([], stops)


class StopTest(LockTestCase):

    def setUp(self):
        LockTestCase.setUp(self)
        self.lock_before = self.lock()

    def test_a_removed_header_stops(self):
        os.remove(os.path.join(self.delivery, 'libddk/include/ddk/driver.h'))
        changes, stops = self.check()
        self.assertEqual(['include/ddk/driver.h'], changes['files_removed'])
        self.assertIn('files_removed', stops)

    def test_a_symbol_no_longer_exported_stops_and_is_named(self):
        self.symbols({'libfdt.a': ['fdt_check_header']})
        changes, stops = self.check()
        self.assertEqual({'lib/libfdt.a': ['fdt_size_cells']},
                         changes['symbols_removed'])
        self.assertEqual(['symbols_removed'], stops)

    def test_a_removed_library_is_one_removal_not_a_symbol_list(self):
        os.remove(os.path.join(self.delivery, 'libddk/lib/libfdt.a'))
        changes, stops = self.check()
        self.assertEqual({}, changes['symbols_removed'])
        self.assertEqual(['files_removed'], stops)

    def test_a_renamed_module_stops_and_says_its_new_name(self):
        os.remove(os.path.join(self.delivery, 'grt_ddk/garnet/public/lib/fsl/io',
                               'fsl_io.ddk.mk'))
        self.module('grt_ddk', 'garnet/public/lib/fsl/io', 'io',
                    'static_library', 'garnet/public/lib/fsl/io/fd.cc')
        changes, stops = self.check()
        self.assertEqual(['fsl_io (now io)'], changes['modules_removed'])
        self.assertIn('modules_removed', stops)

    def test_a_module_changing_half_stops(self):
        os.remove(os.path.join(self.delivery, 'mtk_ddk/garnet/drivers/i2c',
                               'mt8678_i2c.ddk.mk'))
        self.module('grt_ddk', 'garnet/drivers/i2c', 'mt8678_i2c', 'driver',
                    'garnet/drivers/i2c/i2c.c')
        changes, stops = self.check()
        self.assertEqual(['modules_moved'], stops)

    def test_a_different_kernel_abi_stops(self):
        write(os.path.join(self.delivery, 'libddk/abi/ABI.fingerprint'),
              'bbbb\n')
        changes, stops = self.check()
        self.assertEqual(['aaaa', 'bbbb'], changes['abi_changed'])
        self.assertIn('abi_changed', stops)

    def test_accepting_makes_the_change_the_new_promise(self):
        os.remove(os.path.join(self.delivery, 'libddk/include/ddk/driver.h'))
        self.lock_before = self.lock()
        changes, stops = self.check()
        self.assertEqual([], stops)


class LayoutTest(LockTestCase):

    LAYOUT = {'libddk': 'libddk/aries_v1', 'grt_ddk': 'grt_ddk/garnet',
              'mtk_ddk': 'mtk_ddk/aries_v1'}

    def test_the_snapshot_does_not_depend_on_the_layout(self):
        flat, flat_symbols = ddk_lock.snapshot(self.delivery, self.nm)
        nest(self.delivery, self.LAYOUT)
        nested, nested_symbols = ddk_lock.snapshot(self.delivery, self.nm,
                                                   self.LAYOUT)
        self.assertEqual(flat, nested)
        self.assertEqual(flat_symbols, nested_symbols)


if __name__ == '__main__':
    unittest.main()

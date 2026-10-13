#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for export_toolchain.py.

The keep set here is declared, not measured, so the tests that matter are the
ones that catch a declaration which stopped describing the tree: a driver that
moved, a second target appearing beside the first, a runtime filter that
matched nothing.  Each of those produces an export that looks fine and fails
at the customer's first link, which is why every one of them is an error here
rather than a warning.

The ELF fixtures are assembled byte by byte.  A real toolchain is 1.4 GB and
not something a test can carry, and the gates read section headers and
e_machine directly -- so a synthetic header exercises exactly the code that
runs in a release.
"""

from __future__ import print_function

import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import export_toolchain  # noqa: E402


def archive(members):
    """A Unix archive holding the given member payloads."""
    out = [b'!<arch>\n']
    for index, blob in enumerate(members):
        name = ('m%d.o' % index).ljust(16).encode('ascii')
        header = name + b'0'.ljust(12) + b'0'.ljust(6) + b'0'.ljust(6)
        header += b'644'.ljust(8) + str(len(blob)).ljust(10).encode('ascii')
        header += b'`\n'
        out.append(header)
        out.append(blob)
        if len(blob) % 2:
            out.append(b'\n')
    return b''.join(out)


def elf(machine, sections=(), needed=()):
    """A 64-bit little-endian ELF with the given section names.

    Enough of one for the readers under test: a section header table with a
    string table, and optionally a PT_DYNAMIC with DT_NEEDED entries.
    """
    names = b'\0'
    offsets = []
    for name in list(sections) + ['.shstrtab']:
        offsets.append(len(names))
        names += name.encode('ascii') + b'\0'

    ehsize = 64
    phentsize, shentsize = 56, 64
    count = len(sections) + 1

    dynamic = b''
    strings = b'\0'
    string_offsets = []
    for library in needed:
        string_offsets.append(len(strings))
        strings += library.encode('ascii') + b'\0'

    phoff = ehsize
    phnum = 2 if needed else 0
    body_off = phoff + phnum * phentsize

    dynstr_off = body_off
    names_off = dynstr_off + len(strings)
    if needed:
        for offset in string_offsets:
            dynamic += struct.pack('<QQ', 1, offset)      # DT_NEEDED
        dynamic += struct.pack('<QQ', 5, dynstr_off)      # DT_STRTAB (vaddr)
        dynamic += struct.pack('<QQ', 0, 0)               # DT_NULL
    dyn_off = names_off + len(names)
    shoff = dyn_off + len(dynamic)

    data = bytearray()
    data += b'\x7fELF\x02\x01\x01\x00' + b'\0' * 8
    data += struct.pack('<HHI', 3, machine, 1)            # type, machine, ver
    data += struct.pack('<QQQ', 0, phoff if phnum else 0, shoff)
    data += struct.pack('<I', 0)
    data += struct.pack('<HHHHHH', ehsize, phentsize, phnum,
                        shentsize, count, count - 1)
    assert len(data) == ehsize

    if phnum:
        # PT_LOAD covering the whole file, identity-mapped, then PT_DYNAMIC.
        data += struct.pack('<IIQQQQQQ', 1, 4, 0, 0, 0, shoff, shoff, 0x1000)
        data += struct.pack('<IIQQQQQQ', 2, 6, dyn_off, dyn_off, dyn_off,
                            len(dynamic), len(dynamic), 8)
    assert len(data) == body_off

    data += strings
    data += names
    data += dynamic
    assert len(data) == shoff

    for index, offset in enumerate(offsets):
        data += struct.pack('<II', offset, 3 if index == count - 1 else 1)
        data += struct.pack('<QQQQ', 0, 0, 0, 0)
        data += struct.pack('<IIQQ', 0, 0, 1, 0)
    # The string table's own offset and size, patched into its header.
    last = shoff + (count - 1) * shentsize
    data[last + 0x18:last + 0x20] = struct.pack('<Q', names_off)
    data[last + 0x20:last + 0x28] = struct.pack('<Q', len(names))
    return bytes(data)


class ToolchainTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='toolchain-test-')
        self.source = os.path.join(self.root, 'clang')
        self.build_source()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relative, data):
        path = os.path.join(self.source, relative)
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'wb') as handle:
            handle.write(data)
        return path

    def build_source(self, version='7.0.0', targets=('aarch64-fuchsia',)):
        host = elf(export_toolchain.EM_X86_64, needed=['libxml2.so.2',
                                                       'libc.so.6'])
        for driver, aliases in export_toolchain.DRIVERS:
            self.write(driver, host)
            for alias in aliases:
                self.write(alias, host)
        for extra in ('bin/clang-tidy', 'bin/opt', 'bin/wasm-ld'):
            self.write(extra, host)

        self.write('include/c++/v1/string', b'// libc++\n')
        self.write('lib/clang/%s/include/stddef.h' % version, b'/* h */\n')
        target_elf = elf(export_toolchain.EM_AARCH64)
        for target in targets:
            self.write('lib/%s/lib/libc++.so.2' % target, target_elf)
            self.write('lib/%s/lib/asan/libc++.so.2' % target, target_elf)
        runtime = 'lib/clang/%s/lib/fuchsia' % version
        for target in targets:
            arch = target.split('-')[0]
            self.write('%s/libclang_rt.builtins-%s.a' % (runtime, arch),
                       b'!<arch>\n')
            self.write('%s/libclang_rt.asan-%s.a' % (runtime, arch),
                       b'!<arch>\n')
        self.write('%s/libclang_rt.builtins-x86_64.a' % runtime, b'!<arch>\n')
        with open(self.source + '.stamp', 'wb') as handle:
            handle.write(b'deadbeef\n')

    def export(self, **kwargs):
        version, targets = export_toolchain.discover(self.source)
        target = kwargs.pop('target', 'aarch64-fuchsia')
        files, links = export_toolchain.plan(self.source, version, target,
                                            **kwargs)
        dest = os.path.join(self.root, 'out')
        export_toolchain.copy(self.source, dest, files, links)
        return dest, files, links, version, targets

    # -- discovery ---------------------------------------------------------

    def test_discovers_the_release_and_the_targets(self):
        version, targets = export_toolchain.discover(self.source)
        self.assertEqual('7.0.0', version)
        self.assertEqual(['aarch64-fuchsia'], targets)

    def test_two_releases_is_an_error_not_a_choice(self):
        # Picking one would be a guess, and the wrong guess exports a
        # resource directory the driver will not look in.
        os.makedirs(os.path.join(self.source, 'lib', 'clang', '8.0.0'))
        self.assertRaises(export_toolchain.ToolchainError,
                          export_toolchain.discover, self.source)

    def test_a_tree_that_is_not_a_toolchain_is_an_error(self):
        self.assertRaises(export_toolchain.ToolchainError,
                          export_toolchain.discover, self.root)

    def test_reads_the_cipd_stamp(self):
        self.assertEqual('deadbeef', export_toolchain.stamp_of(self.source))

    # -- the keep set ------------------------------------------------------

    def test_keeps_the_drivers_and_aliases_them(self):
        dest, files, links, _, _ = self.export()
        self.assertIn('bin/clang', files)
        self.assertIn(('bin/clang++', 'clang'), links)
        self.assertTrue(os.path.islink(os.path.join(dest, 'bin/clang++')))
        # The alias is a symlink, not a second copy: in the real prebuilt
        # each of these is 78 MB.
        self.assertEqual('clang',
                         os.readlink(os.path.join(dest, 'bin/clang++')))

    def test_drops_the_tools_no_ddk_build_runs(self):
        _, files, _, _, _ = self.export()
        for dropped in ('bin/clang-tidy', 'bin/opt', 'bin/wasm-ld'):
            self.assertNotIn(dropped, files)

    def test_a_missing_driver_is_an_error(self):
        os.remove(os.path.join(self.source, 'bin', 'ld.lld'))
        # clang forks ld.lld by name from beside itself; an export without it
        # compiles everything and links nothing.
        self.assertRaises(export_toolchain.ToolchainError, self.export)

    def test_keeps_only_the_declared_target(self):
        shutil.rmtree(self.source)
        self.build_source(targets=('aarch64-fuchsia', 'x86_64-fuchsia'))
        _, files, _, _, targets = self.export()
        self.assertEqual(['aarch64-fuchsia', 'x86_64-fuchsia'], targets)
        self.assertTrue(any(f.startswith('lib/aarch64-fuchsia') for f in files))
        self.assertFalse(any(f.startswith('lib/x86_64-fuchsia')
                             for f in files))

    def test_keeps_only_the_target_runtime(self):
        _, files, _, _, _ = self.export()
        runtime = [f for f in files if 'libclang_rt' in f]
        self.assertEqual(
            ['lib/clang/7.0.0/lib/fuchsia/libclang_rt.builtins-aarch64.a'],
            runtime)

    def test_sanitizers_are_left_out_by_default(self):
        _, files, _, _, _ = self.export()
        self.assertFalse(any('asan' in f for f in files))

    def test_sanitizers_can_be_asked_for(self):
        _, files, _, _, _ = self.export(sanitizers=True)
        self.assertTrue(any('lib/aarch64-fuchsia/lib/asan' in f
                            for f in files))
        self.assertTrue(any('libclang_rt.asan-aarch64' in f for f in files))

    def test_a_runtime_filter_that_matches_nothing_is_an_error(self):
        runtime = os.path.join(self.source, 'lib', 'clang', '7.0.0', 'lib',
                               'fuchsia')
        for name in os.listdir(runtime):
            if 'aarch64' in name:
                os.remove(os.path.join(runtime, name))
        # Silently exporting no builtins is the failure that only shows up
        # when a consumer's build needs __aarch64_ldadd8_acq_rel.
        self.assertRaises(export_toolchain.ToolchainError, self.export)

    def test_copy_is_a_clean_replacement(self):
        dest, _, _, _, _ = self.export()
        stale = os.path.join(dest, 'bin', 'leftover')
        with open(stale, 'wb') as handle:
            handle.write(b'x')
        self.export()
        self.assertFalse(os.path.exists(stale))

    # -- the libraries the host tools need ---------------------------------

    def pinned(self, *names):
        """A stand-in for the sysroot buildtools ships."""
        directory = os.path.join(self.root, 'sysroot')
        if not os.path.isdir(directory):
            os.makedirs(directory)
        for name in names:
            with open(os.path.join(directory, name), 'wb') as handle:
                handle.write(elf(export_toolchain.EM_X86_64))
        return directory

    def test_a_needed_library_is_carried_along(self):
        # ld.lld wants libxml2.so.2, and a machine whose libxml2 has moved on
        # cannot start it at all.  Two hundred files compile before the first
        # link says so.
        dest, _, _, _, _ = self.export()
        shipped = export_toolchain.collect_host_libs(
            dest, [self.pinned('libxml2.so.2')])
        self.assertEqual(['libxml2.so.2'], sorted(shipped))
        self.assertTrue(os.path.isfile(os.path.join(
            dest, export_toolchain.HOST_LIB_DIR, 'libxml2.so.2')))

    def test_glibc_is_not_carried_along(self):
        # It has to match the loader already running; shipping it would be
        # shipping a second C library.
        dest, _, _, _, _ = self.export()
        shipped = export_toolchain.collect_host_libs(
            dest, [self.pinned('libxml2.so.2', 'libc.so.6')])
        self.assertNotIn('libc.so.6', shipped)

    def test_a_library_missing_from_the_pinned_tree_is_an_error(self):
        # Falling back to this machine is what --host-libs exists to prevent.
        dest, _, _, _, _ = self.export()
        self.assertRaises(export_toolchain.ToolchainError,
                          export_toolchain.collect_host_libs,
                          dest, [self.pinned()])

    def test_no_pinned_tree_at_all_is_an_error(self):
        dest, _, _, _, _ = self.export()
        self.assertRaises(export_toolchain.ToolchainError,
                          export_toolchain.collect_host_libs, dest, [])

    def test_what_a_library_itself_needs_is_carried_too(self):
        dest, _, _, _, _ = self.export()
        directory = os.path.join(self.root, 'sysroot')
        os.makedirs(directory)
        with open(os.path.join(directory, 'libxml2.so.2'), 'wb') as handle:
            handle.write(elf(export_toolchain.EM_X86_64,
                             needed=['liblzma.so.5', 'libc.so.6']))
        with open(os.path.join(directory, 'liblzma.so.5'), 'wb') as handle:
            handle.write(elf(export_toolchain.EM_X86_64))
        shipped = export_toolchain.collect_host_libs(dest, [directory])
        self.assertEqual(['liblzma.so.5', 'libxml2.so.2'], sorted(shipped))

    def test_what_is_carried_stops_being_a_requirement(self):
        # Listing it as one is how a customer was left to find libxml2.so.2
        # at the first link.
        dest, _, _, version, _ = self.export()
        export_toolchain.collect_host_libs(dest, [self.pinned('libxml2.so.2')])
        info = export_toolchain.describe(dest, self.source, version,
                                        'aarch64-fuchsia', 'deadbeef')
        self.assertNotIn('libxml2.so.2', info['host_requirements'])
        self.assertIn('libc.so.6', info['host_requirements'])

    # -- archives ----------------------------------------------------------

    def test_an_archive_reports_its_members_architecture(self):
        # Reading only the ELF magic here shipped two archives with DWARF
        # still in them: neither stripped nor checked, while the report said
        # eight binaries stripped and meant eight of ten.
        path = self.write('probe.a',
                          archive([elf(export_toolchain.EM_AARCH64)]))
        self.assertEqual(export_toolchain.EM_AARCH64,
                         export_toolchain.elf_machine(path))

    def test_an_archive_of_nothing_has_no_architecture(self):
        path = self.write('empty.a', archive([b'not an object']))
        self.assertIsNone(export_toolchain.elf_machine(path))

    def test_an_archive_with_debug_information_is_caught(self):
        dest, _, _, _, _ = self.export()
        with open(os.path.join(dest, 'lib/aarch64-fuchsia/lib/probe.a'),
                  'wb') as handle:
            handle.write(archive([elf(export_toolchain.EM_AARCH64,
                                      ['.debug_info'])]))
        offenders = export_toolchain.check_stripped(dest)
        self.assertIn('lib/aarch64-fuchsia/lib/probe.a',
                      [name for name, _ in offenders])

    def test_an_archive_is_offered_to_the_stripper(self):
        dest, _, _, _, _ = self.export()
        with open(os.path.join(dest, 'lib/aarch64-fuchsia/lib/probe.a'),
                  'wb') as handle:
            handle.write(archive([elf(export_toolchain.EM_AARCH64)]))
        script = os.path.join(self.root, 'fake-objcopy')
        with open(script, 'w') as handle:
            handle.write('#!/bin/sh\nexit 0\n')
        os.chmod(script, 0o755)
        result = export_toolchain.strip_target_binaries(dest, script)
        self.assertIn('lib/aarch64-fuchsia/lib/probe.a', result['files'])

    # -- ELF reading -------------------------------------------------------

    def test_reads_the_machine(self):
        path = self.write('probe.elf', elf(export_toolchain.EM_AARCH64))
        self.assertEqual(export_toolchain.EM_AARCH64,
                         export_toolchain.elf_machine(path))

    def test_a_non_elf_has_no_machine(self):
        path = self.write('probe.txt', b'not an elf at all')
        self.assertIsNone(export_toolchain.elf_machine(path))

    def test_reads_the_libraries_a_host_binary_needs(self):
        # ld.lld wants libxml2.so.2 and dies on a host that ships .so.16.
        # Recording it is the whole reason this reader exists.
        path = os.path.join(self.source, 'bin', 'ld.lld')
        self.assertEqual(['libc.so.6', 'libxml2.so.2'],
                         export_toolchain.needed_libraries(path))

    # -- the gates ---------------------------------------------------------

    def test_debug_information_anywhere_is_a_failure(self):
        dest, _, _, _, _ = self.export()
        self.write('bin/clang', elf(export_toolchain.EM_X86_64,
                                    ['.debug_info']))
        shutil.rmtree(dest)
        self.export()
        offenders = export_toolchain.check_stripped(dest)
        self.assertEqual(['bin/clang'], [name for name, _ in offenders])

    def test_a_clean_export_passes_both_gates(self):
        dest, _, _, _, _ = self.export()
        self.assertEqual([], export_toolchain.check_stripped(dest))
        self.assertEqual([], export_toolchain.check_redacted(dest))

    def test_builtin_header_names_are_allowed(self):
        dest, _, _, _, _ = self.export()
        with open(os.path.join(dest, 'bin', 'clang'), 'ab') as handle:
            handle.write(b'\x00sys/types.h\x00')
        self.assertEqual([], export_toolchain.check_redacted(dest))

    def test_a_real_source_path_is_not_allowed(self):
        dest, _, _, _, _ = self.export()
        with open(os.path.join(dest, 'bin', 'clang'), 'ab') as handle:
            handle.write(b'\x00garnet/lib/machina/virtio_rpmb.cc\x00')
        offenders = export_toolchain.check_redacted(dest)
        self.assertEqual(['bin/clang'], [name for name, _ in offenders])

    def test_the_allow_list_is_exact_not_a_suffix(self):
        dest, _, _, _, _ = self.export()
        with open(os.path.join(dest, 'bin', 'clang'), 'ab') as handle:
            handle.write(b'\x00zircon/system/private/sys/types.h\x00')
        offenders = export_toolchain.check_redacted(dest)
        self.assertEqual(['bin/clang'], [name for name, _ in offenders])

    def test_strip_refuses_a_stripper_that_is_not_there(self):
        dest, _, _, _, _ = self.export()
        self.assertRaises(export_toolchain.ToolchainError,
                          export_toolchain.strip_target_binaries,
                          dest, os.path.join(self.root, 'no-such-objcopy'))

    def test_strip_leaves_host_binaries_to_the_gate(self):
        # The pinned objcopy is aarch64-only, so handing it an x86-64 binary
        # is not something the release does; check_stripped is what covers
        # them instead.
        dest, _, _, _, _ = self.export()
        script = os.path.join(self.root, 'fake-objcopy')
        with open(script, 'w') as handle:
            handle.write('#!/bin/sh\nexit 0\n')
        os.chmod(script, 0o755)
        result = export_toolchain.strip_target_binaries(dest, script)
        self.assertFalse(any(name.startswith('bin/')
                             for name in result['files']))
        self.assertTrue(any('aarch64' in name for name in result['files']))

    # -- what the export says about itself ---------------------------------

    def test_the_source_path_is_relative_to_the_checkout(self):
        # An absolute path here would ship the host layout in the metadata,
        # beside binaries the trim spent a pass cutting exactly that out of.
        dest, _, _, version, _ = self.export()
        info = export_toolchain.describe(dest, self.source, version,
                                        'aarch64-fuchsia', 'deadbeef',
                                        source_root=self.root)
        self.assertEqual('clang', info['source'])

    def test_a_toolchain_outside_the_checkout_is_not_named(self):
        dest, _, _, version, _ = self.export()
        info = export_toolchain.describe(dest, self.source, version,
                                        'aarch64-fuchsia', 'deadbeef',
                                        source_root=os.path.join(
                                            self.root, 'elsewhere', 'deeper'))
        self.assertEqual('(external)', info['source'])

    def test_describe_records_the_stamp_and_the_host_requirements(self):
        dest, _, _, version, _ = self.export()
        info = export_toolchain.describe(dest, self.source, version,
                                        'aarch64-fuchsia', 'deadbeef')
        self.assertEqual('deadbeef', info['prebuilt_stamp'])
        self.assertEqual('7.0.0', info['clang_release'])
        self.assertEqual('aarch64-fuchsia', info['target'])
        self.assertIn('libxml2.so.2', info['host_requirements'])

    def test_the_fingerprint_follows_the_contents(self):
        dest, _, _, version, _ = self.export()
        first = export_toolchain.describe(dest, self.source, version,
                                          'aarch64-fuchsia', 'deadbeef')
        with open(os.path.join(dest, 'include/c++/v1/string'), 'ab') as handle:
            handle.write(b'// changed\n')
        second = export_toolchain.describe(dest, self.source, version,
                                           'aarch64-fuchsia', 'deadbeef')
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])

    def test_the_fingerprint_ignores_its_own_record(self):
        dest, _, _, version, _ = self.export()
        first = export_toolchain.describe(dest, self.source, version,
                                          'aarch64-fuchsia', 'deadbeef')
        export_toolchain.write_info(dest, first)
        second = export_toolchain.describe(dest, self.source, version,
                                           'aarch64-fuchsia', 'deadbeef')
        self.assertEqual(first['fingerprint'], second['fingerprint'])

    def test_the_record_is_json_and_reproducible(self):
        dest, _, _, version, _ = self.export()
        info = export_toolchain.describe(dest, self.source, version,
                                        'aarch64-fuchsia', 'deadbeef',
                                        source_date=1757000000)
        path = export_toolchain.write_info(dest, info)
        with open(path, 'rb') as handle:
            first = handle.read()
        export_toolchain.write_info(dest, info)
        with open(path, 'rb') as handle:
            self.assertEqual(first, handle.read())
        self.assertEqual(1757000000,
                         json.loads(first.decode('utf-8'))['source_date'])


if __name__ == '__main__':
    unittest.main()

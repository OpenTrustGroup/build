#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for assemble_selftest.py and check_hermetic.py."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import assemble_selftest
import partition
import check_hermetic


class Args(object):
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class SandboxTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='ddk-selftest-test-')
        self.product = os.path.join(self.root, 'grpower/workspace/nebula')
        self.out_root = os.path.join(self.product, 'out/venus-hee/arm64')
        self.libddk = os.path.join(self.out_root, 'ddk/libddk')
        self.sources = os.path.join(self.out_root, 'ddk/selftest_src')
        self.toolchain = os.path.join(self.product, 'zircon/prebuilt/clang')
        self.sandbox = os.path.join(self.root, 'sealed/ddk-selftest')
        self.makefile = os.path.join(DDK_DIR, 'selftest.mk')

        self.write(self.libddk, 'include/ddk/driver.h', '#pragma once\n')
        self.write(self.libddk, 'lib/libddk.a', 'ar\n')
        self.write(self.libddk, 'sysroot/include/stdatomic.h', '\n')
        self.write(self.sources, 'garnet/drivers/i2c/mt8678_i2c/i2c.c',
                   'int main(void);\n')
        # Beside the sources it describes, so a partition by path carries
        # it to the same half as the code.
        self.write(self.sources,
                   'garnet/drivers/i2c/mt8678_i2c/mt8678_i2c.ddk.mk',
                   'MODULE_SRCS := garnet/drivers/i2c/mt8678_i2c/i2c.c\n')
        self.write(self.toolchain, 'bin/clang', '#!/bin/sh\n')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, root, relative, content):
        path = os.path.join(root, relative)
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'w') as handle:
            handle.write(content)
        return path

    def args(self, out=None):
        return Args(libddk=self.libddk, sources=['src=' + self.sources],
                    rename_rules=None,
                    toolchain=self.toolchain, makefile=self.makefile,
                    out=out or self.sandbox, source_root=self.product,
                    out_root=self.out_root)

    def rules(self, rename=None):
        """A rules file on disk, with or without a rename block."""
        rules = {'format_version': partition.FORMAT_VERSION,
                 'parts': {'src': 'the half'},
                 'default_part': 'src',
                 'ignore': [],
                 'rules': [{'prefix': 'garnet/drivers', 'part': 'src',
                            'reason': 'all of it'}]}
        if rename is not None:
            rules['rename'] = rename
        path = os.path.join(self.root, 'rules.json')
        with open(path, 'w') as handle:
            json.dump(rules, handle)
        return path

    def test_rules_with_no_rename_assemble_the_tree_as_it_is(self):
        # The delivery renames nothing today, and the sandbox has to be laid
        # out the way the delivery is. Refusing to assemble without a rename
        # would make turning one back on a change here as well as in the
        # rules.
        args = self.args()
        args.rename_rules = self.rules()
        report = assemble_selftest.assemble(args)
        self.assertTrue(os.path.isdir(
            os.path.join(self.sandbox, 'src', 'garnet', 'drivers')))
        self.assertEqual(report['modules'], ['mt8678_i2c'])

    def test_rules_with_a_rename_apply_it(self):
        args = self.args()
        args.rename_rules = self.rules(
            {'from': 'garnet', 'to': 'renamed', 'reason': 'because'})
        assemble_selftest.assemble(args)
        self.assertTrue(os.path.isdir(
            os.path.join(self.sandbox, 'src', 'renamed', 'drivers')))
        self.assertFalse(os.path.exists(
            os.path.join(self.sandbox, 'src', 'garnet')))

    # -- isolation -----------------------------------------------------------

    def test_sandbox_inside_the_product_tree_is_refused(self):
        inside = os.path.join(self.product, 'out/ddk-selftest')
        try:
            assemble_selftest.assemble(self.args(out=inside))
            self.fail('expected an isolation failure')
        except assemble_selftest.AssembleError as error:
            self.assertIn('outside the', str(error))

    def test_sandbox_containing_the_product_tree_is_refused(self):
        self.assertRaises(assemble_selftest.AssembleError,
                          assemble_selftest.assemble,
                          self.args(out=self.root))

    def test_sandbox_beside_the_product_tree_is_allowed(self):
        report = assemble_selftest.assemble(self.args())
        self.assertTrue(os.path.isdir(os.path.join(self.sandbox, 'libddk')))
        self.assertTrue(os.path.isdir(os.path.join(self.sandbox, 'src')))
        self.assertEqual(report['modules'], ['mt8678_i2c'])

    def test_product_tree_is_not_reachable_from_the_sandbox(self):
        assemble_selftest.assemble(self.args())
        for current, dirs, files in os.walk(self.sandbox):
            for name in list(dirs) + files:
                path = os.path.join(current, name)
                if os.path.islink(path):
                    target = os.path.realpath(path)
                    # The toolchain is the single permitted outside input.
                    self.assertTrue(
                        target.startswith(os.path.realpath(self.toolchain)),
                        'unexpected escape: %s -> %s' % (path, target))

    def test_the_toolchain_is_not_linked_in(self):
        """The compiler ships inside libddk; a link here is a fourth thing in
        a layout that has three, and it appears in no delivery."""
        assemble_selftest.assemble(self.args())
        self.assertFalse(os.path.lexists(os.path.join(self.sandbox,
                                                      'toolchain')))

    def test_a_half_lands_under_its_own_name(self):
        # grt_ddk beside mtk_ddk is what MTK receives; mtk_ddk alone is what
        # reaches a customer.  The Makefile reads which from the directories.
        result = assemble_selftest.assemble(self.args())
        self.assertEqual(['src'], result['roots'])
        self.assertTrue(os.path.isdir(os.path.join(self.sandbox, 'src')))

    def test_only_declared_sources_reach_the_sandbox(self):
        # A GRT-private library that MTK never receives; if the sandbox held a
        # full Garnet copy this would compile locally and hide the gap.
        self.write(self.product, 'garnet/public/lib/blk_crypto/blk_crypto.c',
                   'int f(void) { return 0; }\n')
        assemble_selftest.assemble(self.args())
        for current, _, files in os.walk(os.path.join(self.sandbox, 'src')):
            for name in files:
                self.assertNotEqual(name, 'blk_crypto.c',
                                    'undeclared source entered the sandbox')

    def test_every_declared_module_is_discovered(self):
        self.write(self.sources, 'garnet/drivers/spi/mt8678_spi/spi.c', '\n')
        self.write(self.sources,
                   'garnet/drivers/spi/mt8678_spi/mt8678_spi.ddk.mk',
                   'MODULE_SRCS := garnet/drivers/spi/mt8678_spi/spi.c\n')
        report = assemble_selftest.assemble(self.args())
        self.assertEqual(report['modules'], ['mt8678_i2c', 'mt8678_spi'])

    def test_sources_keep_their_source_tree_path(self):
        # The halves of a split must merge by union, so a file may not be
        # relocated on the way in.
        assemble_selftest.assemble(self.args())
        self.assertTrue(os.path.isfile(os.path.join(
            self.sandbox, 'src/garnet/drivers/i2c/mt8678_i2c/i2c.c')))

    def test_missing_module_description_is_an_error(self):
        os.remove(os.path.join(
            self.sources,
            'garnet/drivers/i2c/mt8678_i2c/mt8678_i2c.ddk.mk'))
        self.assertRaises(assemble_selftest.AssembleError,
                          assemble_selftest.assemble, self.args())

    def test_two_descriptions_with_one_name_is_an_error(self):
        # The module is named by its description, not by its directory, so two
        # claiming one name would have one silently replace the other.
        self.write(self.sources, 'garnet/lib/other/mt8678_i2c.ddk.mk',
                   'MODULE_SRCS :=\n')
        with self.assertRaises(assemble_selftest.AssembleError) as caught:
            assemble_selftest.assemble(self.args())
        self.assertIn('claim the name mt8678_i2c', str(caught.exception))

    def test_empty_libddk_is_an_error(self):
        shutil.rmtree(self.libddk)
        os.makedirs(self.libddk)
        self.assertRaises(assemble_selftest.AssembleError,
                          assemble_selftest.assemble, self.args())


class MakefileTestCase(unittest.TestCase):
    """The Makefile a consumer receives, which ships inside the half."""

    def setUp(self):
        with open(os.path.join(DDK_DIR, 'selftest.mk')) as handle:
            self.text = handle.read()

    def test_a_bare_make_builds_rather_than_lists(self):
        # `all` has to be declared last, which leaves `list` as the first
        # rule.  Without a default goal a bare `make` prints module names,
        # builds nothing and exits 0, which reads exactly like a build.
        self.assertIn('.DEFAULT_GOAL := all', self.text)
        goal = self.text.index('.DEFAULT_GOAL := all')
        self.assertLess(goal, self.text.index('\nlist:'))

    def two_halves(self, root):
        """A delivery as it ships: a component, and halves that carry the
        build that makes them."""
        for half, module in (('grt_ddk', 'a'), ('mtk_ddk', 'b')):
            os.makedirs(os.path.join(root, half, 'src/drivers'))
            with open(os.path.join(root, half,
                                   'src/drivers/%s.ddk.mk' % module),
                      'w') as handle:
                handle.write('MODULE_SRCS := src/drivers/%s.c\n' % module)
            with open(os.path.join(root, half, 'src/drivers/%s.c' % module),
                      'w') as handle:
                handle.write('int %s;\n' % module)
            shutil.copyfile(os.path.join(DDK_DIR, 'selftest.mk'),
                            os.path.join(root, half, 'Makefile'))
        os.makedirs(os.path.join(root, 'libddk'))

    def make(self, root, half, arguments):
        """make, run inside a half, the way a consumer runs it."""
        return subprocess.check_output(
            ['make'] + list(arguments), cwd=os.path.join(root, half),
            stderr=subprocess.STDOUT).decode('utf-8')

    def resolved_toolchain(self, root, half='mtk_ddk', extra=()):
        """What TOOLCHAIN expands to for a half laid out on disk."""
        with open(os.path.join(root, half, 'probe.mk'), 'w') as handle:
            handle.write('show:\n\t@echo $(TOOLCHAIN)\n')
        out = self.make(root, half, ['-s', '-f', 'Makefile', '-f', 'probe.mk',
                                     'show'] + list(extra))
        return out.strip().splitlines()[-1]

    def test_the_component_carries_the_compiler(self):
        # It ships as libddk/toolchain beside the half, so a consumer has one
        # thing to sync and cannot end up with a component and a compiler out
        # of step.
        root = tempfile.mkdtemp(prefix='ddk-toolchain-test-')
        try:
            self.two_halves(root)
            self.assertEqual('../libddk/toolchain',
                             self.resolved_toolchain(root))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_separately_supplied_compiler_overrides_it(self):
        # Our own pipeline builds the delivery before it can put the compiler
        # inside it, so it says where the compiler is on make's command line.
        root = tempfile.mkdtemp(prefix='ddk-toolchain-test-')
        try:
            self.two_halves(root)
            self.assertEqual('/elsewhere/clang',
                             self.resolved_toolchain(
                                 root, extra=['TOOLCHAIN=/elsewhere/clang']))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_half_builds_itself_and_not_the_other(self):
        # This is what a customer receives: one half, and the other as the
        # binaries inside libddk.  A half that reached into its neighbour
        # would build here and nowhere else.
        root = tempfile.mkdtemp(prefix='ddk-half-test-')
        try:
            self.two_halves(root)
            text = self.make(root, 'mtk_ddk', ['-n'])
            self.assertIn('src/drivers/b.c', text)
            self.assertNotIn('a.c', text)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_half_can_be_built_by_its_own_name(self):
        # `make mtk_ddk` is the spelling the delivery was built with while
        # this file sat above the halves, and it still works from inside.
        root = tempfile.mkdtemp(prefix='ddk-half-test-')
        try:
            self.two_halves(root)
            self.assertIn('src/drivers/b.c',
                          self.make(root, 'mtk_ddk', ['-n', 'mtk_ddk']))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_list_says_which_half_a_module_is_in(self):
        root = tempfile.mkdtemp(prefix='ddk-half-test-')
        try:
            self.two_halves(root)
            self.assertEqual(['grt_ddk\ta'],
                             self.make(root, 'grt_ddk',
                                       ['-s', 'list']).splitlines())
            self.assertEqual(['mtk_ddk\tb'],
                             self.make(root, 'mtk_ddk',
                                       ['-s', 'list']).splitlines())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def stamped(self, root, **stamps):
        for name, value in stamps.items():
            if value is None:
                continue
            with open(os.path.join(root, name, 'RELEASE'), 'w') as handle:
                handle.write(value + '\n')

    def test_parts_from_one_release_build(self):
        root = tempfile.mkdtemp(prefix='ddk-release-test-')
        try:
            self.two_halves(root)
            self.stamped(root, libddk='r1', grt_ddk='r1', mtk_ddk='r1')
            self.assertEqual(1, len(self.make(root, 'mtk_ddk',
                                              ['-s', 'list']).splitlines()))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_parts_from_different_releases_build_with_a_warning(self):
        # The customer works in mtk_ddk, so its release moves apart from the
        # component's; the ABI and toolchain are what the recipe holds them
        # to.  The mismatch is named, and the half still builds.
        root = tempfile.mkdtemp(prefix='ddk-release-test-')
        try:
            self.two_halves(root)
            self.stamped(root, libddk='old', grt_ddk='r1', mtk_ddk='r1')
            out = self.make(root, 'mtk_ddk', ['-s', 'list'])
            self.assertIn('different releases', out)
            self.assertIn('mtk_ddk\tb', out.splitlines())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_half_with_no_stamp_builds_against_a_stamped_component(self):
        # MTK's half ships as a git repository and is tagged rather than
        # stamped: a RELEASE file there would have to be committed, and
        # `Release <id>` is a message their Gerrit refuses.  So one part
        # having none is the delivered shape, not a mismatch.
        root = tempfile.mkdtemp(prefix='ddk-release-test-')
        try:
            self.two_halves(root)
            self.stamped(root, libddk='r1')
            self.assertEqual(1, len(self.make(root, 'mtk_ddk',
                                              ['-s', 'list']).splitlines()))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_sandbox_with_no_stamp_anywhere_is_fine(self):
        # Ours are assembled, not delivered, and carry none.
        root = tempfile.mkdtemp(prefix='ddk-release-test-')
        try:
            self.two_halves(root)
            self.assertEqual(1, len(self.make(root, 'grt_ddk',
                                              ['-s', 'list']).splitlines()))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_the_abi_flags_survive_a_hostile_environment(self):
        # bitbake exports CFLAGS and LDFLAGS. With ?= they replaced ours
        # outright, dropping --target and compiling for the host; the failure
        # then arrives as a mangling mismatch at link time.
        root = tempfile.mkdtemp(prefix='ddk-flags-test-')
        try:
            self.two_halves(root)
            with open(os.path.join(root, 'mtk_ddk', 'probe.mk'),
                      'w') as handle:
                handle.write('show:\n\t@echo $(CFLAGS)\n')
            environment = dict(os.environ)
            environment['CFLAGS'] = '-O2 --sysroot=/elsewhere'
            out = subprocess.check_output(
                ['make', '-s', '-f', 'Makefile', '-f', 'probe.mk', 'show'],
                cwd=os.path.join(root, 'mtk_ddk'), env=environment,
                stderr=subprocess.STDOUT)
            text = out.decode('utf-8')
            self.assertIn('--target=aarch64-fuchsia', text)
            self.assertNotIn('/elsewhere', text)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_half_with_its_own_sysroot_reaches_it(self):
        # What only one half reads travels inside it, sysroot and all, and
        # --sysroot names one directory: the half's own has to arrive as an
        # include path of its own or the headers it carries are unreachable.
        root = tempfile.mkdtemp(prefix='ddk-sysroot-test-')
        try:
            self.two_halves(root)
            # A half's component as the delivery builds it: headers of its
            # own, and the sysroot headers only this half reads.
            for relative in ('libddk/include/fs/vnode.h',
                             'libddk/sysroot/include/zircon/fidl.h'):
                path = os.path.join(root, 'grt_ddk', relative)
                if not os.path.isdir(os.path.dirname(path)):
                    os.makedirs(os.path.dirname(path))
                with open(path, 'w') as handle:
                    handle.write('\n')
            with open(os.path.join(root, 'grt_ddk', 'probe.mk'),
                      'w') as handle:
                handle.write('show:\n\t@echo [$(EXTRA_SYSINCS)]\n')
            out = self.make(root, 'grt_ddk',
                            ['-s', '-f', 'Makefile', '-f', 'probe.mk', 'show'])
            self.assertIn('-isystem libddk/sysroot/include', out)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_half_without_one_emits_no_bare_isystem(self):
        # MTK's half carries no component of its own.  A bare -isystem would
        # take the flag after it as its argument, the way a bare -I would.
        root = tempfile.mkdtemp(prefix='ddk-sysroot-test-')
        try:
            self.two_halves(root)
            with open(os.path.join(root, 'mtk_ddk', 'probe.mk'),
                      'w') as handle:
                handle.write('show:\n\t@echo [$(EXTRA_SYSINCS)]\n')
            out = self.make(root, 'mtk_ddk',
                            ['-s', '-f', 'Makefile', '-f', 'probe.mk', 'show'])
            self.assertIn('[]', out)
            self.assertNotIn('-isystem', out)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_extra_flags_are_still_a_way_in(self):
        root = tempfile.mkdtemp(prefix='ddk-flags-test-')
        try:
            self.two_halves(root)
            with open(os.path.join(root, 'mtk_ddk', 'probe.mk'),
                      'w') as handle:
                handle.write('show:\n\t@echo [$(EXTRA_CFLAGS)]\n')
            out = self.make(root, 'mtk_ddk',
                            ['-s', '-f', 'Makefile', '-f', 'probe.mk', 'show',
                             'EXTRA_CFLAGS=-O2 -g'])
            self.assertIn('[-O2 -g]', out)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_install_names_where_things_go(self):
        # A recipe's FILES line has to name these, and reading them out of
        # the build beats keeping a second list in step with this one.
        root = tempfile.mkdtemp(prefix='ddk-install-test-')
        try:
            self.two_halves(root)
            self.assertEqual(['/ddk/drivers/b.so'],
                             self.make(root, 'mtk_ddk',
                                       ['-s', 'list-install']).split())
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_half_with_no_module_description_says_so(self):
        root = tempfile.mkdtemp(prefix='ddk-empty-half-test-')
        try:
            os.makedirs(os.path.join(root, 'libddk'))
            os.makedirs(os.path.join(root, 'mtk_ddk'))
            shutil.copyfile(os.path.join(DDK_DIR, 'selftest.mk'),
                            os.path.join(root, 'mtk_ddk', 'Makefile'))
            try:
                self.make(root, 'mtk_ddk', ['-s', 'list'])
                self.fail('a half with nothing to build was accepted')
            except subprocess.CalledProcessError as error:
                self.assertIn('no module description',
                              error.output.decode('utf-8'))
        finally:
            shutil.rmtree(root, ignore_errors=True)


class HermeticTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='ddk-hermetic-test-')
        self.sandbox = os.path.join(self.root, 'sealed')
        self.build = os.path.join(self.sandbox, 'build')
        self.toolchain = os.path.join(self.root, 'clang')
        for relative in ('libddk/include/ddk/driver.h',
                         'libddk/sysroot/include/stdatomic.h',
                         'src/garnet/drivers/i2c/i2c.c'):
            self.write(self.sandbox, relative, '\n')
        self.write(self.toolchain, 'lib/clang/10/include/stddef.h', '\n')
        os.makedirs(self.build)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, root, relative, content):
        path = os.path.join(root, relative)
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'w') as handle:
            handle.write(content)
        return path

    def depfile(self, name, dependencies):
        path = os.path.join(self.build, name)
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, 'w') as handle:
            handle.write('%s.o: \\\n  %s\n'
                         % (name[:-2], ' \\\n  '.join(dependencies)))
        return path

    def test_relative_dependencies_resolve_against_the_compile_dir(self):
        # The compiler writes paths relative to the directory it ran in, which
        # is the sandbox root.  Resolving them against the dependency file's
        # own directory puts them under build/, which is a zone: nothing then
        # escapes and nothing is measured, and the check passes proving
        # nothing.
        self.depfile('i2c/i2c.d', [
            'src/garnet/drivers/i2c/i2c.c',
            'libddk/include/ddk/driver.h',
        ])
        report = check_hermetic.check(self.build, self.sandbox, self.toolchain)
        self.assertEqual(report['src'], ['garnet/drivers/i2c/i2c.c'])
        self.assertEqual(report['libddk'], ['include/ddk/driver.h'])

    def test_a_relative_dependency_that_escapes_is_caught(self):
        self.depfile('i2c/i2c.d', ['../../../elsewhere/host.h'])
        self.assertRaises(check_hermetic.HermeticError, check_hermetic.check,
                          self.build, self.sandbox, self.toolchain)

    def test_dependencies_inside_the_sandbox_pass(self):
        self.depfile('i2c/i2c.d', [
            os.path.join(self.sandbox, 'src/garnet/drivers/i2c/i2c.c'),
            os.path.join(self.sandbox, 'libddk/include/ddk/driver.h'),
            os.path.join(self.sandbox, 'libddk/sysroot/include/stdatomic.h'),
            os.path.join(self.toolchain, 'lib/clang/10/include/stddef.h'),
        ])
        report = check_hermetic.check(self.build, self.sandbox, self.toolchain)
        self.assertEqual(report['depfiles'], 1)
        self.assertIn('include/ddk/driver.h', report['libddk'])
        self.assertIn('garnet/drivers/i2c/i2c.c', report['src'])

    def test_host_header_is_caught(self):
        # The export machine has the dev package installed; the customer does
        # not. Nothing else in the pipeline notices this.
        self.depfile('i2c/i2c.d', [
            os.path.join(self.sandbox, 'src/garnet/drivers/i2c/i2c.c'),
            '/usr/include/openssl/ssl.h',
        ])
        try:
            check_hermetic.check(self.build, self.sandbox, self.toolchain)
            self.fail('expected a hermeticity failure')
        except check_hermetic.HermeticError as error:
            self.assertIn('/usr/include/openssl/ssl.h', str(error))

    def test_product_tree_header_is_caught(self):
        product = self.write(self.root, 'nebula/garnet/public/lib/fxl/log.h',
                             '\n')
        self.depfile('i2c/i2c.d', [
            os.path.join(self.sandbox, 'src/garnet/drivers/i2c/i2c.c'), product])
        self.assertRaises(check_hermetic.HermeticError, check_hermetic.check,
                          self.build, self.sandbox, self.toolchain)

    def test_relative_escape_is_caught(self):
        self.write(self.root, 'outside.h', '\n')
        self.depfile('i2c/i2c.d', ['../../../outside.h'])
        self.assertRaises(check_hermetic.HermeticError, check_hermetic.check,
                          self.build, self.sandbox, self.toolchain)

    def test_phony_rules_from_dash_mp_are_not_dependencies(self):
        # -MP emits "a.h:" per header; reading those rule names back as
        # dependencies puts colon-suffixed junk in the measured closure.
        path = os.path.join(self.build, 'i2c/i2c.d')
        os.makedirs(os.path.dirname(path))
        source = os.path.join(self.sandbox, 'src/garnet/drivers/i2c/i2c.c')
        header = os.path.join(self.sandbox, 'libddk/include/ddk/driver.h')
        with open(path, 'w') as handle:
            handle.write('i2c.o: %s \\\n  %s\n\n%s:\n' %
                         (source, header, header))
        report = check_hermetic.check(self.build, self.sandbox, self.toolchain)
        self.assertEqual(report['libddk'], ['include/ddk/driver.h'])
        self.assertEqual(report['src'], ['garnet/drivers/i2c/i2c.c'])

    def test_missing_depfiles_is_an_error(self):
        try:
            check_hermetic.check(self.build, self.sandbox, self.toolchain)
            self.fail('expected a missing-depfile failure')
        except check_hermetic.HermeticError as error:
            self.assertIn('no dependency files', str(error))

    def test_line_continuations_and_escaped_spaces(self):
        self.depfile('i2c/i2c.d', [
            os.path.join(self.sandbox, 'src/garnet/drivers/i2c/i2c.c'),
            os.path.join(self.sandbox, 'libddk/include/ddk/driver.h'),
        ])
        report = check_hermetic.check(self.build, self.sandbox, self.toolchain)
        self.assertEqual(len(report['src']) + len(report['libddk']), 2)

    def test_measured_closure_is_reported(self):
        self.depfile('i2c/i2c.d', [
            os.path.join(self.sandbox, 'libddk/include/ddk/driver.h')])
        report = check_hermetic.check(self.build, self.sandbox, self.toolchain)
        self.assertEqual(report['libddk'], ['include/ddk/driver.h'])
        payload = json.dumps({'libddk': report['libddk']})
        self.assertIn('include/ddk/driver.h', payload)


if __name__ == '__main__':
    unittest.main()

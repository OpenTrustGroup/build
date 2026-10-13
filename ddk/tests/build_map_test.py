#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for build_map.py.

Every rule here was found by comparing a derived module against one that had
been written by hand, and each test is the smallest build that shows it: the
shape of liblua, of fsl_io reaching into fxl, of two drivers sharing a source
set, of blk_crypto listing a dependency it does not use.  The documents are
what GN and Ninja print, cut down, so the derivation is tested without either.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

DDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(DDK_DIR, 'tools'))

import build_map  # noqa: E402

SHARED = '//build/toolchain/fuchsia:arm64-shared'
DEFAULT = '//build/toolchain/fuchsia:arm64'
BUILDCONFIG = '//build/config/BUILDCONFIG.gn'

RULES = {
    'default_part': 'grt_ddk',
    'rules': [{'prefix': 'drivers/mtk', 'part': 'mtk_ddk'}],
}

# The same rules as a file on disk: the CLI reads them through
# partition.read_rules, which checks the format and the parts.
CLI_RULES = dict(RULES, format_version=1,
                 parts={'grt_ddk': "GRT's half", 'mtk_ddk': "MTK's half"},
                 rules=[dict(entry, reason='MTK maintains it')
                        for entry in RULES['rules']])


class Discarded(object):
    """Somewhere for a command's own printing to go while a test runs.

    Not io.StringIO: on python2 these commands print both str and unicode --
    a %-formatted str on one line, a unicode from print_function on the
    next -- and each of io's buffers refuses one of them.
    """

    def write(self, text):
        pass

    def flush(self):
        pass


def blame(*groups):
    """`gn desc --blame` lines from (from, added_by, values) triples."""
    lines = []
    for source, added_by, values in groups:
        lines.append('From %s' % source)
        if added_by:
            lines.append('     (Added by %s)' % added_by)
        lines.extend('  %s' % v for v in values)
    return lines


class Build(object):
    """A small product build, spelled the way GN and Ninja describe one."""

    def __init__(self):
        self.project = {'build_settings': {'build_dir': '//out/x/',
                                           'default_toolchain': DEFAULT},
                        'targets': {}}
        self.described = {}
        self.deps = {}

    def target(self, label, kind, sources=(), deps=(), libs=(),
               output_name=None, headers=(), **fields):
        compiled = [s for s in sources
                    if s.endswith(build_map.COMPILE_SUFFIXES)]
        outputs = {}
        for source in compiled:
            obj = 'obj/%s.o' % source[2:]
            outputs[source] = [obj]
            self.deps[obj] = {'status': 'VALID',
                              'files': [source[2:]] + list(headers)}
        self.project['targets'][label] = {
            'type': kind, 'sources': list(sources), 'deps': list(deps),
            'libs': list(libs), 'source_outputs': outputs,
            'output_name': output_name or build_map.label_name(label),
        }
        self.described[label] = dict(
            (f, fields.get(f, [])) for f in build_map.ATTRIBUTED)
        return self

    def node(self, label, kind, deps=(), libs=()):
        """Something that is only wiring: a group, a copy, an action."""
        self.project['targets'][label] = {'type': kind, 'deps': list(deps),
                                          'libs': list(libs)}
        return self

    def map(self):
        return build_map.build_map(self.project, self.described, self.deps)


class ReadingTest(unittest.TestCase):

    def test_blame_keeps_the_config_and_who_added_it(self):
        groups = build_map.parse_blame(blame(
            ('//d:drv', None, ['MT=1']),
            ('//build/config:compiler', BUILDCONFIG + ':151', ['-g'])))
        self.assertEqual({'from': '//d:drv', 'added_by': None,
                          'values': ['MT=1']}, groups[0])
        self.assertEqual(BUILDCONFIG + ':151', groups[1]['added_by'])

    def test_values_before_any_from_are_kept(self):
        groups = build_map.parse_blame(['  -O2', 'From //a:b', '  -g'])
        self.assertEqual([None, '//a:b'], [g['from'] for g in groups])

    def test_ninja_paths_come_back_root_relative(self):
        root = '/src'
        text = ('obj/a.o: #deps 3, deps mtime 1 (VALID)\n'
                '    ../../garnet/a.c\n'
                '    /src/zircon/x.h\n'
                '    /usr/include/stdio.h\n')
        record = build_map.parse_ninja_deps(text, '/src/out/x', root)
        self.assertEqual(['garnet/a.c', 'zircon/x.h', '/usr/include/stdio.h'],
                         record['obj/a.o']['files'])

    def test_the_build_directory_is_not_written_into_the_map(self):
        # Read on one build directory, applied to another.
        self.assertEqual('$build/gen/x.h',
                         build_map.source_path('//out/x/gen/x.h', 'out/x'))
        self.assertEqual('garnet/public',
                         build_map.source_path('//garnet/public/', 'out/x'))

    def test_json_after_what_exec_script_printed(self):
        self.assertEqual({'a': 1},
                         build_map.read_json_output('secmgr key\n{"a": 1}'))


class MapTest(unittest.TestCase):

    def test_only_what_compiles_or_links_is_a_target(self):
        the_map = Build().target('//d:drv', 'loadable_module', ['//d/a.c']) \
            .node('//z:ddk', 'group').map()
        self.assertEqual(['//d:drv'], list(the_map['targets']))
        self.assertIn('//z:ddk', the_map['graph'])

    def test_headers_are_what_the_objects_read(self):
        the_map = Build().target('//d:drv', 'loadable_module', ['//d/a.c'],
                                 headers=['d/a.h', 'zircon/ddk.h']).map()
        self.assertEqual(['d/a.h', 'zircon/ddk.h'],
                         the_map['targets']['//d:drv']['headers'])

    def test_a_source_with_no_dependency_record_is_counted(self):
        build = Build().target('//d:drv', 'loadable_module', ['//d/a.c'])
        build.deps.clear()
        the_map = build.map()
        self.assertEqual(['d/a.c'], the_map['targets']['//d:drv']['unread'])
        self.assertEqual(1, the_map['objects_without_depfile'])

    def test_a_target_gn_desc_did_not_describe_is_refused(self):
        build = Build().target('//d:drv', 'loadable_module', ['//d/a.c'])
        build.described.clear()
        self.assertRaises(build_map.MapError, build.map)


class OriginTest(unittest.TestCase):

    def kind(self, source, added_by, directory='garnet/d'):
        return build_map.origin_kind({'from': source, 'added_by': added_by},
                                     directory)

    def test_the_five_kinds(self):
        self.assertEqual('local', self.kind('//garnet/d:drv', None))
        self.assertEqual('local', self.kind('//garnet/d:warnings',
                                            '//garnet/d/BUILD.gn:4'))
        self.assertEqual('applied', self.kind('//garnet/public:config',
                                              '//garnet/d/BUILD.gn:9'))
        self.assertEqual('default', self.kind('//build/config:compiler',
                                              BUILDCONFIG + ':151'))
        self.assertEqual('closed', self.kind('//zircon/public/lib/ddk:cfg',
                                             '//zircon/public/lib/ddk/BUILD.gn:47'))
        self.assertEqual('inherited', self.kind('//third_party/json:cfg',
                                                '//third_party/json/BUILD.gn:8'))


class DeriveTest(unittest.TestCase):

    def derive(self, build, label, modules, **kwargs):
        the_map = build.map()
        provided = kwargs.pop('provided', {})
        owners, _ = build_map.assign_folds(the_map, modules, provided)
        return build_map.derive_module(the_map, label, modules, RULES,
                                       kwargs.pop('abi', ()),
                                       kwargs.pop('abi_cxx', ()),
                                       provided, owners, **kwargs)

    # -- flags -------------------------------------------------------------

    def test_a_drivers_own_defines_and_directories_and_nothing_else(self):
        build = Build().target(
            '//garnet/drivers/i2c:drv', 'loadable_module',
            ['//garnet/drivers/i2c/i2c.c'], output_name='i2c',
            defines=blame(('//garnet/drivers/i2c:drv', None,
                           ['_ALL_SOURCE=1', 'MT8678_I2C=1']),
                          ('//build/config:debug', BUILDCONFIG + ':1',
                           ['__Nebula__=1'])),
            include_dirs=blame(
                ('//garnet/drivers/i2c:drv', None, ['garnet/soc/include']),
                ('//build/config:default_include_dirs', BUILDCONFIG + ':2',
                 ['', '$build/gen']),
                ('//zircon/public/lib/ddk:ddk_config',
                 '//zircon/public/lib/ddk/BUILD.gn:47',
                 ['zircon/system/ulib/ddk/include'])))
        module = self.derive(build, '//garnet/drivers/i2c:drv',
                             {'//garnet/drivers/i2c:drv': 'i2c'},
                             abi=('-D_ALL_SOURCE=1',))
        self.assertEqual(['-DMT8678_I2C=1'], module['MODULE_CFLAGS'])
        self.assertEqual(['garnet/soc/include'],
                         module['MODULE_INCLUDE_DIRS'])
        self.assertEqual('driver', module['MODULE_TYPE'])

    def test_an_inherited_define_travels_and_an_inherited_warning_does_not(self):
        # rapidjson's public defines decide how its headers compile in the
        # consumer; fdt's public -Wno-sign-compare changes no object.
        build = Build().target(
            '//garnet/bin:bin', 'executable', ['//garnet/bin/main.cc'],
            defines=blame(('//third_party/rapidjson:cfg',
                           '//third_party/rapidjson/BUILD.gn:86',
                           ['RAPIDJSON_HAS_STDSTRING'])),
            cflags=blame(('//third_party/fdt:cfg', '//third_party/fdt/BUILD.gn:26',
                          ['-Wno-sign-compare'])))
        module = self.derive(build, '//garnet/bin:bin', {'//garnet/bin:bin': 'bin'})
        self.assertEqual(['-DRAPIDJSON_HAS_STDSTRING'], module['MODULE_CFLAGS'])

    def test_a_shared_config_applied_here_is_not_this_modules_directory(self):
        build = Build().target(
            '//garnet/lib:lib', 'static_library', ['//garnet/lib/a.cc'],
            include_dirs=blame(('//garnet/public:config',
                                '//garnet/lib/BUILD.gn:9',
                                ['garnet/public', 'peridot/public'])))
        module = self.derive(build, '//garnet/lib:lib', {'//garnet/lib:lib': 'lib'})
        self.assertEqual([], module['MODULE_INCLUDE_DIRS'])

    def test_a_cxx_target_that_removed_no_exceptions_says_so(self):
        # The Makefile gives every C++ module -fno-exceptions; guest took
        # no_exceptions off in its BUILD.gn.
        build = Build().target(
            '//garnet/bin:bin', 'executable', ['//garnet/bin/main.cc'],
            cflags_cc=blame(('//build/config:no_rtti', BUILDCONFIG + ':3',
                             ['-fno-rtti'])))
        module = self.derive(build, '//garnet/bin:bin', {'//garnet/bin:bin': 'bin'},
                             abi_cxx=('-fno-exceptions', '-fno-rtti'))
        self.assertEqual(['-fexceptions'], module['MODULE_CFLAGS'])

    # -- what a module is made of -----------------------------------------

    def test_a_library_made_of_source_sets_names_their_sources(self):
        # liblua has no sources of its own.
        build = Build() \
            .target('//garnet/lua:liblua', 'shared_library',
                    deps=['//garnet/lua:core', '//garnet/lua:lib']) \
            .target('//garnet/lua:core', 'source_set', ['//garnet/lua/lapi.c']) \
            .target('//garnet/lua:lib', 'source_set', ['//garnet/lua/lbase.c'])
        module = self.derive(build, '//garnet/lua:liblua',
                             {'//garnet/lua:liblua': 'lua'})
        self.assertEqual(['garnet/lua/lapi.c', 'garnet/lua/lbase.c'],
                         module['MODULE_SRCS'])

    def test_a_source_set_belongs_to_one_library_and_others_link_it(self):
        # fxl_lib is fxl's.  fsl_io depends on it directly, and folding it
        # there too would put the same symbols in two archives.
        build = Build() \
            .target('//garnet/fxl:fxl', 'shared_library', deps=['//garnet/fxl:fxl_lib']) \
            .target('//garnet/fxl:fxl_lib', 'source_set', ['//garnet/fxl/log.cc']) \
            .target('//garnet/fsl/io:io', 'source_set', ['//garnet/fsl/io/fd.cc'],
                    deps=['//garnet/fxl:fxl_lib'])
        modules = {'//garnet/fxl:fxl': 'fxl', '//garnet/fsl/io:io': 'fsl_io'}
        io_module = self.derive(build, '//garnet/fsl/io:io', modules)
        self.assertEqual(['garnet/fsl/io/fd.cc'], io_module['MODULE_SRCS'])
        self.assertEqual(['fxl'], io_module['MODULE_DEPS'])
        fxl_module = self.derive(build, '//garnet/fxl:fxl', modules)
        self.assertEqual(['garnet/fxl/log.cc'], fxl_module['MODULE_SRCS'])

    def test_two_drivers_sharing_a_source_set_each_compile_it(self):
        # Two .so files: neither links the other.
        build = Build() \
            .target('//garnet/board:board', 'loadable_module', ['//garnet/board/b.c'],
                    deps=['//garnet/board:eint_hw']) \
            .target('//garnet/eint:eint', 'loadable_module', ['//garnet/eint/e.c'],
                    deps=['//garnet/board:eint_hw']) \
            .target('//garnet/board:eint_hw', 'source_set', ['//garnet/board/hw.c'])
        modules = {'//garnet/board:board': 'board', '//garnet/eint:eint': 'eint'}
        for label in modules:
            module = self.derive(build, label, modules)
            self.assertIn('garnet/board/hw.c', module['MODULE_SRCS'])
            self.assertEqual([], module['MODULE_DEPS'])

    def test_a_module_is_a_module_on_every_toolchain(self):
        shared_io = '//garnet/fsl/io:io(%s)' % SHARED
        build = Build() \
            .target('//garnet/drv:drv(%s)' % SHARED, 'loadable_module',
                    ['//garnet/drv/d.c'], deps=[shared_io]) \
            .target(shared_io, 'source_set', ['//garnet/fsl/io/fd.cc']) \
            .target('//garnet/fsl/io:io', 'source_set', ['//garnet/fsl/io/fd.cc'])
        modules = {'//garnet/drv:drv(%s)' % SHARED: 'drv',
                   '//garnet/fsl/io:io': 'fsl_io'}
        keyed = dict((build_map.strip_toolchain(l), n) for l, n in modules.items())
        keyed.update(modules)
        module = self.derive(build, '//garnet/drv:drv(%s)' % SHARED, keyed)
        self.assertEqual(['fsl_io'], module['MODULE_DEPS'])
        self.assertEqual(['garnet/drv/d.c'], module['MODULE_SRCS'])

    # -- what a module links -----------------------------------------------

    def test_an_archive_libddk_provides_is_linked_not_compiled(self):
        build = Build() \
            .target('//garnet/bin:bin', 'executable', ['//garnet/bin/m.c'],
                    deps=['//third_party/fdt:fdt']) \
            .target('//third_party/fdt:fdt', 'source_set', ['//third_party/fdt/fdt.c'])
        module = self.derive(build, '//garnet/bin:bin', {'//garnet/bin:bin': 'bin'},
                             provided={'fdt': 'fdt'})
        self.assertEqual(['garnet/bin/m.c'], module['MODULE_SRCS'])
        self.assertIn('fdt', module['MODULE_LIBS'])

    def test_an_archive_can_be_named_by_its_label(self):
        # libnbl_fwk_hee.a is built from a target called nbl_fwk.
        build = Build() \
            .target('//garnet/bin:bin', 'executable', ['//garnet/bin/m.c'],
                    deps=['//third_party/hee/nbl_fwk:nbl_fwk']) \
            .target('//third_party/hee/nbl_fwk:nbl_fwk', 'source_set',
                    ['//third_party/hee/nbl_fwk/log.c'])
        module = self.derive(build, '//garnet/bin:bin', {'//garnet/bin:bin': 'bin'},
                             provided={'//third_party/hee/nbl_fwk:nbl_fwk': 'nbl_fwk_hee',
                                       'nbl_fwk_hee': 'nbl_fwk_hee'})
        self.assertEqual(['nbl_fwk_hee'], module['MODULE_LIBS'])
        self.assertEqual(['garnet/bin/m.c'], module['MODULE_SRCS'])

    def test_a_library_called_libsomething_links_as_something(self):
        build = Build() \
            .target('//garnet/bin:bin', 'executable', ['//garnet/bin/m.c'],
                    deps=['//third_party/hee:libnbl_nmt_tee']) \
            .target('//third_party/hee:libnbl_nmt_tee', 'static_library',
                    ['//third_party/hee/nmt.c'])
        module = self.derive(build, '//garnet/bin:bin', {'//garnet/bin:bin': 'bin'},
                             provided={'nbl_nmt_tee': 'nbl_nmt_tee'})
        self.assertEqual(['nbl_nmt_tee'], module['MODULE_LIBS'])

    def test_a_zircon_group_names_its_archive_through_its_libs(self):
        build = Build() \
            .target('//garnet/fidl:cpp', 'static_library', ['//garnet/fidl/a.cc'],
                    deps=['//zircon/public/lib/async-default:async-default']) \
            .node('//zircon/public/lib/async-default:async-default', 'group',
                  libs=['//out/x/obj/zircon/libasync.default.so', 'zircon'])
        module = self.derive(build, '//garnet/fidl:cpp', {'//garnet/fidl:cpp': 'fidl_cpp'},
                             provided={'async.default': 'async.default',
                                       'zircon': 'zircon'})
        self.assertEqual(['async.default', 'zircon'], module['MODULE_LIBS'])

    def test_an_archive_brings_what_it_links_but_not_a_module(self):
        # vm_config still needs protobuf_full beside it.  blk_crypto lists
        # fxl and uses none of it; that must not make MTK's driver depend on
        # GRT's source.
        build = Build() \
            .target('//garnet/drivers/mtk/ufs:drv', 'loadable_module',
                    ['//garnet/drivers/mtk/ufs/u.c'],
                    deps=['//garnet/lib/blk_crypto:blk_crypto', '//garnet/proto:vm_config']) \
            .target('//garnet/lib/blk_crypto:blk_crypto', 'static_library',
                    ['//garnet/lib/blk_crypto/c.c'], deps=['//garnet/fxl:fxl']) \
            .target('//garnet/proto:vm_config', 'static_library', ['//garnet/proto/v.cc'],
                    deps=['//third_party/protobuf:protobuf_full']) \
            .target('//third_party/protobuf:protobuf_full', 'static_library',
                    ['//third_party/protobuf/p.cc']) \
            .target('//garnet/fxl:fxl', 'shared_library', ['//garnet/fxl/log.cc'])
        modules = {'//garnet/drivers/mtk/ufs:drv': 'ufs', '//garnet/fxl:fxl': 'fxl'}
        module = self.derive(build, '//garnet/drivers/mtk/ufs:drv', modules,
                             provided={'blk_crypto': 'blk_crypto', 'vm_config': 'vm_config',
                                       'protobuf_full': 'protobuf_full'})
        self.assertEqual([], module['MODULE_DEPS'])
        self.assertEqual(['fxl'], module['through_archive'])
        self.assertEqual(['blk_crypto', 'vm_config', 'protobuf_full'],
                         module['MODULE_LIBS'])
        self.assertEqual('mtk', module['MODULE_OWNER'])

    def test_a_library_with_nothing_to_compile_is_looked_through(self):
        # //garnet/public/lib/fsl bundles the fsl pieces.
        build = Build() \
            .target('//garnet/bin:bin', 'executable', ['//garnet/bin/m.c'],
                    deps=['//garnet/fsl:fsl']) \
            .target('//garnet/fsl:fsl', 'static_library', deps=['//garnet/fsl/io:io']) \
            .target('//garnet/fsl/io:io', 'source_set', ['//garnet/fsl/io/fd.cc'])
        modules = {'//garnet/bin:bin': 'bin', '//garnet/fsl/io:io': 'fsl_io'}
        module = self.derive(build, '//garnet/bin:bin', modules)
        self.assertEqual(['fsl_io'], module['MODULE_DEPS'])
        self.assertNotIn('fsl', module['MODULE_LIBS'])

    def test_a_library_libddk_cannot_provide_is_left_out_and_said(self):
        # The product links librng.a beside libcrypto.so; libddk has crypto
        # as a shared library with rng inside it.
        build = Build().target('//garnet/bin:bin', 'executable', ['//garnet/bin/m.c'],
                               libs=['//out/x/obj/libcrypto.so', 'out/zircon/librng.a'])
        module = self.derive(build, '//garnet/bin:bin', {'//garnet/bin:bin': 'bin'},
                             provided={'crypto': 'crypto'})
        self.assertEqual(['crypto'], module['MODULE_LIBS'])
        self.assertEqual(['rng'], module['unsatisfied'])

    def test_the_name_is_the_policys_and_otherwise_a_suggestion(self):
        build = Build().target('//garnet/public/lib/app/cpp:cpp', 'static_library',
                               ['//garnet/public/lib/app/cpp/a.cc'])
        named = self.derive(build, '//garnet/public/lib/app/cpp:cpp',
                            {'//garnet/public/lib/app/cpp:cpp': 'app_cpp'})
        self.assertEqual('app_cpp', named['MODULE_NAME'])
        entry = build.map()['targets']['//garnet/public/lib/app/cpp:cpp']
        self.assertEqual('cpp', build_map.default_module_name(entry))


POLICY = {
    'deliver': ['//garnet/drivers/*', '//garnet/bin/guest:*',
                '//garnet/lib/littlefs:littlefs'],
    'exclude': ['//garnet/drivers/gpio:gpio_demo'],
    'binary': ['//zircon/*', '//third_party/*',
               '//garnet/public/lib/blk_crypto:*'],
    'source': ['//third_party/littlefs:littlefs'],
}


def released(modules, files=(), half=()):
    """A lock as ddk_lock writes it: modules by name, and libddk's files.

    `half` is what a half carries as a component of its own, which is where
    splitting libddk left most of the archives.
    """
    lock = {'modules': modules, 'files': dict((f, 'x') for f in files)}
    if half:
        lock['half_components'] = {'grt_ddk': dict((f, 'x') for f in half)}
    return lock


class SelectTest(unittest.TestCase):

    def select(self, build, lock=None, symbols=None, nm_symbols=None):
        the_map = build.map()
        if nm_symbols is not None:
            original = build_map.object_symbols
            build_map.object_symbols = \
                lambda m, labels, d, nm, cache: set(nm_symbols.get(
                    build_map.strip_toolchain(labels[0]), []))
            try:
                return build_map.select(the_map, POLICY, lock, symbols,
                                        '/build', 'nm')
            finally:
                build_map.object_symbols = original
        return build_map.select(the_map, POLICY, lock, symbols)

    def test_the_policy_delivers_drivers_and_programs_not_tests(self):
        build = Build() \
            .target('//garnet/drivers/i2c:drv(%s)' % SHARED, 'loadable_module',
                    ['//garnet/drivers/i2c/i.c']) \
            .target('//garnet/drivers/gpio:gpio_demo', 'executable',
                    ['//garnet/drivers/gpio/demo.cc']) \
            .target('//garnet/bin/guest:bin', 'executable', ['//garnet/bin/guest/m.cc']) \
            .target('//garnet/bin/guest:guest_unittests', 'executable',
                    ['//garnet/bin/guest/t.cc'])
        build.project['targets']['//garnet/bin/guest:guest_unittests'][
            'testonly'] = True
        chosen = self.select(build)
        self.assertEqual(['//garnet/bin/guest:bin', '//garnet/drivers/i2c:drv'],
                         chosen['roots'])

    def test_a_directory_pattern_does_not_make_its_libraries_roots(self):
        # //garnet/bin/guest:* delivers guest's programs; guest:lib ships
        # because they link it, not because the pattern matched it.
        build = Build() \
            .target('//garnet/bin/guest:lib', 'source_set', ['//garnet/bin/guest/l.cc'])
        self.assertEqual([], self.select(build)['roots'])

    def test_a_library_named_outright_is_a_root(self):
        build = Build().target('//garnet/lib/littlefs:littlefs', 'static_library',
                               ['//garnet/lib/littlefs/fs.c'])
        self.assertEqual(['//garnet/lib/littlefs:littlefs'],
                         self.select(build)['roots'])

    def test_the_narrower_pattern_wins_between_binary_and_source(self):
        build = Build() \
            .target('//garnet/drivers/x:drv', 'loadable_module', ['//garnet/drivers/x/x.c'],
                    deps=['//third_party/littlefs:littlefs', '//third_party/fdt:fdt']) \
            .target('//third_party/littlefs:littlefs', 'static_library',
                    ['//third_party/littlefs/lfs.c']) \
            .target('//third_party/fdt:fdt', 'source_set', ['//third_party/fdt/fdt.c'])
        chosen = self.select(build)
        self.assertIn('//third_party/fdt:fdt', chosen['archives'])
        self.assertIn('littlefs', chosen['modules'].values())

    def test_generated_code_ships_compiled(self):
        build = Build() \
            .target('//garnet/drivers/x:drv', 'loadable_module', ['//garnet/drivers/x/x.c'],
                    deps=['//garnet/public/lib/app/fidl:fidl']) \
            .target('//garnet/public/lib/app/fidl:fidl', 'static_library',
                    ['//out/x/fidling/gen/component.cc'])
        self.assertIn('//garnet/public/lib/app/fidl:fidl',
                      self.select(build)['archives'])

    def test_a_library_takes_the_source_sets_beside_it_on_any_toolchain(self):
        # fxl is only a shared library on the shared toolchain; fxl_lib's
        # default variant is the one chosen, and it is still fxl's.
        build = Build() \
            .target('//garnet/drivers/x:drv(%s)' % SHARED, 'loadable_module',
                    ['//garnet/drivers/x/x.c'], deps=['//garnet/fxl:fxl(%s)' % SHARED]) \
            .target('//garnet/fxl:fxl(%s)' % SHARED, 'shared_library',
                    deps=['//garnet/fxl:fxl_lib(%s)' % SHARED]) \
            .target('//garnet/fxl:fxl_lib(%s)' % SHARED, 'source_set', ['//garnet/fxl/log.cc']) \
            .target('//garnet/fxl:fxl_lib', 'source_set', ['//garnet/fxl/log.cc'])
        chosen = self.select(build)
        self.assertEqual(['//garnet/fxl:fxl_lib'], chosen['folded'])
        self.assertIn('fxl', chosen['modules'].values())

    def test_a_source_set_two_programs_link_is_a_module_and_one_is_not(self):
        build = Build() \
            .target('//garnet/bin/guest:bin', 'executable', ['//garnet/bin/guest/a.cc'],
                    deps=['//garnet/bin/guest:lib', '//garnet/bin/guest:only_bin']) \
            .target('//garnet/bin/guest:bin_sos', 'executable', ['//garnet/bin/guest/b.cc'],
                    deps=['//garnet/bin/guest:lib']) \
            .target('//garnet/bin/guest:lib', 'source_set', ['//garnet/bin/guest/l.cc']) \
            .target('//garnet/bin/guest:only_bin', 'source_set', ['//garnet/bin/guest/o.cc'])
        chosen = self.select(build)
        self.assertIn('//garnet/bin/guest:lib', chosen['modules'])
        self.assertEqual(['//garnet/bin/guest:only_bin'], chosen['folded'])

    def test_a_source_set_only_drivers_use_is_compiled_into_each(self):
        build = Build() \
            .target('//garnet/drivers/a:drv', 'loadable_module', ['//garnet/drivers/a/a.c'],
                    deps=['//garnet/drivers/board:hw']) \
            .target('//garnet/drivers/b:drv', 'loadable_module', ['//garnet/drivers/b/b.c'],
                    deps=['//garnet/drivers/board:hw']) \
            .target('//garnet/drivers/board:hw', 'source_set', ['//garnet/drivers/board/hw.c'])
        self.assertEqual(['//garnet/drivers/board:hw'], self.select(build)['folded'])

    def test_a_released_module_keeps_its_name_and_type(self):
        # Five GN libraries are called cpp; fxl was released as an archive
        # though the product builds it shared.
        build = Build() \
            .target('//garnet/bin/guest:bin', 'executable', ['//garnet/bin/guest/m.cc'],
                    deps=['//garnet/public/lib/app/cpp:cpp', '//garnet/fxl:fxl']) \
            .target('//garnet/public/lib/app/cpp:cpp', 'static_library',
                    ['//garnet/public/lib/app/cpp/a.cc']) \
            .target('//garnet/fxl:fxl', 'shared_library', ['//garnet/fxl/log.cc'])
        lock = released({
            'app_cpp': {'dir': 'garnet/public/lib/app/cpp', 'type': 'static_library',
                        'half': 'grt_ddk', 'sources': ['garnet/public/lib/app/cpp/a.cc']},
            'fxl': {'dir': 'garnet/fxl', 'type': 'static_library', 'half': 'grt_ddk',
                    'sources': ['garnet/fxl/log.cc']}})
        chosen = self.select(build, lock)
        self.assertEqual('app_cpp', chosen['modules']['//garnet/public/lib/app/cpp:cpp'])
        self.assertEqual({'//garnet/fxl:fxl': 'static_library'}, chosen['types'])

    def test_a_new_module_whose_name_is_taken_is_qualified(self):
        build = Build() \
            .target('//garnet/bin/guest:bin', 'executable', ['//garnet/bin/guest/m.cc'],
                    deps=['//garnet/public/lib/app/cpp:cpp', '//garnet/public/lib/svc/cpp:cpp']) \
            .target('//garnet/public/lib/app/cpp:cpp', 'static_library',
                    ['//garnet/public/lib/app/cpp/a.cc']) \
            .target('//garnet/public/lib/svc/cpp:cpp', 'static_library',
                    ['//garnet/public/lib/svc/cpp/s.cc'])
        names = sorted(self.select(build)['modules'].values())
        # guest:bin too: bin/ is where the Makefile installs programs, so
        # a module of that name is qualified the same way.
        self.assertEqual(['cpp', 'guest_bin', 'svc_cpp'], names)

    def test_a_binary_library_is_named_after_the_released_one_with_its_symbols(self):
        # libnbl_fwk_hee.a is built from nbl_fwk; libcrypto.so also holds
        # libc-trusty's symbols and is not libc-trusty.a.
        build = Build() \
            .target('//garnet/bin/guest:bin', 'executable', ['//garnet/bin/guest/m.cc'],
                    deps=['//third_party/hee/nbl_fwk:nbl_fwk', '//zircon/trusty/libc:libc-trusty']) \
            .target('//third_party/hee/nbl_fwk:nbl_fwk', 'source_set',
                    ['//third_party/hee/nbl_fwk/log.c']) \
            .target('//zircon/trusty/libc:libc-trusty', 'source_set',
                    ['//zircon/trusty/libc/str.c'])
        lock = released({}, ['lib/libnbl_fwk_hee.a', 'lib/libc-trusty.a', 'lib/libcrypto.so'])
        symbols = {'lib/libnbl_fwk_hee.a': ['nbl_log', 'nbl_time'],
                   'lib/libc-trusty.a': ['strlcpy', 'memset_s'],
                   'lib/libcrypto.so': ['strlcpy', 'memset_s'] + ['c%d' % i for i in range(50)]}
        chosen = self.select(build, lock, symbols, nm_symbols={
            '//third_party/hee/nbl_fwk:nbl_fwk': ['nbl_log', 'nbl_time'],
            '//zircon/trusty/libc:libc-trusty': ['strlcpy', 'memset_s']})
        self.assertEqual('nbl_fwk_hee', chosen['archives']['//third_party/hee/nbl_fwk:nbl_fwk'])
        self.assertEqual('c-trusty', chosen['archives']['//zircon/trusty/libc:libc-trusty'])

    def test_a_new_binary_does_not_take_a_released_librarys_name(self):
        # features/fidl:fidl exports none of libfidl.a's symbols, so it is
        # not that library; named plainly it would ship as libfidl.a and
        # displace the one every fidl proxy links.
        build = Build() \
            .target('//garnet/bin/guest:bin', 'executable', ['//garnet/bin/guest/m.cc'],
                    deps=['//garnet/public/lib/features/fidl:fidl']) \
            .target('//garnet/public/lib/features/fidl:fidl', 'source_set',
                    ['$build/fidling/gen/fuchsia/cpp/features.cc'])
        lock = released({}, [], half=['lib/libfidl.a'])
        symbols = {'grt_ddk/libddk/lib/libfidl.a': ['fidl_encode', 'fidl_decode']}
        chosen = self.select(build, lock, symbols, nm_symbols={
            '//garnet/public/lib/features/fidl:fidl': ['features_GetFeatures']})
        self.assertEqual('fidl_features',
                         chosen['archives']['//garnet/public/lib/features/fidl:fidl'])
        self.assertTrue(any('named fidl_features by rule' in n
                            for n in chosen['notes']), chosen['notes'])

    def test_a_binary_whose_objects_define_more_than_the_library_exports(self):
        # boringssl's objects define every internal symbol too; libssl.so
        # exports the visible ones.  Over the union they barely overlap;
        # the library is the one whose exports these objects all define.
        build = Build() \
            .target('//garnet/bin/guest:bin', 'executable', ['//garnet/bin/guest/m.cc'],
                    deps=['//third_party/boringssl:ssl']) \
            .target('//third_party/boringssl:ssl', 'source_set',
                    ['//third_party/boringssl/ssl.c'])
        lock = released({}, [], half=['lib/libssl_boringssl.so'])
        symbols = {'grt_ddk/libddk/lib/libssl_boringssl.so': ['SSL_new', 'SSL_free']}
        chosen = self.select(build, lock, symbols, nm_symbols={
            '//third_party/boringssl:ssl': ['SSL_new', 'SSL_free'] + ['ssl_internal_%d' % i for i in range(6)]})
        self.assertEqual('ssl_boringssl',
                         chosen['archives']['//third_party/boringssl:ssl'])

    def test_a_name_the_makefile_uses_for_a_directory_is_qualified(self):
        build = Build() \
            .target('//garnet/bin/guest:bin', 'executable', ['//garnet/bin/guest/m.cc'],
                    deps=['//garnet/bin/guest:lib', '//garnet/bin/guest:sos']) \
            .target('//garnet/bin/guest:lib', 'static_library',
                    ['//garnet/bin/guest/kernel.cc']) \
            .target('//garnet/bin/guest:sos', 'executable',
                    ['//garnet/bin/guest/sos.cc'], deps=['//garnet/bin/guest:lib'])
        chosen = self.select(build)
        self.assertEqual('guest_lib', chosen['modules']['//garnet/bin/guest:lib'])

    def test_a_library_folded_into_its_neighbour_stays_folded(self):
        # fxl's record holds fxl_logging's sources because fxl_logging was
        # folded into it; fxl is still there to be fxl, so fxl_logging is
        # not a module of its own.
        build = Build() \
            .target('//garnet/bin/guest:bin', 'executable', ['//garnet/bin/guest/m.cc'],
                    deps=['//garnet/public/lib/fxl:fxl']) \
            .target('//garnet/public/lib/fxl:fxl', 'static_library',
                    ['//garnet/public/lib/fxl/fxl.cc', '//garnet/public/lib/fxl/a.cc',
                     '//garnet/public/lib/fxl/b.cc', '//garnet/public/lib/fxl/c.cc'],
                    deps=['//garnet/public/lib/fxl:fxl_logging']) \
            .target('//garnet/public/lib/fxl:fxl_logging', 'shared_library',
                    ['//garnet/public/lib/fxl/logging.cc'])
        lock = released({'fxl': {
            'dir': 'garnet/public/lib/fxl', 'type': 'static_library', 'half': 'grt_ddk',
            'sources': ['garnet/public/lib/fxl/fxl.cc', 'garnet/public/lib/fxl/a.cc',
                        'garnet/public/lib/fxl/b.cc', 'garnet/public/lib/fxl/c.cc',
                        'garnet/public/lib/fxl/logging.cc']}}, [])
        chosen = self.select(build, lock)
        self.assertEqual('fxl', chosen['modules']['//garnet/public/lib/fxl:fxl'])
        self.assertNotIn('//garnet/public/lib/fxl:fxl_logging', chosen['modules'])

    def test_a_module_is_matched_by_the_record_that_holds_all_its_sources(self):
        # The record is the module as shipped, folds included: guest_lib
        # carries callback's files beside guest:lib's own.  Over the union
        # that is a third; the record has all of guest:lib's sources.
        build = Build() \
            .target('//garnet/bin/guest:bin', 'executable', ['//garnet/bin/guest/m.cc'],
                    deps=['//garnet/bin/guest:lib', '//garnet/bin/guest:sos']) \
            .target('//garnet/bin/guest:lib', 'static_library',
                    ['//garnet/bin/guest/kernel.cc', '//garnet/bin/guest/linux.cc']) \
            .target('//garnet/bin/guest:sos', 'executable',
                    ['//garnet/bin/guest/sos.cc'], deps=['//garnet/bin/guest:lib'])
        lock = released({'guest_lib': {
            'dir': 'garnet/bin/guest', 'type': 'static_library', 'half': 'grt_ddk',
            'sources': ['garnet/bin/guest/kernel.cc', 'garnet/bin/guest/linux.cc',
                        'garnet/lib/callback/a.cc', 'garnet/lib/callback/b.cc',
                        'garnet/lib/callback/c.cc', 'garnet/lib/callback/d.cc']}}, [])
        chosen = self.select(build, lock)
        self.assertEqual('guest_lib', chosen['modules']['//garnet/bin/guest:lib'])

    def test_a_binary_named_after_a_library_that_moved_inside_a_half(self):
        # Splitting libddk moved libnbl_fwk_hee.a into grt_ddk's own
        # component.  It is still the released library exporting these
        # symbols, so the name holds; reading the lock's `files` alone
        # renamed it nbl_fwk and no module could link it.
        build = Build() \
            .target('//garnet/bin/guest:bin', 'executable', ['//garnet/bin/guest/m.cc'],
                    deps=['//third_party/hee/nbl_fwk:nbl_fwk']) \
            .target('//third_party/hee/nbl_fwk:nbl_fwk', 'source_set',
                    ['//third_party/hee/nbl_fwk/log.c'])
        lock = released({}, [], half=['lib/libnbl_fwk_hee.a'])
        symbols = {'grt_ddk/libddk/lib/libnbl_fwk_hee.a': ['nbl_log', 'nbl_time']}
        chosen = self.select(build, lock, symbols, nm_symbols={
            '//third_party/hee/nbl_fwk:nbl_fwk': ['nbl_log', 'nbl_time']})
        self.assertEqual('nbl_fwk_hee',
                         chosen['archives']['//third_party/hee/nbl_fwk:nbl_fwk'])


class LockSurfaceTest(unittest.TestCase):

    def test_a_library_inside_a_half_is_still_released(self):
        # The lock records what moved inside grt_ddk under half_components,
        # and keys its symbols by where it lives.
        surface = build_map.lock_surface(
            released({}, ['lib/libddk.a'], half=['lib/libfbl.a']))
        self.assertEqual('lib/libddk.a', surface['lib/libddk.a'])
        self.assertEqual('grt_ddk/libddk/lib/libfbl.a',
                         surface['lib/libfbl.a'])

    def test_a_library_libddk_still_carries_keeps_its_own_key(self):
        surface = build_map.lock_surface(
            released({}, ['lib/libddk.a'], half=['lib/libddk.a']))
        self.assertEqual('lib/libddk.a', surface['lib/libddk.a'])


class ModulesCliTest(unittest.TestCase):
    """`modules` reads the lock itself, so the CLI is what has to be tested.

    A library the lock records inside a half is one the delivery still ships:
    the module that links it must keep it in MODULE_LIBS.  Reading only the
    lock's `files` dropped 34 of them at once and every sealed build failed
    to link.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='build-map-cli-')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_modules(self, lock, archives=None, build=None):
        build = build or Build().target(
            '//garnet/drivers/a:drv', 'loadable_module',
            ['//garnet/drivers/a/a.c'], libs=['fbl'])
        out = os.path.join(self.root, 'modules')
        paths = {}
        for name, document in (('map', build.map()), ('rules', CLI_RULES),
                               ('selection', {'modules': {
                                   '//garnet/drivers/a:drv': 'a'},
                                   'archives': archives or {},
                                   'folded': []}),
                               ('lock', lock)):
            paths[name] = os.path.join(self.root, name + '.json')
            with io.open(paths[name], 'w', encoding='utf-8') as handle:
                handle.write(u'%s' % json.dumps(document))
        argv = sys.argv
        stdout = sys.stdout
        sys.argv = ['build_map.py', 'modules',
                    '--map', paths['map'], '--rules', paths['rules'],
                    '--makefile', os.path.join(DDK_DIR, 'selftest.mk'),
                    '--selection', paths['selection'],
                    '--lock', paths['lock'], '--out', out]
        sys.stdout = Discarded()
        try:
            self.assertEqual(0, build_map.main())
        finally:
            sys.argv, sys.stdout = argv, stdout
        for current, _, names in os.walk(out):
            for name in names:
                if name == 'a.ddk.mk':
                    with io.open(os.path.join(current, name),
                                 encoding='utf-8') as handle:
                        return handle.read()
        self.fail('no module description was written')

    def test_a_library_the_lock_keeps_inside_a_half_is_still_linked(self):
        # libddk stays in `files` so the lock names something: an empty lock
        # provides nothing and every library links, which is not the
        # question being asked here.
        text = self.run_modules(
            released({}, ['lib/libddk.a'], half=['lib/libfbl.a']))
        self.assertIn('fbl', text.split('MODULE_LIBS')[1].split('\n')[0])

    def test_a_library_no_component_carries_is_left_out(self):
        text = self.run_modules(released({}, ['lib/libddk.a']))
        self.assertNotIn('fbl', text.split('MODULE_LIBS')[1].split('\n')[0])

    def test_an_archive_the_selection_ships_is_linked_though_the_lock_is_silent(self):
        # mt8668's features/fidl bindings are new: the lock, seeded from
        # mt8678, knows nothing of them, but the selection ships them and
        # the derivation plans them into lib/, so the module that needs
        # them links them.
        build = Build() \
            .target('//garnet/drivers/a:drv', 'loadable_module',
                    ['//garnet/drivers/a/a.c'],
                    deps=['//garnet/public/lib/features/fidl:fidl']) \
            .target('//garnet/public/lib/features/fidl:fidl', 'source_set',
                    ['$build/fidling/gen/fuchsia/cpp/features.cc'])
        text = self.run_modules(
            released({}, ['lib/libddk.a']),
            archives={'//garnet/public/lib/features/fidl:fidl':
                      'fidl_features'}, build=build)
        self.assertIn('fidl_features',
                      text.split('MODULE_LIBS')[1].split('\n')[0])


class MakefileTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='build-map-test-')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_the_flags_every_module_already_gets(self):
        path = os.path.join(self.root, 'selftest.mk')
        with io.open(path, 'w', encoding='utf-8') as handle:
            handle.write(u'ABI_DEFINES := \\\n\t-D_ALL_SOURCE=1 \\\n'
                         u'\t-DHAVE_PTHREAD\n\n'
                         u'ABI_CXX_FLAGS := \\\n\t-fno-exceptions \\\n'
                         u'\t-fno-rtti\n')
        self.assertEqual(set(['-D_ALL_SOURCE=1', '-DHAVE_PTHREAD']),
                         build_map.read_abi_defines(path))
        self.assertEqual(['-fno-exceptions', '-fno-rtti'],
                         build_map.read_abi_cxx_flags(path))

    def test_the_rendered_description_reads_back(self):
        module = {'MODULE_NAME': 'i2c', 'MODULE_OWNER': 'mtk',
                  'MODULE_TYPE': 'driver', 'MODULE_DEPS': [],
                  'MODULE_SRCS': ['garnet/i2c.c'], 'MODULE_INCLUDE_DIRS': [],
                  'MODULE_LIBS': ['ddk', 'zircon'],
                  'MODULE_CFLAGS': ['-DMT8678_I2C=1']}
        path = os.path.join(self.root, 'i2c.ddk.mk')
        with io.open(path, 'w', encoding='utf-8') as handle:
            handle.write(build_map.render_module(module, '//garnet/i2c:drv'))
        _, fields = build_map.read_module_file(path)
        self.assertEqual('i2c', fields['MODULE_NAME'])
        self.assertEqual(['ddk', 'zircon'], fields['MODULE_LIBS'])

    def test_the_old_archive_declarations_name_their_targets(self):
        project = {'targets': {
            '//garnet/app/fidl:fidl_ddk_archive': {
                'type': 'action',
                'args': ['--file', 'internal:lib/libfidl_component.a=/out/libfidl.a'],
                'deps': ['//garnet/app/fidl:fidl(%s)' % SHARED]}}}
        self.assertEqual({'//garnet/app/fidl:fidl': 'fidl_component'},
                         build_map.declared_archives(project))


if __name__ == '__main__':
    unittest.main()

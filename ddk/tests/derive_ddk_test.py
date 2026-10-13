#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for derive_ddk.py.

Each rule placed a real file wrongly before it was a rule, and each test is a
checkout small enough to show that one file: grt-i2c.h read across the
halves, rapidjson spelled two ways and including its neighbours by quotes, an
include inside a doc comment, an MSVC-only header, a header the sysroot
already answers, a header only a test lists.
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

import derive_ddk  # noqa: E402

SHARED = '//build/toolchain/fuchsia:arm64-shared'
RULES = {'default_part': 'grt_ddk',
         'rules': [{'prefix': 'drivers/mtk', 'part': 'mtk_ddk'}]}

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


def group(source, values, added_by=None):
    return {'from': source, 'added_by': added_by, 'values': list(values)}


class Checkout(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='derive-ddk-test-')
        self.tree = derive_ddk.Tree(self.root, os.path.join(self.root, 'out/x'))
        self.targets = {}
        self.selection = {'modules': {}, 'archives': {}, 'folded': []}
        self.write('out/x/sysroot/include/stdio.h', '')
        self.write('out/x/sysroot/include/module.modulemap', '')
        self.write('out/x/sysroot/lib/libc.so', '')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, relative, text):
        path = os.path.join(self.root, relative)
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with io.open(path, 'w', encoding='utf-8') as handle:
            handle.write(u'%s' % text)

    def module(self, label, name, directory, sources, headers=(),
               include_dirs=(), kind='loadable_module', testonly=False):
        self.targets[label] = {
            'type': kind, 'dir': directory, 'name': label.split(':')[-1],
            'output_name': name, 'sources': list(sources),
            'headers': list(headers), 'deps': [], 'libs': [], 'objects': [],
            'outputs': [], 'testonly': testonly,
            'cflags': [group('//build/config:compiler',
                             ['--sysroot=%s/out/x/sysroot' % self.root])],
            'include_dirs': [group(label, [d]) for d in include_dirs],
        }
        if name:
            self.selection['modules'][label] = name

    def the_map(self):
        return {'targets': self.targets, 'graph': {},
                'default_toolchain': '//build/toolchain/fuchsia:arm64'}

    def component(self, released=(), keeps=()):
        the_map = self.the_map()
        halves = derive_ddk.plan_sources(the_map, self.selection, RULES,
                                         self.tree)
        plan, _ = derive_ddk.plan_component(the_map, self.selection, RULES,
                                            self.tree, halves, keeps,
                                            set(released))
        return plan

    def sources(self, released_halves=None):
        return derive_ddk.plan_sources(self.the_map(), self.selection, RULES,
                                       self.tree, released_halves)


class SysrootTest(Checkout):

    def test_the_sysroot_libraries_whole_and_its_headers_as_read(self):
        # The libraries travel whole -- a consumer may link -lm though we
        # did not -- and a header travels because a half read it.  Taking
        # the headers whole handed GRT's own ABI to both halves.
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'])
        self.write('garnet/drivers/a/a.c', '')
        plan = self.component()
        self.assertIn('sysroot/lib/libc.so', plan)
        self.assertNotIn('sysroot/include/module.modulemap', plan)
        # ar.h is neither read nor one of libc++'s: it stays behind.
        self.write('out/x/sysroot/include/ar.h', '')
        self.assertNotIn('sysroot/include/ar.h', self.component())

    def test_what_libcxx_falls_back_to_travels_though_nothing_read_it(self):
        # <cwchar> reaches past itself with #include_next for the sysroot's
        # wchar.h.  No source and no sysroot header spells that, so only
        # knowing the set carries it -- and a half compiling C++ needs it.
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'])
        self.write('garnet/drivers/a/a.c', '')
        self.write('out/x/sysroot/include/wchar.h', '')
        plan = self.component()
        self.assertIn('sysroot/include/stdio.h', plan)
        self.assertIn('sysroot/include/wchar.h', plan)

    def test_a_sysroot_header_a_half_read_travels(self):
        self.write('garnet/drivers/a/a.c', '#include <stdio.h>\n')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'],
                    headers=['out/x/sysroot/include/stdio.h'])
        self.assertIn('sysroot/include/stdio.h', self.component())

    def test_a_sysroot_header_only_a_half_source_spells_travels(self):
        # The product build never read it, so `readers` does not have it and
        # no sysroot header includes it; the half that compiles the source
        # still needs it.
        self.write('out/x/sysroot/include/zircon/syscalls/exception.h', '')
        self.write('garnet/drivers/a/a.c',
                   '#include <zircon/syscalls/exception.h>\n')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'])
        plan = derive_ddk.plan_component(
            self.the_map(), self.selection, RULES, self.tree,
            ['garnet/drivers/a/a.c'])[0]
        self.assertIn('sysroot/include/zircon/syscalls/exception.h', plan)

    def test_a_sysroot_header_follows_every_branch_it_spells(self):
        # bits/stat.h reads one copy under __x86_64__ and another otherwise.
        # Following only the branch this compiler takes dropped the one
        # aarch64 compiles.
        self.write('out/x/sysroot/include/bits/aarch64/stat.h', '')
        self.write('out/x/sysroot/include/bits/x86_64/stat.h', '')
        self.write('out/x/sysroot/include/bits/stat.h',
                   '#if defined(__x86_64__)\n#include "x86_64/stat.h"\n'
                   '#else\n#include "aarch64/stat.h"\n#endif\n')
        self.write('garnet/drivers/a/a.c', '#include <bits/stat.h>\n')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'],
                    headers=['out/x/sysroot/include/bits/stat.h'])
        plan = self.component()
        self.assertIn('sysroot/include/bits/aarch64/stat.h', plan)
        self.assertIn('sysroot/include/bits/x86_64/stat.h', plan)


class HeaderTest(Checkout):

    def setUp(self):
        Checkout.setUp(self)
        self.write('zircon/ulib/ddk/include/ddk/driver.h', '#pragma once\n')

    def test_a_header_lands_where_the_code_spells_it(self):
        self.write('garnet/drivers/a/a.c', '#include <ddk/driver.h>\n')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'],
                    headers=['zircon/ulib/ddk/include/ddk/driver.h'],
                    include_dirs=['zircon/ulib/ddk/include'])
        plan = self.component()
        self.assertEqual('zircon/ulib/ddk/include/ddk/driver.h',
                         plan['include/ddk/driver.h'])

    def test_every_spelling_the_code_uses(self):
        # <json/doc.h> in one place and "third_party/json/json/doc.h" in
        # another.
        self.write('third_party/json/json/doc.h', '#pragma once\n')
        self.write('garnet/drivers/a/a.c', '#include <json/doc.h>\n')
        self.write('garnet/drivers/b/b.c',
                   '#include "third_party/json/json/doc.h"\n')
        for letter in 'ab':
            self.module('//garnet/drivers/%s:drv' % letter, letter,
                        'garnet/drivers/%s' % letter,
                        ['garnet/drivers/%s/%s.c' % (letter, letter)],
                        headers=['third_party/json/json/doc.h'],
                        include_dirs=['', 'third_party/json'])
        plan = self.component()
        self.assertIn('include/json/doc.h', plan)
        self.assertIn('include/third_party/json/json/doc.h', plan)

    def test_two_spellings_of_one_header_publish_one_file(self):
        # <zx/time.h> in one place and <lib/zx/time.h> in another: the
        # product resolves both to ulib/zx/include/lib/zx/time.h through
        # two -I roots.  Two copies under include/ would each be #pragma
        # once'd on its own, and a unit spelling it both ways would define
        # zx::duration twice.  One file, under lib/; the Makefile adds
        # include/lib so the short spelling still resolves.
        self.write('zircon/ulib/zx/include/lib/zx/time.h', '#pragma once\n')
        self.write('garnet/drivers/a/a.c', '#include <zx/time.h>\n')
        self.write('garnet/drivers/b/b.c', '#include <lib/zx/time.h>\n')
        for letter in 'ab':
            self.module('//garnet/drivers/%s:drv' % letter, letter,
                        'garnet/drivers/%s' % letter,
                        ['garnet/drivers/%s/%s.c' % (letter, letter)],
                        headers=['zircon/ulib/zx/include/lib/zx/time.h'],
                        include_dirs=['zircon/ulib/zx/include',
                                      'zircon/ulib/zx/include/lib'])
        plan = self.component()
        self.assertEqual('zircon/ulib/zx/include/lib/zx/time.h',
                         plan['include/lib/zx/time.h'])
        self.assertNotIn('include/zx/time.h', plan)

    def test_a_quoted_neighbour_follows_its_includer(self):
        # rapidjson's document.h includes "reader.h"; under the path the
        # code uses for document.h, reader.h has to be beside it.
        self.write('third_party/json/json/doc.h', '#include "reader.h"\n')
        self.write('third_party/json/json/reader.h', '#pragma once\n')
        self.write('garnet/drivers/a/a.c',
                   '#include "third_party/json/json/doc.h"\n')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'],
                    headers=['third_party/json/json/doc.h',
                             'third_party/json/json/reader.h'],
                    include_dirs=[''])
        plan = self.component()
        self.assertIn('include/third_party/json/json/reader.h', plan)
        self.assertNotIn('include/reader.h', plan)

    def test_an_include_in_a_comment_is_not_an_include(self):
        self.write('third_party/json/json/doc.h',
                   '/* usage:\n   #include "json/fake.h"\n*/\n'
                   '// #include "json/other.h"\n')
        self.write('third_party/json/json/fake.h', '')
        self.write('third_party/json/json/other.h', '')
        self.write('garnet/drivers/a/a.c',
                   '#include "third_party/json/json/doc.h"\n')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'],
                    headers=['third_party/json/json/doc.h'],
                    include_dirs=['', 'third_party/json'])
        plan = self.component(released=['include/json/fake.h',
                                         'include/json/other.h'])
        # Released or not, an include nothing compiles leads nowhere.
        self.assertFalse([d for d in plan if d.endswith(('fake.h', 'other.h'))])

    def test_an_include_this_compiler_never_takes_is_not_followed(self):
        # msinttypes: included under _MSC_VER, never read, never released.
        self.write('third_party/json/json/doc.h',
                   '#ifdef _MSC_VER\n#include "win.h"\n#endif\n')
        self.write('third_party/json/json/win.h', '')
        self.write('garnet/drivers/a/a.c',
                   '#include "third_party/json/json/doc.h"\n')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'],
                    headers=['third_party/json/json/doc.h'],
                    include_dirs=[''])
        self.assertNotIn('include/third_party/json/json/win.h',
                         self.component())

    def test_one_the_last_release_shipped_is_followed(self):
        # protobuf's atomicops for other architectures were shipped.
        self.write('third_party/json/json/doc.h',
                   '#ifdef _MSC_VER\n#include "win.h"\n#endif\n')
        self.write('third_party/json/json/win.h', '')
        self.write('garnet/drivers/a/a.c',
                   '#include "third_party/json/json/doc.h"\n')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'],
                    headers=['third_party/json/json/doc.h'],
                    include_dirs=[''])
        plan = self.component(
            released=['include/third_party/json/json/win.h'])
        self.assertIn('include/third_party/json/json/win.h', plan)

    def test_a_spelling_the_sysroot_answers_is_not_published_again(self):
        self.write('zircon/public/stdio.h', '')
        self.write('garnet/drivers/a/a.c', '#include <stdio.h>\n')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'],
                    headers=['zircon/public/stdio.h'],
                    include_dirs=['zircon/public'])
        self.assertNotIn('include/stdio.h', self.component())
        self.assertIn('include/stdio.h',
                      self.component(released=['include/stdio.h']))

    def test_a_released_header_now_reached_through_the_sysroot_stays(self):
        # Read from the sysroot and nowhere else, so nothing above publishes
        # it; the last release had it under include/, and it is still there.
        self.write('garnet/drivers/a/a.c', '#include <stdio.h>\n')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'],
                    headers=['out/x/sysroot/include/stdio.h'])
        self.assertNotIn('include/stdio.h', self.component())
        plan = self.component(released=['include/stdio.h'])
        self.assertEqual(plan['sysroot/include/stdio.h'],
                         plan['include/stdio.h'])

    def test_a_released_header_gone_from_the_tree_is_not_invented(self):
        self.write('garnet/drivers/a/a.c', '')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'])
        self.assertNotIn('include/gone.h',
                         self.component(released=['include/gone.h']))

    def test_a_trim_policy_entry_point_is_published(self):
        self.write('zircon/trusty/include/trusty_log.h', '')
        self.write('garnet/drivers/a/a.c', '#include <ddk/driver.h>\n')
        self.targets.clear()
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'],
                    headers=['zircon/ulib/ddk/include/ddk/driver.h'])
        self.targets['//garnet/drivers/a:drv']['include_dirs'] = [
            group('//zircon/trusty:config', ['zircon/trusty/include'],
                  '//zircon/trusty/BUILD.gn:4'),
            group('//zircon/ulib/ddk:config', ['zircon/ulib/ddk/include'],
                  '//zircon/ulib/ddk/BUILD.gn:4')]
        plan = self.component(keeps=['trusty_log.h'])
        self.assertEqual('zircon/trusty/include/trusty_log.h',
                         plan['include/trusty_log.h'])


class HalvesTest(Checkout):

    def setUp(self):
        Checkout.setUp(self)
        self.write('garnet/drivers/mtk/ufs/u.c', '#include "proto.h"\n')
        self.write('garnet/drivers/mtk/ufs/proto.h', '#pragma once\n')
        self.write('garnet/lib/machina/m.cc',
                   '#include "garnet/drivers/mtk/ufs/proto.h"\n')
        self.module('//garnet/drivers/mtk/ufs:drv', 'ufs', 'garnet/drivers/mtk/ufs',
                    ['garnet/drivers/mtk/ufs/u.c'],
                    headers=['garnet/drivers/mtk/ufs/proto.h'],
                    include_dirs=['garnet/drivers/mtk/ufs'])
        self.module('//garnet/lib/machina:machina', 'machina', 'garnet/lib/machina',
                    ['garnet/lib/machina/m.cc'],
                    headers=['garnet/drivers/mtk/ufs/proto.h'],
                    include_dirs=[''], kind='shared_library')

    def test_a_header_read_across_the_halves_is_published_where_the_other_half_looks(self):
        plan = self.component()
        self.assertIn('include/garnet/drivers/mtk/ufs/proto.h', plan)
        self.assertNotIn('include/proto.h', plan)

    def test_it_stays_in_its_own_half_too(self):
        self.assertIn('garnet/drivers/mtk/ufs/proto.h', self.sources())

    def test_a_header_read_only_inside_its_half_is_not_libddks(self):
        self.targets['//garnet/lib/machina:machina']['headers'] = []
        self.write('garnet/lib/machina/m.cc', '')
        self.assertFalse([d for d in self.component() if 'proto.h' in d])


class SourcesTest(Checkout):

    def test_a_modules_territory_is_every_directory_its_sources_are_in(self):
        # Declared in ufs/mt8678, compiling ufs/core, headers in ufs/core/inc.
        self.write('garnet/drivers/ufs/core/c.c', '')
        self.write('garnet/drivers/ufs/core/inc/c.h', '')
        self.write('garnet/drivers/ufs/mt8678/p.c', '')
        self.module('//garnet/drivers/ufs/mt8678:drv', 'ufs', 'garnet/drivers/ufs/mt8678',
                    ['garnet/drivers/ufs/core/c.c', 'garnet/drivers/ufs/mt8678/p.c'],
                    headers=['garnet/drivers/ufs/core/inc/c.h'])
        self.assertIn('garnet/drivers/ufs/core/inc/c.h', self.sources())

    def test_headers_beside_the_sources_ship_but_not_those_only_tests_list(self):
        self.write('garnet/lib/machina/m.cc', '')
        self.write('garnet/lib/machina/guest_view.h', '')
        self.write('garnet/lib/machina/queue_fake.h', '')
        self.module('//garnet/lib/machina:machina', 'machina', 'garnet/lib/machina',
                    ['garnet/lib/machina/m.cc'], kind='shared_library')
        self.module('//garnet/lib/machina:tests', None, 'garnet/lib/machina',
                    ['garnet/lib/machina/queue_fake.h'], kind='executable',
                    testonly=True)
        plan = self.sources()
        self.assertIn('garnet/lib/machina/guest_view.h', plan)
        self.assertNotIn('garnet/lib/machina/queue_fake.h', plan)

    def test_what_a_half_shipped_and_is_still_read_stays(self):
        # blk_crypto.h belongs to a closed library and a delivered driver
        # includes it; the last release shipped it in the half.
        self.write('garnet/drivers/block/b.c', '')
        self.write('garnet/public/lib/blk_crypto/blk_crypto.h', '')
        self.module('//garnet/drivers/block:drv', 'block', 'garnet/drivers/block',
                    ['garnet/drivers/block/b.c'],
                    headers=['garnet/public/lib/blk_crypto/blk_crypto.h'])
        self.assertNotIn('garnet/public/lib/blk_crypto/blk_crypto.h',
                         self.sources())
        self.assertIn('garnet/public/lib/blk_crypto/blk_crypto.h', self.sources(
            {'grt_ddk': ['garnet/public/lib/blk_crypto/blk_crypto.h']}))


class InputsTest(Checkout):

    def test_the_manifest_and_layout_materialize_ddk_reads(self):
        self.write('zircon/x.h', '')
        stage = os.path.join(self.root, 'out/x/stage')
        manifest = os.path.join(self.root, 'm.sdk')
        layout = os.path.join(self.root, 'l.json')
        count = derive_ddk.write_inputs(
            {'include/x.h': 'zircon/x.h',
             'sysroot/lib/libc.so': 'out/x/sysroot/lib/libc.so'},
            self.tree, 'libddk', stage, manifest, layout)
        self.assertEqual(2, count)
        with io.open(manifest, encoding='utf-8') as handle:
            document = json.load(handle)
        files = dict((f['destination'], f['source'])
                     for a in document['atoms'] for f in a['files'])
        self.assertEqual('source://zircon/x.h', files['include/x.h'])
        self.assertEqual('out://sysroot/lib/libc.so',
                         files['sysroot/lib/libc.so'])
        self.assertEqual('1', document['meta']['source-locator-schema'])
        with io.open(layout, encoding='utf-8') as handle:
            contract = json.load(handle)
        self.assertEqual(['header'], contract['content']['include'])
        self.assertEqual(['zircon'], contract['closed_source_roots'])

    def test_a_source_set_is_archived_from_its_objects(self):
        self.write('out/x/obj/a.o', 'object')
        ar = os.path.join(self.root, 'ar')
        with io.open(ar, 'w', encoding='utf-8') as handle:
            handle.write(u'#!/bin/sh\nout=$2; shift 2; cat "$@" > "$out"\n')
        os.chmod(ar, os.stat(ar).st_mode | stat.S_IEXEC)
        self.targets['//third_party/fdt:fdt'] = {'objects': ['obj/a.o']}
        derive_ddk.archive_objects.the_map = self.the_map()
        stage = os.path.join(self.root, 'out/x/stage')
        derive_ddk.write_inputs({'lib/libfdt.a': 'archive://third_party/fdt:fdt'},
                                self.tree, 'libddk', stage,
                                os.path.join(self.root, 'm.sdk'),
                                os.path.join(self.root, 'l.json'), (), ar)
        with open(os.path.join(stage, 'lib/libfdt.a')) as handle:
            self.assertEqual('object', handle.read())


class ComponentCliTest(Checkout):
    """`component` reads the lock itself, so the CLI is what has to be tested.

    Splitting libddk moved most of what the last release shipped inside
    grt_ddk, where the lock records it under half_components.  A header that
    moved is still a header the release shipped.
    """

    def run_component(self, lock):
        paths = {}
        for name, document in (
                ('map', self.the_map()), ('selection', self.selection),
                ('rules', CLI_RULES), ('lock', lock)):
            paths[name] = os.path.join(self.root, name + '.json')
            with io.open(paths[name], 'w', encoding='utf-8') as handle:
                handle.write(u'%s' % json.dumps(document))
        plan = os.path.join(self.root, 'plan.json')
        argv, stdout = sys.argv, sys.stdout
        sys.argv = ['derive_ddk.py', 'component',
                    '--map', paths['map'], '--selection', paths['selection'],
                    '--rules', paths['rules'], '--root', self.root,
                    '--build-dir', os.path.join(self.root, 'out/x'),
                    '--lock', paths['lock'], '--plan', plan]
        sys.stdout = Discarded()
        try:
            self.assertEqual(0, derive_ddk.main())
        finally:
            sys.argv, sys.stdout = argv, stdout
        with io.open(plan, encoding='utf-8') as handle:
            return json.load(handle)['files']

    def setUp(self):
        Checkout.setUp(self)
        self.write('garnet/drivers/a/a.c', '#include <stdio.h>\n')
        self.module('//garnet/drivers/a:drv', 'a', 'garnet/drivers/a',
                    ['garnet/drivers/a/a.c'],
                    headers=['out/x/sysroot/include/stdio.h'])

    def test_a_released_header_recorded_inside_a_half_still_stays(self):
        plan = self.run_component(
            {'files': {}, 'half_components':
             {'grt_ddk': {'include/stdio.h': 'x'}}})
        self.assertEqual(plan['sysroot/include/stdio.h'],
                         plan['include/stdio.h'])

    def test_a_header_no_component_ever_shipped_is_not_invented(self):
        plan = self.run_component({'files': {}, 'half_components': {}})
        self.assertNotIn('include/stdio.h', plan)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for layout.py: where each part of a delivery sits."""

from __future__ import print_function

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'tools'))

import layout  # noqa: E402

NESTED = 'libddk=libddk/aries_v1,grt_ddk=grt_ddk/garnet,mtk_ddk=mtk_ddk/aries_v1'


class ParseTest(unittest.TestCase):

    def test_nothing_is_the_flat_layout(self):
        self.assertEqual({}, layout.parse(''))
        self.assertEqual({}, layout.parse(None))
        self.assertEqual('libddk', layout.where('libddk', {}))

    def test_each_part_goes_where_it_says(self):
        parsed = layout.parse(NESTED)
        self.assertEqual({'libddk': 'libddk/aries_v1',
                          'grt_ddk': 'grt_ddk/garnet',
                          'mtk_ddk': 'mtk_ddk/aries_v1'}, parsed)
        self.assertEqual(os.path.join('out', 'grt_ddk', 'garnet'),
                         layout.part_dir('out', 'grt_ddk', parsed))

    def test_a_part_the_layout_does_not_name_sits_at_its_name(self):
        parsed = layout.parse('libddk=libddk/aries_v1')
        self.assertEqual('grt_ddk', layout.where('grt_ddk', parsed))

    def test_spec_is_the_inverse_of_parse(self):
        parsed = layout.parse(NESTED)
        self.assertEqual(parsed, layout.parse(layout.spec(parsed)))

    def test_slashes_around_a_directory_are_not_part_of_it(self):
        self.assertEqual({'libddk': 'a/b'}, layout.parse('libddk=/a/b/'
                                                         .replace('/a', 'a')))

    def test_what_is_refused(self):
        for bad in ('libddk', 'libddk=', '=x', 'libddk=/abs',
                    'libddk=../up', 'libddk=x,libddk=y', 'libddk=x,grt_ddk=x',
                    'libddk=x,grt_ddk=x/y'):
            with self.assertRaises(layout.LayoutError):
                layout.parse(bad)


class DeliveryTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='layout-test-')
        self.layout = layout.parse(NESTED)
        for part in ('libddk', 'grt_ddk'):
            os.makedirs(layout.part_dir(self.root, part, self.layout))
        os.makedirs(os.path.join(self.root, 'mtk_ddk'))  # wrapper, empty

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_parts_are_found_where_the_layout_puts_them(self):
        # mtk_ddk/ is there, but mtk_ddk/aries_v1 is not: not a part.
        self.assertEqual(['libddk', 'grt_ddk'],
                         layout.parts(self.root, self.layout))

    def test_without_a_layout_every_directory_is_a_part(self):
        self.assertEqual(['grt_ddk', 'libddk', 'mtk_ddk'],
                         layout.parts(self.root, {}))

    def test_a_directory_is_named_back_to_its_part(self):
        self.assertEqual('grt_ddk', layout.part_of(
            self.root, os.path.join(self.root, 'grt_ddk', 'garnet'),
            self.layout))
        self.assertIsNone(layout.part_of(
            self.root, os.path.join(self.root, 'grt_ddk'), self.layout))

    def test_the_wrappers_are_expected_entries(self):
        self.assertEqual({'libddk', 'libddk/aries_v1', 'grt_ddk',
                          'grt_ddk/garnet', 'mtk_ddk', 'mtk_ddk/aries_v1'},
                         layout.expected_entries(self.layout))
        self.assertEqual({'libddk', 'grt_ddk', 'mtk_ddk'},
                         layout.expected_entries({}))

    def test_the_policy_names_it(self):
        path = os.path.join(self.root, 'delivery.json')
        with open(path, 'w') as handle:
            json.dump({'layout': {'libddk': 'libddk/aries_v1'}}, handle)
        self.assertEqual({'libddk': 'libddk/aries_v1'},
                         layout.read_policy(path))
        with open(path, 'w') as handle:
            json.dump({'deliver': []}, handle)
        self.assertEqual({}, layout.read_policy(path))


if __name__ == '__main__':
    unittest.main()

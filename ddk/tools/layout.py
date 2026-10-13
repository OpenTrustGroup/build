#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Where each part of a delivery sits.

A delivery has three parts -- libddk, grt_ddk, mtk_ddk -- and MTK keep each
one as a repo project.  The manifest puts them one level down from the part's
name, and the project's own name is the directory:

    src/hypervisor/
      libddk/aries_v1/    the component
      grt_ddk/garnet/     one source half
      mtk_ddk/aries_v1/   the other

output/ is laid out the same way, so what the release produces is what the
manifest provisions, directory for directory.  The layout is policy --
`delivery.json` names it -- because nothing in the build knows what a project
is called on the other side.

Tools take it as `--layout PART=DIR,PART=DIR`.  Without one, a part sits at
its own name, which is what the tests build and what a delivery looked like
before the layout existed.
"""

import json
import os

PARTS = ('libddk', 'grt_ddk', 'mtk_ddk')


class LayoutError(Exception):
    pass


def parse(spec):
    """PART=DIR,PART=DIR -> {part: dir}.  Empty or None is the flat layout."""
    layout = {}
    for item in (spec or '').split(','):
        item = item.strip()
        if not item:
            continue
        if '=' not in item:
            raise LayoutError('layout entry %r is not PART=DIR' % item)
        part, _, where = item.partition('=')
        part, where = part.strip(), where.strip()
        if not part or not where:
            raise LayoutError('layout entry %r is not PART=DIR' % item)
        if os.path.isabs(where) or '..' in where.split('/'):
            raise LayoutError('%s: %r must be relative and inside the '
                              'delivery' % (part, where))
        where = where.strip('/')
        if part in layout:
            raise LayoutError('%s is laid out twice' % part)
        layout[part] = where
    placed = list(layout.values())
    if len(set(placed)) != len(placed):
        raise LayoutError('two parts share a directory: %s'
                          % ', '.join(sorted(set(
                              p for p in placed if placed.count(p) > 1))))
    for part, where in layout.items():
        for other, elsewhere in layout.items():
            if other != part and (elsewhere + '/').startswith(where + '/'):
                raise LayoutError('%s at %s would contain %s at %s'
                                  % (part, where, other, elsewhere))
    return layout


def spec(layout):
    """The inverse of parse: what to hand a tool on its command line."""
    return ','.join('%s=%s' % item for item in sorted(layout.items()))


def read_policy(path):
    """The layout delivery.json names, or the flat one if it names none."""
    with open(path) as handle:
        policy = json.load(handle)
    layout = policy.get('layout') or {}
    return parse(','.join('%s=%s' % item for item in sorted(layout.items())))


def where(part, layout=None):
    """The directory a part sits in, relative to the delivery."""
    return (layout or {}).get(part, part)


def part_dir(delivery, part, layout=None):
    return os.path.join(delivery, *where(part, layout).split('/'))


def parts(delivery, layout=None, known=PARTS):
    """The parts a delivery holds.

    With a layout, by looking where each known part would be: a directory the
    layout does not name is not a part.  Without one, every directory is a
    part, which is what a flat delivery has always meant.
    """
    if not layout:
        return sorted(name for name in os.listdir(delivery)
                      if os.path.isdir(os.path.join(delivery, name)))
    return [part for part in known
            if os.path.isdir(part_dir(delivery, part, layout))]


def part_of(delivery, path, layout=None, known=PARTS):
    """Which part a directory under the delivery is, or None."""
    relative = os.path.relpath(path, delivery).replace(os.sep, '/')
    for part in known:
        if where(part, layout) == relative:
            return part
    return None


def expected_entries(layout=None, known=PARTS):
    """Every directory a delivery may hold, wrappers included, relative."""
    entries = set()
    for part in known:
        pieces = where(part, layout).split('/')
        for depth in range(1, len(pieces) + 1):
            entries.add('/'.join(pieces[:depth]))
    return entries

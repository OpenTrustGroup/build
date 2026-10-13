#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import json
import os
import sys


SOURCE_LOCATOR_SCHEMA = '1'
try:
    STRING_TYPES = (basestring,)
except NameError:
    STRING_TYPES = (str,)


def _source_locator(source, roots):
    if not os.path.isabs(source):
        raise ValueError('SDK source is not absolute: %s' % source)

    source = os.path.realpath(source)
    if not os.path.exists(source):
        raise ValueError('SDK source does not exist: %s' % source)
    for root_id, root in roots:
        root = os.path.realpath(root)
        if source == root:
            raise ValueError('SDK source must name a file below a root: %s' %
                             source)
        elif source.startswith(root + os.sep):
            relative = os.path.relpath(source, root)
        else:
            continue
        return '%s://%s' % (root_id, relative.replace(os.sep, '/'))
    raise ValueError('SDK source is outside declared roots: %s' % source)


def make_portable_manifest(manifest, out_root, source_root):
    if not isinstance(manifest, dict):
        raise ValueError('SDK manifest must be an object')
    atoms = manifest.get('atoms')
    metadata = manifest.get('meta')
    if not isinstance(atoms, list) or not isinstance(metadata, dict):
        raise ValueError('SDK manifest is missing atoms or metadata')

    roots = (
        ('out', out_root),
        ('source', source_root),
    )
    for atom in atoms:
        for entry in atom.get('files', []):
            source = entry.get('source')
            if not isinstance(source, STRING_TYPES):
                raise ValueError('SDK file source must be a string')
            entry['source'] = _source_locator(source, roots)
    metadata['source-locator-schema'] = SOURCE_LOCATOR_SCHEMA
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--out-root', required=True)
    parser.add_argument('--source-root', required=True)
    args = parser.parse_args()

    with open(args.manifest, 'r') as manifest_file:
        manifest = json.load(manifest_file)
    try:
        make_portable_manifest(manifest, args.out_root, args.source_root)
    except ValueError as error:
        parser.error(str(error))
    with open(os.path.abspath(args.out), 'w') as output_file:
        json.dump(manifest, output_file, indent=2, sort_keys=True)


if __name__ == '__main__':
    sys.exit(main())

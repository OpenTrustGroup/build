#!/usr/bin/env python
# Copyright 2017 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import json
import sys

from sdk_common import Atom


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest',
                        help='Path to the SDK\'s manifest file',
                        required=True)
    parser.add_argument('--meta',
                        help='Path to output metadata file',
                        required=True)
    parser.add_argument('--target-arch',
                        help='Architecture of precompiled target atoms',
                        required=True)
    parser.add_argument('--host-arch',
                        help='Architecture of host tools',
                        required=True)
    args = parser.parse_args()

    with open(args.manifest, 'r') as manifest_file:
        manifest = json.load(manifest_file)

    atoms = [Atom(a) for a in manifest['atoms']]
    meta = {
        'arch': {
            'host': args.host_arch,
            'target': [
                args.target_arch,
            ],
        },
        'parts': filter(lambda m: m, [a.metadata for a in atoms]),
    }

    with open(args.meta, 'w') as meta_file:
        json.dump(meta, meta_file, indent=2, sort_keys=True)


if __name__ == '__main__':
    sys.exit(main())

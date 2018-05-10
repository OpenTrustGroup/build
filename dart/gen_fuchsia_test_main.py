#!/usr/bin/env python
# Copyright 2017 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import os
import re
import sys


def main():
    parser = argparse.ArgumentParser(
        sys.argv[0],
        description="Generate main file for Fuchsia dart test")
    parser.add_argument("--out",
                        help="Path to .dart file to generate",
                        required=True)
    parser.add_argument('--source-dir',
                        help='Path to test sources',
                        required=True)
    parser.add_argument("--helper",
                        help="Path to fuchsia_test_helper.dart",
                        required=True)
    args = parser.parse_args()

    out_dir = os.path.dirname(args.out)
    test_files = []
    for root, dirs, files in os.walk(args.source_dir):
        for f in files:
            if not f.endswith('_test.dart'):
                continue
            test_files.append(os.path.relpath(os.path.join(root, f), out_dir))

    outfile = open(args.out, 'w')
    outfile.write('''// Generated by generate_test_main.py

    // ignore_for_file: directives_ordering

    import 'dart:async';
    import 'dart:isolate';
    import '%s';
    ''' % os.path.relpath(args.helper, out_dir))

    for i, path in enumerate(test_files):
        outfile.write("import '%s' as test_%d;\n" % (path, i))

    outfile.write('''
    Future<int> main(List<String> args) async {
      await runFuchsiaTests(<MainFunction>[
    ''')

    for i in range(len(test_files)):
        outfile.write('test_%d.main,\n' % i)

    outfile.write(''']);

      // Quit.
      Isolate.current.kill();
      return 0;
    }
    ''')

    outfile.close()


if __name__ == '__main__':
    main()
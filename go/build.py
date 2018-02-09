#!/usr/bin/env python
# Copyright 2017 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# Build script for a Go app.

import argparse
import os
import subprocess
import sys
import string
import shutil
import errno


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fuchsia-root', help='Path to root of Fuchsia project',
                        required=True)
    parser.add_argument('--root-out-dir', help='Path to root of build output',
                        required=True)
    parser.add_argument('--zircon-sysroot', help='The Zircon sysroot to use',
                        required=True)
    parser.add_argument('--depfile', help='The path to the depfile',
                        required=True)
    parser.add_argument('--current-cpu', help='Target architecture.',
                        choices=['x64', 'arm64'], required=True)
    parser.add_argument('--current-os', help='Target operating system.',
                        choices=['fuchsia', 'linux', 'mac', 'win'], required=True)
    parser.add_argument('--go-tool', help='The go tool to use for builds')
    parser.add_argument('--is-test', help='True if the target is a go test',
                        default=False)
    parser.add_argument('--go-dependency', help='Manifest of dest=src of dependencies',
                        action='append')
    parser.add_argument('--binname', help='Output file', required=True)
    parser.add_argument('--unstripped-binname', help='Unstripped output file')
    parser.add_argument('--toolchain-prefix', help='Path to toolchain binaries',
                        required=False)
    parser.add_argument('--verbose', help='Tell the go tool to be verbose about what it is doing',
                        action='store_true')
    parser.add_argument('package', help='The package name')
    args = parser.parse_args()

    goarch = {
        'x64': 'amd64',
        'arm64': 'arm64',
    }[args.current_cpu]
    goos = {
        'fuchsia': 'fuchsia',
        'linux': 'linux',
        'mac': 'darwin',
        'win': 'windows',
    }[args.current_os]

    output_name = os.path.join(args.root_out_dir, args.binname)
    depfile_output = output_name
    if args.unstripped_binname:
        stripped_output_name = output_name
        output_name = os.path.join(args.root_out_dir, 'exe.unstripped',
                                   args.binname)

    # Project path is a package specific gopath, also known as a "project" in go parlance.
    project_path = os.path.join(args.root_out_dir, 'gen', 'gopaths', args.binname)

    # Clean up any old project path to avoid leaking old dependencies
    shutil.rmtree(os.path.join(project_path, 'src'), ignore_errors=True)
    os.makedirs(os.path.join(project_path, 'src'))

    if args.go_dependency:
      # Create a gopath for the packages dependency tree
      for dep in args.go_dependency:
        dst, src = string.split(dep, '=', 2)
        # |dst| must be relative
        if os.path.isabs(dst):
          raise ValueError("--go-dependency destination location must be relative to $project_path/src")
        dstdir = os.path.join(project_path, 'src', os.path.dirname(dst))
        try:
          os.makedirs(dstdir)
        except OSError as e:
          # EEXIST occurs if two gopath entries share the same parent name
          if e.errno != errno.EEXIST:
            raise
        tgt = os.path.join(dstdir, os.path.basename(dst))
        # The source tree is effectively read-only once the build begins.
        # Therefore it is an error if tgt is in the source tree. At first
        # glance this may seem impossible, but it can happen if dst is foo/bar
        # and foo is a symlink back to the source tree.
        canon_root_out_dir = os.path.realpath(args.root_out_dir)
        canon_tgt = os.path.realpath(tgt)
        if not canon_tgt.startswith(canon_root_out_dir):
          raise ValueError("--go-dependency destination not in --root-out-dir: provided=%s, path=%s, realpath=%s" % (dst, tgt, canon_tgt))
        os.symlink(src, tgt)

    gopath = os.path.abspath(project_path)

    env = {}
    if args.current_os == 'fuchsia':
        env['CGO_ENABLED'] = '1'
    env['GOARCH'] = goarch
    env['GOOS'] = goos
    env['GOPATH'] = gopath
    env['ZIRCON_SYSROOT'] = args.zircon_sysroot

    if goos == 'fuchsia':
        gengoroot = os.path.abspath(os.path.join(args.root_out_dir, 'goroot'))
        if os.path.exists(gengoroot):
            env['GOROOT'] = gengoroot

    # /usr/bin:/bin are required for basic things like bash(1) and env(1), but
    # preference the toolchain path. Note that on Mac, ld is also found from
    # /usr/bin.
    env['PATH'] = args.toolchain_prefix + ":/usr/bin:/bin"

    cmd = [args.go_tool]
    if args.is_test:
      cmd += ['test', '-c']
    else:
      cmd += ['build']
    if args.verbose:
      cmd += ['-x']
    cmd += ['-pkgdir', os.path.join(project_path, 'pkg'), '-o',
            output_name, args.package]

    retcode = subprocess.call(cmd, env=env)

    if retcode == 0 and args.unstripped_binname:
      retcode = subprocess.call([os.path.join(args.toolchain_prefix,
                                              'llvm-objcopy'),
                                 '--strip-sections',
                                 output_name,
                                 stripped_output_name],
                                env=env)

    if retcode == 0:
        godepfile = os.path.join(args.fuchsia_root, 'buildtools/godepfile')
        if args.depfile is not None:
            with open(args.depfile, "wb") as out:
                env['GOROOT'] = os.path.join(args.fuchsia_root, "third_party/go")
                godepfile_args = [godepfile, '-o', depfile_output]
                if args.is_test:
                    godepfile_args += [ '-test']
                godepfile_args += [args.package]
                subprocess.Popen(godepfile_args, stdout=out, env=env)

    return retcode


if __name__ == '__main__':
    sys.exit(main())

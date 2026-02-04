#!/usr/bin/env python3

import io
import os
import shutil
import subprocess


class PackagesResolver:

  def resolve(self, packages_root, packages, out_cwd):
    '''
    Resolve what packages included in |packages| from |packages_root|.

    Example:
      packages = garnet/packages/venus-hee
      return [nbl_loader_tests, nbl_activity_tests...]
    '''

    def parseline(line):
      begin = line.index('[') + 1
      end = line.rindex(']')
      return [x.strip(' "') for x in line[begin:end].split(',')]

    assert os.path.exists(out_cwd)

    zircon_project = os.path.basename(packages)
    target_gen_dir = os.path.join(out_cwd, 'out/' + zircon_project + '/arm64/gen/build/gn/')
    packages_file = os.path.join(target_gen_dir, 'packages')
    system_manifest_file = os.path.join(target_gen_dir,
                                        'system.bootfs.manifest')
    args = [
        'build/gn/process_build_packages.py', '--targets-file', packages_file,
        '--system-manifest', system_manifest_file,
        '--packages=["' + packages + '"]', '--packages-root', packages_root,
        '--build-root', 'out/' + zircon_project + '/arm64/'
    ]
    print("args=" + str(args))
    output = io.StringIO(subprocess.check_output(args, universal_newlines=True))
    return parseline(output.readline())


def copyfile_relative(filename: str, src_cwd: str, dst_cwd: str) -> str:
  '''
  Copy file to |dst_cwd| from 'cur_cwd' or |src_cwd| perspective.

    Example:

    cur_cwd = /tmp/nebula/out/arm64
    filename = 'clang/7.0.0/lib/fuchsia/libclang_rt.asan-aarch64.so'
    src_cwd = '../../buildtools/linux-x64/clang/lib'
    dst_cwd = /tmp/buildroot/out/arm64

    case 1: filename exitst from |cur_cwd| perspective.
            result: /tmp/buildroot/out/arm64/clang/7.0.0/lib...

    case 2: filename not exitst from |cur_cwd| perspective.
            result: /tmp/buildroot/buildtools/linux-x64/clang...
  '''
  assert os.path.exists(src_cwd)
  assert not os.path.isabs(src_cwd)
  assert os.path.exists(dst_cwd)

  if not os.path.exists(filename):
    filename = os.path.normpath(os.path.join(src_cwd, filename))

  assert os.path.exists(filename)
  assert not os.path.isabs(filename)

  dst_dir = os.path.dirname(os.path.normpath(os.path.join(dst_cwd, filename)))

  os.makedirs(dst_dir, exist_ok=True)
  return shutil.copy(filename, dst_dir)


def copydir_relative(dirname: str,
                     src_cwd: str,
                     dst_cwd: str,
                     ignore=None) -> list:
  '''
  Copy dir to |dst_cwd| from 'cur_cwd' or |src_cwd| perspective.
  '''
  assert os.path.exists(src_cwd)
  assert not os.path.isabs(src_cwd)
  assert os.path.exists(dst_cwd)

  if not os.path.exists(dirname):
    dirname = os.path.normpath(os.path.join(src_cwd, dirname))

  assert os.path.exists(dirname)
  assert not os.path.isabs(dirname)

  return shutil.copytree(dirname, os.path.join(dst_cwd, dirname), ignore=ignore)


def get_supported_platforms(all_boards_file: str, arch: str) -> list:
  supported_platforms = {}
  with open(all_boards_file) as file:
    for line in file:
      if line.startswith('['):
        end = line.find(']')
        assert end > 0, 'Undetermined board file line: %r' % line
        curr_platform = line[1:end]
        supported_platforms[curr_platform] = []
      else:
        supported_platforms[curr_platform].append(line.strip())

  return supported_platforms[arch]

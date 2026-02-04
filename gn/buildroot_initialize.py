#!/usr/bin/env python3

import argparse
import collections
import os
import common
import logging
import manifest
import paths
from shutil import ignore_patterns
import typing
import shutil

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s',
                    datefmt='%Y-%m-%d %H:%M',
                    level=logging.INFO)

artifact_entry = collections.namedtuple('artifact_entry', [
    'cwd',
    'manifest',
])

# assume cwd = out/venus-hee/arm64
default_artifacts = [
    artifact_entry(
        '../../../buildtools/linux-x64/clang/lib',
        '../../../buildtools/linux-x64/clang/lib/aarch64-fuchsia.manifest'),

    # PackagesResolver
    artifact_entry('gen/build/gn/', 'gen/build/gn/system.bootfs.manifest'),

    # debug
    artifact_entry('../../build-zircon/build-venus-hee-ulib',
                   '../../build-zircon/build-venus-hee-ulib/bootfs.manifest'),
    artifact_entry('../../build-zircon/build-venus-hee',
                   '../../build-zircon/build-venus-hee/bootfs.manifest'),

    # release
    artifact_entry(
        '../../build-zircon-release/build-venus-hee-ulib',
        '../../build-zircon-release/build-venus-hee-ulib/bootfs.manifest'),
    artifact_entry('../../build-zircon-release/build-venus-hee',
                   '../../build-zircon-release/build-venus-hee/bootfs.manifest'),
]

# assume cwd = out/venus-tee/arm64
default_artifacts_tee = [
    artifact_entry(
        '../../../buildtools/linux-x64/clang/lib',
        '../../../buildtools/linux-x64/clang/lib/aarch64-fuchsia.manifest'),

    # PackagesResolver
    artifact_entry('gen/build/gn/', 'gen/build/gn/system.bootfs.manifest'),

    # debug
    artifact_entry('../../build-zircon/build-venus-tee-ulib',
                   '../../build-zircon/build-venus-tee-ulib/bootfs.manifest'),
    artifact_entry('../../build-zircon/build-venus-tee',
                   '../../build-zircon/build-venus-tee/bootfs.manifest'),

    # release
    artifact_entry(
        '../../build-zircon-release/build-venus-tee-ulib',
        '../../build-zircon-release/build-venus-tee-ulib/bootfs.manifest'),
    artifact_entry('../../build-zircon-release/build-venus-tee',
                   '../../build-zircon-release/build-venus-tee/bootfs.manifest'),
]

def append_manifest_entry(file: typing.TextIO,
                          entry: manifest.manifest_entry) -> None:
  line = ''
  if entry.in_image:
    line += '[' + entry.in_image + ']'
  if entry.group:
    line += '{' + entry.group + '}'
  line += entry.target + '=' + entry.source + '\n'
  file.write(line)


def emit_manifests(filename: str, manifest_cwd: str,
                   manifests: manifest.manifest_entry) -> None:
  filename = os.path.normpath(os.path.join(manifest_cwd, filename))
  os.makedirs(os.path.dirname(filename), exist_ok=True)
  with open(filename, 'w') as file:
    for entry in manifests:
      append_manifest_entry(file, entry)


# def maybe_copy_unstripped(filename: str, manifest_cwd: str,
#                           dst_cwd: str) -> None:
#   filename = os.path.join(manifest_cwd, filename)

#   if filename.endswith('.strip'):
#     debugfile = filename[:-6]
#   else:
#     dir, file = os.path.split(filename)
#     if file.endswith('.so') or '.so.' in file:
#       subdir = 'lib.unstripped'
#     else:
#       subdir = 'exe.unstripped'
#     debugfile = os.path.join(dir, subdir, file)
#     if not os.path.exists(debugfile):
#       debugfile = os.path.join(subdir, filename)
#       if not os.path.exists(debugfile):
#         return

#   if debugfile != filename:
#     common.copyfile_relative(debugfile, manifest_cwd, dst_cwd)


def copy_artifacts(manifest_path: str, manifest_cwd: str, dst_cwd: str) -> list:
  result = []
  with open(manifest_path, 'r') as file:
    manifest_entries = manifest.read_manifest_file(file, manifest_cwd,
                                                   manifest_cwd)
    for entry in manifest_entries:
      common.copyfile_relative(entry.source, manifest_cwd, dst_cwd)
      # maybe_copy_unstripped(entry.source, manifest_cwd, dst_cwd)
      result.append(entry)
  return result


def copy_libraries(dst_cwd: str) -> None:
  path = 'arm64-shared'
  for filename in os.listdir(path):
    if filename.endswith('.so'):
      common.copyfile_relative(os.path.join(path, filename), '.', dst_cwd)


def copy_zircon_binaries(dst_cwd: str, zircon_project: str) -> None:
  all_boards_file = '../../build-zircon/build-' + zircon_project + '/export/all-boards.list'
  supported_platforms = common.get_supported_platforms(all_boards_file, 'arm64')
  assert len(supported_platforms) > 0

  cwds = [
      '../../build-zircon/build-' + zircon_project + '/',
      '../../build-zircon-release/build-' + zircon_project + '/',
  ]
  for cwd in cwds:
    for platform in supported_platforms:
      mdi = os.path.join(cwd, '{}-mdi.bin'.format(platform))
      shim = os.path.join(cwd, '{}-boot-shim.bin'.format(platform))
      common.copyfile_relative(mdi, '.', dst_cwd)
      common.copyfile_relative(shim, '.', dst_cwd)

    common.copyfile_relative(os.path.join(cwd, 'zircon.bin'), '.', dst_cwd)


def copy_cmdline_binaries(dst_cwd: str) -> None:
  common.copyfile_relative('cmdline.bin', '.', dst_cwd)
  common.copyfile_relative('cmdline.rel.bin', '.', dst_cwd)


def copy_mkbootfs(dst_cwd: str, zircon_project: str) -> None:
  common.copyfile_relative('../../build-zircon/build-' + zircon_project + '/tools/mkbootfs',
                           '.', dst_cwd)


def override_bootfs_manifest(packges_home: str, dst_cwd: str, zircon_project: str) -> None:
  entries = [
      (os.path.join(packges_home, 'bootfs.manifest.debug.' + zircon_project),
       os.path.join(dst_cwd,
                    '../../build-zircon/build-' + zircon_project + '/bootfs.manifest')),
      (os.path.join(packges_home, 'bootfs.manifest.release.' + zircon_project),
       os.path.join(dst_cwd,
                    '../../build-zircon-release/build-' + zircon_project + '/bootfs.manifest')),
  ]
  for src, dst in entries:
    assert os.path.exists(src)
    assert os.path.exists(dst)
    shutil.copyfile(src, dst)


def copy_packages(nebula_home: str, buildroot_home: str,
                  packages_home: str, zircon_project: str) -> None:
  packages = common.PackagesResolver().resolve(packages_home,
                                               'garnet/packages/' + zircon_project,
                                               buildroot_home)
  manifest_filenames = [
      'boot_manifest',
      'system_manifest',
      'archive_manifest',
      'package_manifest',
      'final_archive_manifest',
      'final_package_manifest',
  ]

  if zircon_project == 'venus-hee':
    artifacts = default_artifacts
  elif zircon_project == 'venus-tee':
    artifacts = default_artifacts_tee

  cur_cwd = os.getcwd()
  nebula_cwd = os.path.join(nebula_home, 'out/' + zircon_project + '/arm64')
  buildroot_cwd = os.path.join(buildroot_home, 'out/' + zircon_project + '/arm64')

  for package in packages:
    for filename in manifest_filenames:
      manifest_path = os.path.join(os.path.join('package', package), filename)
      artifacts.append(artifact_entry('.', manifest_path))

  # switch to out/<zircon_project>/arm64 and simulate GN actions
  os.chdir(nebula_cwd)

  for entry in artifacts:
    manifest_entries = copy_artifacts(entry.manifest, entry.cwd, buildroot_cwd)
    emit_manifests(entry.manifest, buildroot_cwd, manifest_entries)

  # Copy all stripped .so to solve dependencies not listed in manifests.
  copy_libraries(buildroot_cwd)

  copy_cmdline_binaries(buildroot_cwd)
  copy_zircon_binaries(buildroot_cwd, zircon_project)
  copy_mkbootfs(buildroot_cwd, zircon_project)

  override_bootfs_manifest(packages_home, buildroot_cwd, zircon_project)
  os.chdir(cur_cwd)


def buildroot_initialize(nebula_home: str, buildroot_home: str,
                         packages_home: str) -> None:
  assert os.path.exists(nebula_home)
  assert os.path.exists(packages_home)

  # In case copying into ourself
  assert buildroot_home != paths.FUCHSIA_ROOT

  if os.path.exists(buildroot_home):
    shutil.rmtree(buildroot_home)
  os.mkdir(buildroot_home)

  file_list = [
    'build/packages/bootfs',
    'build/gn/buildroot_gen_product.py',
    'build/gn/buildroot_initialize.py',
    'build/gn/buildroot_transfer.py',
    'build/gn/common.py',
    'build/gn/elfinfo.py',
    'build/gn/finalize_manifests.py',
    'build/gn/manifest.py',
    'build/gn/package_imports_resolver.py',
    'build/gn/paths.py',
    'build/gn/process_build_packages.py',
    'build/gn/variant.py',
  ]

  dir_list = [
    'build/nebula/buildroot_packages',
  ]

  for f in file_list:
    common.copyfile_relative(f, '.', buildroot_home)

  for d in dir_list:
    common.copydir_relative(d,
                            '.',
                            buildroot_home,
                            ignore=ignore_patterns('.*', '*.pyc', '__pycache__'))

  copy_packages(nebula_home, buildroot_home, packages_home, 'venus-hee')
  copy_packages(nebula_home, buildroot_home, packages_home, 'venus-tee')



def parse_args():
  parser = argparse.ArgumentParser(description='Setup buildroot environment')
  parser.add_argument('--nebula-home',
                      required=True,
                      help='a path to nebula source root')
  parser.add_argument('--buildroot-home',
                      required=True,
                      help='a path to generate buildroot')
  parser.add_argument('--packages-home',
                      required=True,
                      help='a path to folder containing buildroot packages')

  return parser.parse_args()


def main():
  args = parse_args()

  buildroot_initialize(args.nebula_home, args.buildroot_home,
                       args.packages_home)


if __name__ == '__main__':
  raise SystemExit(main())

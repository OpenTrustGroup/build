#!/usr/bin/env python3

import argparse
import common
import json
import logging
import os
import subprocess

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s',
                    datefmt='%Y-%m-%d %H:%M',
                    level=logging.INFO)


def finalize_manifests(platform: str, product: str, packages: list,
                       variant: str, zircon_project: str) -> dict:
  '''
    The --image-type is created image's type, if file which set the same image-type will be put in manifest.
    The --cwd and --group options apply to subsequent --manifest arguments.  Each
    input --manifest is assigned to the preceding --output/-standalone-output
    argument file.  Any input --manifest that precedes all --output arguments just
    supplies auxiliary files implicitly required by other (later) input manifests,
    but does not add all its files to any --output manifest.  This is used for
    shared libraries and the like.
  '''

  assert platform
  assert product
  assert packages

  image_type = variant
  if image_type == 'debug':
    depfile = 'finalize_manifests_{}-{}.d'.format(platform, product)
    buildid_file = 'ids.txt.{}-{}'.format(platform, product)
    zircon_out_dir = 'build-zircon'
    boot_manifest_out = 'boot.manifest.{}-{}'.format(platform, product)
    system_manifest_out = 'system.manifest.{}-{}'.format(platform, product)
    boot_image_groups = 'misc,test'
    system_image_groups = 'all'
  elif image_type == 'release':
    depfile = 'finalize_manifests_{}-{}-rel.d'.format(platform, product)
    buildid_file = 'ids.txt.{}-{}-rel'.format(platform, product)
    zircon_out_dir = 'build-zircon-release'
    boot_manifest_out = 'boot.manifest.{}-{}-rel'.format(platform, product)
    system_manifest_out = 'system.manifest.{}-{}-rel'.format(platform, product)
    boot_image_groups = ''
    system_image_groups = ''
  else:
    raise NotImplementedError

  args = ['../../../build/gn/finalize_manifests.py']
  args += ['--depfile={}'.format(depfile)]
  args += ['--build-id-file={}'.format(buildid_file)]
  args += ['--image-type={}'.format(image_type)]

  ###########################################################################
  # aux manifest
  args += ['--cwd=../../../buildtools/linux-x64/clang/lib']
  args += [
      '--manifest=../../../buildtools/linux-x64/clang/lib/aarch64-fuchsia.manifest'
  ]

  args += ['--cwd=../../{}/build-{}-ulib'.format(zircon_out_dir, zircon_project)]
  args += [
      '--manifest=../../{}/build-{}-ulib/bootfs.manifest'.format(
          zircon_out_dir, zircon_project)
  ]

  ###########################################################################
  # boot_manifest
  args += ['--output={}'.format(boot_manifest_out)]
  args += ['--groups=core']
  args += ['--cwd=../../../']
  args += [
      '--manifest=../../{}/build-{}/bootfs.manifest'.format(zircon_out_dir, zircon_project)
  ]
  args += ['--cwd=.']
  args += ['--groups=all']
  for package in packages:
    args += ['--manifest=package/%s/boot_manifest' % package]

  ###########################################################################
  # system_manifest
  args += ['--output={}'.format(system_manifest_out)]
  args += ['--groups={}'.format(boot_image_groups)]
  args += ['--cwd=../../../']
  args += [
      '--manifest=../../{}/build-{}/bootfs.manifest'.format(zircon_out_dir, zircon_project)
  ]
  args += ['--cwd=.']
  args += ['--groups={}'.format(system_image_groups)]
  args += ['--manifest=gen/build/gn/system.bootfs.manifest']
  for package in packages:
    args += ['--manifest=package/%s/system_manifest' % package]

  ###########################################################################
  # archive_manifest
  for package in packages:
    args += ['--standalone-output=package/%s/final_archive_manifest' % package]
    args += ['--manifest=package/%s/archive_manifest' % package]

  ###########################################################################
  # package_manifest
  for package in packages:
    args += ['--standalone-output=package/%s/final_package_manifest' % package]
    args += ['--manifest=package/%s/package_manifest' % package]

  logging.debug('cmd: %s', args)
  subprocess.check_call(args, universal_newlines=True)

  return boot_manifest_out, system_manifest_out


def generate_finalize_manifests(platform: str, product: str, packages: list, zircon_project: str):
  manifests = {'boot': {}, 'system': {}}
  for variant in ['debug', 'release']:
    boot_manifest, system_manifest = finalize_manifests(platform, product,
                                                        packages, variant,
                                                        zircon_project)
    manifests['boot'][variant] = boot_manifest
    manifests['system'][variant] = system_manifest
  return manifests


def mkbootfs_checked(args, zircon_project: str):
  args = ['../../build-zircon/build-' + zircon_project + '/tools/mkbootfs'] + args
  logging.info('cmd: %s', args)
  subprocess.check_call(args, universal_newlines=True)


def generate_plid_img(platform: str, product: str, vid: str, pid: str, sbt: str, zircon_project: str) -> dict:
  outputs = {
      'debug': 'bootdata-plid-{}-{}.bin'.format(platform, product),
      'release': 'bootdata-plid-{}-{}-rel.bin'.format(platform, product),
  }

  for variant in ['debug', 'release']:
    args = [
        '-o', outputs[variant], '--vid', vid, '--pid', pid, '--board', product, '--sbt', sbt
    ]
    mkbootfs_checked(args, zircon_project)

  return outputs


def generate_cmdline_img(platform: str, product: str, cmdline_file, zircon_project: str) -> dict:
  outputs = {
      'debug': 'cmdline_{}-{}.bin'.format(platform, product),
      'release': 'cmdline_{}-{}-rel.bin'.format(platform, product),
  }

  for variant in ['debug', 'release']:
    args = ['-o', outputs[variant], '-C', cmdline_file]
    mkbootfs_checked(args, zircon_project)

  return outputs


def generate_boot_bootfs(platform: str, product: str, cmdline_img: dict,
                         manifest: dict, zircon_project: str) -> dict:
  outputs = {
      'debug': 'boot_bootfs_{}-{}.bin'.format(platform, product),
      'release': 'boot_bootfs_{}-{}-rel.bin'.format(platform, product),
  }
  depfile = {
      'debug': 'boot_bootfs_{}-{}.bin.d'.format(platform, product),
      'release': 'boot_bootfs_{}-{}-rel.bin.d'.format(platform, product),
  }
  mdifile = {
      'debug': '../../build-zircon/build-{}/{}-mdi.bin'.format(zircon_project, platform),
      'release': '../../build-zircon/build-{}/{}-mdi.bin'.format(zircon_project, platform),
  }
  cmdline_base = {'debug': 'cmdline.bin', 'release': 'cmdline.rel.bin'}

  for variant in ['debug', 'release']:
    args = [
        '-o', outputs[variant], '--depfile', depfile[variant], '--target=boot',
        mdifile[variant], manifest[variant], cmdline_base[variant],
        cmdline_img[variant]
    ]
    mkbootfs_checked(args, zircon_project)

  return outputs


def generate_system_bootfs(platform: str, product: str, manifest: dict, zircon_project: str) -> dict:
  outputs = {
      'debug': 'system_bootfs_{}-{}.bin'.format(platform, product),
      'release': 'system_bootfs_{}-{}-rel.bin'.format(platform, product),
  }
  depfile = {
      'debug': 'system_bootfs_{}-{}.bin.d'.format(platform, product),
      'release': 'system_bootfs_{}-{}-rel.bin.d'.format(platform, product),
  }
  for variant in ['debug', 'release']:
    args = [
        '-o', outputs[variant], '--depfile', depfile[variant], manifest[variant]
    ]
    mkbootfs_checked(args, zircon_project)

  return outputs


def generate_bootdata_fs(platform: str, product: str, boot_bootfs: str,
                         system_bootfs: str, zircon_project: str) -> dict:
  outputs = {
      'debug': 'bootdata-fs-{}-{}.bin'.format(platform, product),
      'release': 'bootdata-fs-{}-{}-rel.bin'.format(platform, product),
  }
  depfile = {
      'debug': 'bootdata-fs-{}-{}.bin.d'.format(platform, product),
      'release': 'bootdata-fs-{}-{}-rel.bin.d'.format(platform, product),
  }
  for variant in ['debug', 'release']:
    args = [
        '-o', outputs[variant], '--depfile', depfile[variant],
        boot_bootfs[variant], system_bootfs[variant]
    ]
    mkbootfs_checked(args, zircon_project)

  return outputs


def generate_combo_image(platform: str, product: str, bootdata_fs: str,
                         bootdata_plid: str, zircon_project: str) -> dict:
  outputs = {
      'debug': 'bootdata-combo-{}-{}.bin'.format(platform, product),
      'release': 'bootdata-combo-{}-{}-rel.bin'.format(platform, product),
  }
  depfile = {
      'debug': 'bootdata-combo-{}-{}.bin.d'.format(platform, product),
      'release': 'bootdata-combo-{}-{}-rel.bin.d'.format(platform, product),
  }
  bootshim = {
      'debug':
          '../../build-zircon/build-{}/{}-boot-shim.bin'.format(zircon_project, platform),
      'release':
          '../../build-zircon-release/build-{}/{}-boot-shim.bin'.format(
              zircon_project, platform),
  }
  zircon = {
      'debug': '../../build-zircon/build-{}/zircon.bin'.format(zircon_project),
      'release': '../../build-zircon-release/build-{}/zircon.bin'.format(zircon_project),
  }

  for variant in ['debug', 'release']:
    args = [
        '-o', outputs[variant], '--depfile', depfile[variant], '--header',
        bootshim[variant], '--header-align', '65536', zircon[variant],
        bootdata_fs[variant], bootdata_plid[variant]
    ]
    mkbootfs_checked(args, zircon_project)

  return outputs


def parse_args():
  parser = argparse.ArgumentParser(description='Generate product binary')
  parser.add_argument('--packages-home',
                      required=True,
                      help='a path to folder containing buildroot packages')
  return parser.parse_args()


def main():
  args = parse_args()
  assert os.path.exists(args.packages_home)

  packages_home = args.packages_home

  product_filename = os.path.join(packages_home, 'product.json')
  cmdline_filename = os.path.join(packages_home, 'cmdline.txt')

  with open(product_filename) as f:
    info = json.load(f)

  platform = info['platform']
  product = info['product']
  vid = info['vid']
  pid = info['pid']
  sbt = info['sbt']
  zircon_project = str(info['zircon_project'])

  packages = common.PackagesResolver().resolve(packages_home,
                                               'garnet/packages/' + zircon_project,
                                               os.getcwd())

  # switch to out/<zircon_project>/arm64 and simulate GN actions
  os.chdir('out/' + zircon_project + '/arm64')

  manifests = generate_finalize_manifests(platform, product, packages, zircon_project)

  cmdline_img = generate_cmdline_img(platform, product, cmdline_filename, zircon_project)
  bootfs_boot = generate_boot_bootfs(platform, product, cmdline_img,
                                     manifests['boot'], zircon_project)
  bootfs_system = generate_system_bootfs(platform, product, manifests['system'], zircon_project)
  bootdata_fs = generate_bootdata_fs(platform, product, bootfs_boot,
                                     bootfs_system, zircon_project)
  bootdata_plid = generate_plid_img(platform, product, vid, pid, sbt, zircon_project)

  bootdata_combo = generate_combo_image(platform, product, bootdata_fs, bootdata_plid, zircon_project)

  os.symlink(bootdata_combo['debug'], 'nebula.bin')
  os.symlink(bootdata_combo['release'], 'nebula-rel.bin')


if __name__ == '__main__':
  raise SystemExit(main())

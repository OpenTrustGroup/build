#!/usr/bin/env python3

import argparse
import manifest
import os
import typing


def append_manifest_entry(file: typing.TextIO,
                          entry: manifest.manifest_entry) -> None:
  line = ''
  if entry.in_image:
    line += '[' + entry.in_image + ']'
  if entry.group:
    line += '{' + entry.group + '}'
  line += entry.target + '=' + entry.source + '\n'
  file.write(line)


def emit_manifests(filename, manifests):
  os.makedirs(os.path.dirname(filename), exist_ok=True)
  with open(filename, 'w') as file:
    for entry in manifests:
      append_manifest_entry(file, entry)


def read_manifest_file(filename, from_cwd, to_cwd):
  result = []
  with open(filename, 'r') as file:
    manifest_entries = manifest.read_manifest_file(file, from_cwd, to_cwd)
    for entry in manifest_entries:
      result.append(entry)
  return result


def parse_args():
  parser = argparse.ArgumentParser(
      description='Convert paths in boot.manifest to buildroot relative path')
  parser.add_argument('--manifest-file', required=True)
  parser.add_argument('--from-cwd', required=True)
  parser.add_argument('--to-cwd', required=True)
  parser.add_argument('--out', required=True)
  return parser.parse_args()


def main():
  args = parse_args()
  assert os.path.exists(args.manifest_file)

  entries = read_manifest_file(args.manifest_file, args.from_cwd, args.to_cwd)
  emit_manifests(args.out, entries)


if __name__ == '__main__':
  raise SystemExit(main())

#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Says which build a component came out of.

A customer reports something against "the libddk you sent in March".  Without
this, answering means guessing which build that was.

Three fields, because a timestamp on its own does not identify anything:

  revisions    the commit each repository was at.  This is the identity: it
               says exactly what went in, and it can be checked out again.
  source_date  the newest of those commits.  Derived from the revisions, so
               two builds of the same source agree on it.
  built_at     when the export ran.  The only field that moves between two
               builds of identical source, which is why it says where it came
               from: set SOURCE_DATE_EPOCH and it is pinned, leave it and it
               is the clock.

`dirty` is recorded per repository and it matters more than the rest.  A
component built from a tree with uncommitted changes cannot be traced to
anything, and finding that out while chasing a customer's bug is late.

`toolchain` is the fourth field, and it is not derivable from the other
three.  The archives here were produced by one specific clang; their C++
symbols carry that clang's libc++ ABI namespace, and a consumer building with
a different one gets undefined references across the whole delivery.  Nothing
in the revisions says which clang that was -- the prebuilt is downloaded, not
committed -- so the CIPD stamp that pins it is recorded here.  Without it, a
report of "the March libddk does not link any more" cannot be told apart from
an ABI change, which is the one question ddk_version.py exists to answer.
"""

from __future__ import print_function

import datetime
import json
import os
import subprocess

FORMAT_VERSION = 1
BUILD_INFO_FILE = 'BUILD_INFO.json'


def _git(repository, *arguments):
    command = ['git', '-C', repository] + list(arguments)
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    out, _ = process.communicate()
    if process.returncode != 0:
        return None
    return out.decode('utf-8', 'replace').strip()


def repositories_for(source_root, sources):
    """The checkouts that contributed a file, and nothing else.

    Not every checkout under the source root: this tree holds a hundred of
    them, most of them Linux trees and vendor drops that no part of the
    component comes from.  Recording those would bury the eight that matter
    and make every unrelated commit look like a change to the delivery.
    """
    root = os.path.abspath(source_root)
    found = set()
    for source in sources:
        path = os.path.dirname(os.path.abspath(source))
        while path.startswith(root) and len(path) >= len(root):
            if os.path.exists(os.path.join(path, '.git')):
                relative = os.path.relpath(path, root).replace(os.sep, '/')
                found.add('.' if relative == '.' else relative)
                break
            if path == root:
                break
            path = os.path.dirname(path)
    return sorted(found)


def describe(source_root, repositories):
    """The revision of every repository, and whether it is clean."""
    root = os.path.abspath(source_root)
    names = repositories
    revisions = {}
    for name in names:
        path = root if name == '.' else os.path.join(root, name)
        commit = _git(path, 'rev-parse', 'HEAD')
        if commit is None:
            continue
        status = _git(path, 'status', '--porcelain')
        stamp = _git(path, 'log', '-1', '--format=%ct')
        revisions[name] = {
            'commit': commit,
            'dirty': bool(status),
            'committed_at': int(stamp) if stamp and stamp.isdigit() else 0,
        }
    return revisions


def toolchain_stamp(source_root):
    """The CIPD stamp of the pinned clang, and its release directory.

    Read from the same files zircon/prebuilt/config.mk pins the build with,
    rather than by running the compiler: the version string a binary prints
    says "7.0.0" for every build of that release, while the stamp is the one
    thing that distinguishes them.  Both are recorded, because the version is
    what a human recognises and the stamp is what actually identifies it.
    """
    root = os.path.abspath(source_root)
    stamp_path = os.path.join(root, 'zircon', 'prebuilt', 'downloads',
                              'clang.stamp')
    stamp = None
    if os.path.isfile(stamp_path):
        with open(stamp_path, 'rb') as handle:
            stamp = handle.read().decode('ascii', 'replace').strip() or None

    releases = os.path.join(root, 'zircon', 'prebuilt', 'downloads', 'clang',
                            'lib', 'clang')
    release = None
    if os.path.isdir(releases):
        found = sorted(name for name in os.listdir(releases)
                       if os.path.isdir(os.path.join(releases, name)))
        # Exactly one, or none: two releases side by side means the pin no
        # longer says which one a build used, and recording either would be a
        # guess dressed up as a record.
        if len(found) == 1:
            release = found[0]

    if stamp is None and release is None:
        return None
    return {'clang_stamp': stamp, 'clang_release': release}


def _utc(seconds):
    moment = datetime.datetime.utcfromtimestamp(int(seconds))
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def build_info(component, source_root, repositories, now=None):
    revisions = describe(source_root, repositories)
    newest = max([r['committed_at'] for r in revisions.values()] or [0])

    pinned = os.environ.get('SOURCE_DATE_EPOCH')
    if pinned and pinned.strip().isdigit():
        built = int(pinned.strip())
        origin = 'SOURCE_DATE_EPOCH'
    elif now is not None:
        built = int(now)
        origin = 'clock'
    else:
        import time
        built = int(time.time())
        origin = 'clock'

    dirty = sorted(name for name in revisions if revisions[name]['dirty'])
    return {
        'format_version': FORMAT_VERSION,
        'component': component,
        'built_at': _utc(built),
        'built_at_source': origin,
        'source_date': _utc(newest) if newest else None,
        'dirty': dirty,
        'revisions': revisions,
        'toolchain': toolchain_stamp(source_root),
    }


def write(component_dir, component, source_root, repositories, now=None):
    info = build_info(component, source_root, repositories, now)
    path = os.path.join(component_dir, BUILD_INFO_FILE)
    with open(path, 'w') as handle:
        json.dump(info, handle, indent=2, sort_keys=True,
                  separators=(',', ': '))
        handle.write('\n')
    return info

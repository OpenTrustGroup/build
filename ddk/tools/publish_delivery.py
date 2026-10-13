#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Turns a finished delivery into the things MTK actually receive.

Every part is kept under `repo`, so every part leaves here as a git
repository, and a note ties them together:

    libddk         a git repository: the component, the compiler inside it
    grt_ddk        a git repository, one commit per release, tagged
    mtk_ddk        a git repository, our commits replayed into it
    RELEASE.txt    the release id and each part's revision

There was a tar of the component for a while, unpacked over the workspace
root, because 178 MB of binaries in a `repo sync` is a real cost.  MTK asked
for a directory instead: one way to fetch a delivery, one tool, and a
component that arrives with the halves rather than beside them.

Every part is handled by the same path even though they hold different
histories.  What this does to each is the same: make it equal to the part
that shipped, commit if that changed anything, tag it with the release.  For
mtk_ddk sync_mirror.py has already replayed our commits and the tree is
equal, so only the tag is new; for the others the commit is the release and
there is nothing else to say about it.

The note is still the part that has to be right: nothing in git ties three
repositories together, so the id and the revisions are written down in one
place, and every part carries the id as well.
"""

from __future__ import print_function

import argparse
import errno
import json
import os
import shutil
import subprocess
import sys

import layout as layout_

LIBDDK_NAME = 'libddk'
RELEASE_NAME = 'RELEASE'
BUILD_INFO_NAME = 'BUILD_INFO.json'
NOTE_NAME = 'RELEASE.txt'
TAG_PREFIX = 'release/'


class PublishError(Exception):
    """A handover that would not be reproducible."""


def _fail(message):
    raise PublishError(message)


def _makedirs(path):
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise


def _git(repo, *arguments, **kwargs):
    process = subprocess.Popen(['git', '-C', repo] + list(arguments),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = process.communicate()
    if process.returncode != 0 and not kwargs.get('allow_failure'):
        _fail('git %s failed in %s: %s'
              % (arguments[0], repo, err.decode('utf-8', 'replace').strip()))
    return out.decode('utf-8', 'replace')


def read_release(delivery, layout=None):
    """The id every part of the delivery carries, or an error."""
    seen = {}
    for name in layout_.parts(delivery, layout):
        path = os.path.join(layout_.part_dir(delivery, name, layout),
                            RELEASE_NAME)
        if os.path.isfile(path):
            with open(path) as handle:
                seen[name] = handle.read().strip()
    if not seen:
        _fail('%s carries no %s: it was not made by make_delivery.py'
              % (delivery, RELEASE_NAME))
    if len(set(seen.values())) != 1:
        _fail('the parts of %s disagree about which release they are: %s'
              % (delivery, ', '.join('%s=%s' % item
                                     for item in sorted(seen.items()))))
    return list(seen.values())[0]


# ---------------------------------------------------------------------------
# the repositories


def release_log(part, identifier):
    """What a release commit should say, for a part that is built output.

    `Release <id>` alone says when but not from what.  A component is built
    from a tree and a compiler, and the question asked of it months later is
    which ones -- so the commit carries the revisions its BUILD_INFO records.

    Only a part that has a BUILD_INFO has this.  The source halves do not:
    their history is the commits themselves, where the same question is
    answered by reading them.
    """
    path = os.path.join(part, BUILD_INFO_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as handle:
            info = json.load(handle)
    except (ValueError, IOError):
        return None

    lines = ['Release %s' % identifier, '']
    for name in sorted(info.get('revisions', {})):
        entry = info['revisions'][name] or {}
        commit = (entry.get('commit') or '')[:12]
        if commit:
            lines.append('%-7s %s%s' % (name, commit,
                                        '  (dirty)' if entry.get('dirty')
                                        else ''))
    toolchain = info.get('toolchain') or {}
    if toolchain.get('clang_release'):
        lines.append('%-7s %s  (%s)' % ('clang', toolchain['clang_release'],
                                        (toolchain.get('clang_stamp')
                                         or '')[:12]))
    if info.get('built_at'):
        lines.append('%-7s %s' % ('built', info['built_at']))
    return '\n'.join(lines).rstrip('\n') + '\n'


def tag_in_place(half, identifier, identity=None, message=None):
    """Commits what the delivery added to a half that is already a repository.

    mtk_ddk ships as a git repository: MTK work in it, so they need our
    commits rather than a fresh tree every release, and the history is part of
    what is handed over.  By the time this runs the tree is the delivery and
    the only thing outstanding is the RELEASE the stamp just wrote, so this
    commits that and tags it.

    libddk is a repository on the same terms and for a different reason: it
    is built output, so it has no commits to replay and its history is one
    commit per release.  That is what `message` is for -- see release_log.

    Nothing is copied.  Copying a repository somewhere else to tag it there
    would leave the one that shipped untagged, which is the one anybody would
    look at.
    """
    _git(half, 'add', '-A')
    if _git(half, 'status', '--porcelain').strip():
        _git(half, 'commit', '-q', '-m', message or 'Release %s' % identifier)
    return _tag(half, identifier)


def _tag(repo, identifier):
    """Puts the release tag on HEAD, or refuses to move one that exists."""
    tag = TAG_PREFIX + identifier
    head = _git(repo, 'rev-parse', 'HEAD').strip()
    if _git(repo, 'tag', '-l', tag).strip():
        if _git(repo, 'rev-list', '-1', tag).strip() != head:
            _fail('%s already has %s, on a different commit. A release is '
                  'made once; publishing it again over something else is how '
                  'two things end up with one name.' % (repo, tag))
    else:
        _git(repo, 'tag', tag)
    return head


def publish_part(half, repo, identifier, identity=None):
    """Makes `repo` equal to `half`, commits what changed, tags the release.

    Equal, not merged: the delivery is what was built and reviewed, and a
    repository that is nearly it is a repository nobody checked.  For a part
    whose history is replayed elsewhere this changes nothing and only tags;
    for one that has none it is the release, in a single commit.
    """
    if not os.path.isdir(half):
        _fail('not a directory: %s' % half)
    if not os.path.isdir(os.path.join(repo, '.git')):
        _makedirs(repo)
        _git(repo, 'init', '-q')
    if identity:
        name, _, mail = identity.partition(' <')
        _git(repo, 'config', 'user.name', name.strip())
        _git(repo, 'config', 'user.email', mail.rstrip('>').strip())

    for current, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d != '.git']
        for name in names:
            path = os.path.join(current, name)
            relative = os.path.relpath(path, repo)
            if relative == '.gitignore':
                continue
            if not os.path.exists(os.path.join(half, relative)):
                os.remove(path)
    for current, dirs, names in os.walk(half):
        dirs.sort()
        for name in sorted(names):
            source = os.path.join(current, name)
            target = os.path.join(repo, os.path.relpath(source, half))
            _makedirs(os.path.dirname(target))
            shutil.copyfile(source, target)
            shutil.copystat(source, target)

    _git(repo, 'add', '-A')
    if _git(repo, 'status', '--porcelain').strip():
        _git(repo, 'commit', '-q', '-m',
             'Release %s' % identifier)
    return _tag(repo, identifier)


# ---------------------------------------------------------------------------
# the note


def write_note(out, identifier, revisions, delivery, layout=None):
    """Everything needed to fetch this release, in one file."""
    _makedirs(out)
    lines = [
        'DDK release %s' % identifier,
        '',
        'Every part carries this id in its own RELEASE file, and the build',
        'refuses to start if they disagree. Nothing in git ties three',
        'repositories together, so this is where they are tied together.',
        '',
        'under repo, all three at the same tag:',
    ]
    for name in sorted(revisions):
        lines.append('  %-10s %s' % (name, revisions[name]))
        lines.append('  %-10s tag %s%s' % ('', TAG_PREFIX, identifier))
    lines += [
        '',
        'libddk is the component, the compiler inside it. Each half carries',
        'its own Makefile and is built on its own:',
        '',
        '  make -C grt_ddk',
        '  make -C mtk_ddk',
    ]

    info = os.path.join(layout_.part_dir(delivery, LIBDDK_NAME, layout),
                        'toolchain', 'TOOLCHAIN_INFO.json')
    if os.path.isfile(info):
        import json
        with open(info) as handle:
            record = json.load(handle)
        lines += [
            '',
            'the compiler ships inside the component. what the build host',
            'still has to have:',
        ]
        for name in record.get('host_requirements', []):
            lines.append('  %s' % name)
        lines.append('  glibc 2.15 or newer, x86-64')

    path = os.path.join(out, NOTE_NAME)
    with open(path, 'w') as handle:
        handle.write('\n'.join(lines) + '\n')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delivery',
                        help='the finished delivery, from make_delivery.py. '
                             'Required except with --release-log, which reads '
                             'one part rather than a delivery.')
    parser.add_argument('--repo', action='append', default=[],
                        help='NAME=PATH: the git repository a part is '
                             'published into. Repeat.')
    parser.add_argument('--out', help='where to write the note')
    parser.add_argument('--release-log', dest='release_log_for',
                        help='print the release commit message for this part '
                             'and exit. Used when seeding a component history '
                             'from a delivery made before it had one.')
    parser.add_argument('--identity', help='committer, as "Name <mail>"')
    parser.add_argument('--layout', default='',
                        help='PART=DIR,...: where each part sits in the '
                             'delivery. A part not named sits at its name.')
    args = parser.parse_args()

    if args.release_log_for:
        part = args.release_log_for
        stamp = os.path.join(part, RELEASE_NAME)
        if not os.path.isfile(stamp):
            sys.stderr.write('error: %s carries no %s\n'
                             % (part, RELEASE_NAME))
            return 1
        with open(stamp) as handle:
            identifier = handle.read().strip()
        sys.stdout.write(release_log(part, identifier)
                         or 'Release %s\n' % identifier)
        return 0

    missing_arguments = [name for name, value
                         in (('--delivery', args.delivery), ('--out', args.out))
                         if not value]
    if missing_arguments:
        sys.stderr.write('error: %s required\n'
                         % ' and '.join(missing_arguments))
        return 1
    try:
        layout = layout_.parse(args.layout)
        parts = layout_.parts(args.delivery, layout)
        identifier = read_release(args.delivery, layout)
        revisions = {}
        # A part that is already a repository is tagged where it stands.
        # Only one that is not gets published into a repository of its own,
        # which is what the parts MTK consume rather than edit do.
        for name in parts:
            half = layout_.part_dir(args.delivery, name, layout)
            if os.path.isdir(os.path.join(half, '.git')):
                revisions[name] = tag_in_place(half, identifier,
                                               args.identity,
                                               release_log(half, identifier))
        for spelling in args.repo:
            if '=' not in spelling:
                _fail('--repo wants NAME=PATH, got %r' % spelling)
            name, _, repo = spelling.partition('=')
            if name in revisions:
                _fail('%s is already a repository in the delivery; publishing '
                      'it into %s as well would leave the one that ships '
                      'untagged' % (name, repo))
            if name not in parts:
                _fail('%s is not a part of this delivery' % name)
            revisions[name] = publish_part(
                layout_.part_dir(args.delivery, name, layout), repo,
                identifier, args.identity)
        missing = sorted(set(parts) - set(revisions))
        if missing:
            _fail('%s of this delivery would not be published: %s. Every part '
                  'is a repository MTK sync, so one left behind is a part '
                  'they cannot fetch.' % ('part(s)', ', '.join(missing)))
        note = write_note(args.out, identifier, revisions, args.delivery,
                          layout)
    except (PublishError, layout_.LayoutError) as error:
        sys.stderr.write('error: %s\n' % error)
        return 1

    for name in sorted(revisions):
        print('  %-10s %s  tag %s%s'
              % (name, revisions[name][:12], TAG_PREFIX, identifier))
    print('published %s -> %s' % (identifier, note))
    return 0


if __name__ == '__main__':
    sys.exit(main())

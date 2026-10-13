#!/usr/bin/env python
# Copyright 2026 GoldenRiver Technology Co., Ltd.
# SPDX-License-Identifier: BSD-3-Clause
"""What MTK receive between one release and the next.

A release produces three directories.  Two of them -- grt_ddk and mtk_ddk --
are git repositories that continue the last release's, so what MTK need in
order to move from the release they have to this one is the commits in
between, as patches they can apply and review.  The third, libddk, is built
artifacts: a patch over a .a is not something anybody can read, so it travels
whole, and what is said about it is which files changed.

Everything goes into one tarball, because a delivery that arrives as several
things arrives incompletely at least once.

The previous release is `output.last`, the copy run_release.sh keeps when it
moves the old output aside.  Without one -- the first release ever, or the
first after the directory was cleared -- there is nothing to diff against and
the whole delivery is the patch.  That case is reported, not guessed at.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile

import layout as layout_

RELEASE_NAME = 'RELEASE'
GIT_DIR = '.git'

# Built output rather than anybody's work.  It is a repository so that its
# releases have an order and a customer can see which one they hold, but a
# patch over a .a describes nothing a reviewer can read, so it ships whole
# however it is stored.
WHOLE_PARTS = frozenset(['libddk'])


class PackError(Exception):
    pass


def _fail(message):
    raise PackError(message)


def _git(repo, *arguments):
    process = subprocess.Popen(('git',) + arguments, cwd=repo,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = process.communicate()
    if process.returncode != 0:
        _fail('git %s in %s: %s' % (' '.join(arguments), repo,
                                    err.decode('utf-8', 'replace').strip()))
    return out.decode('utf-8', 'replace')


def is_repository(path):
    """Whether this directory is itself a repository.

    Asked of the directory, not of git.  `git -C somewhere` walks up until it
    finds a repository, so in a checkout every directory looks like one and
    the answer would be about the tree this release was built from rather
    than about the part.
    """
    return os.path.isdir(os.path.join(path, GIT_DIR))


def parts_of(delivery, layout=None):
    """The parts a delivery holds, which is what has to be accounted for: a
    part nobody packs is a part MTK cannot move forward."""
    if not os.path.isdir(delivery):
        _fail('no delivery at %s' % delivery)
    found = layout_.parts(delivery, layout)
    if not found:
        _fail('%s holds no parts' % delivery)
    return found


def patches_for(part, new, old, into):
    """The commits between the two releases of one repository.

    Anchored on the previous release's HEAD rather than on its tag: the HEAD
    is what was delivered, whatever it was called, and a tag can be missing
    from a delivery that was assembled but never published.
    """
    base = _git(old, 'rev-parse', 'HEAD').strip()
    head = _git(new, 'rev-parse', 'HEAD').strip()
    if base == head:
        return [], base, head
    # The base has to be in this release's history, or the two repositories
    # are unrelated and a patch series between them means nothing.
    try:
        _git(new, 'merge-base', '--is-ancestor', base, head)
    except PackError:
        _fail('%s: the last release (%s) is not an ancestor of this one (%s). '
              'The mirror was rebuilt instead of continued, so no patch from '
              'one to the other applies. Seed it from the last output/.'
              % (part, base[:12], head[:12]))
    os.makedirs(into)
    _git(new, 'format-patch', '--no-signature', '--zero-commit',
         '-o', into, '%s..%s' % (base, head))
    return sorted(os.listdir(into)), base, head


def _digest(path):
    sha = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 16), b''):
            sha.update(block)
    return sha.hexdigest()


def _files_under(root):
    found = {}
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != GIT_DIR)
        for name in sorted(names):
            full = os.path.join(current, name)
            relative = os.path.relpath(full, root).replace(os.sep, '/')
            found[relative] = full
    return found


def compare_trees(new, old):
    """Which files differ, by content rather than by timestamp."""
    theirs, ours = _files_under(old), _files_under(new)
    added = sorted(set(ours) - set(theirs))
    removed = sorted(set(theirs) - set(ours))
    changed = sorted(name for name in set(ours) & set(theirs)
                     if _digest(ours[name]) != _digest(theirs[name]))
    return added, removed, changed


def write_changes(path, added, removed, changed):
    lines = ['# What changed in libddk since the last release.',
             '# It ships whole -- these are the files to look at.', '']
    for label, names in (('added', added), ('removed', removed),
                         ('changed', changed)):
        lines.append('%s: %d' % (label, len(names)))
        lines.extend('  %s' % name for name in names)
        lines.append('')
    with open(path, 'w') as handle:
        handle.write('\n'.join(lines))


def pack(delivery, previous, out, release, identity=None, layout=None):
    staging = os.path.join(out, release)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    report = {'repositories': {}, 'whole': [], 'unchanged': []}
    notes = ['DDK release %s' % release, '']

    have_previous = previous and os.path.isdir(previous)
    if not have_previous:
        notes.append('There is no previous release to compare against, so '
                     'nothing here is a patch: every part travels whole.')
        notes.append('')

    for part in parts_of(delivery, layout):
        new = layout_.part_dir(delivery, part, layout)
        old = layout_.part_dir(previous, part, layout) if have_previous \
            else None

        if (part not in WHOLE_PARTS
                and is_repository(new) and old and is_repository(old)):
            into = os.path.join(staging, part)
            names, base, head = patches_for(part, new, old, into)
            report['repositories'][part] = names
            if names:
                notes.append('%s: %d patch(es), %s..%s'
                             % (part, len(names), base[:12], head[:12]))
            else:
                notes.append('%s: no commits since the last release' % part)
                report['unchanged'].append(part)
            continue

        # Not a repository, or no previous one to continue: it travels whole.
        if old and os.path.isdir(old):
            # _files_under skips .git, so a part that is a repository is
            # compared by what it delivers rather than by its own history.
            added, removed, changed = compare_trees(new, old)
            if not (added or removed or changed):
                notes.append('%s: identical to the last release, not packed'
                             % part)
                report['unchanged'].append(part)
                continue
            where = os.path.join(staging, part)
            os.makedirs(where)
            write_changes(os.path.join(where, 'CHANGES'),
                          added, removed, changed)
            notes.append('%s: whole, %d added / %d removed / %d changed '
                         '(see %s/CHANGES)'
                         % (part, len(added), len(removed), len(changed), part))
        else:
            where = os.path.join(staging, part)
            os.makedirs(where)
            notes.append('%s: whole' % part)

        # Archived at the part's place in the layout, so that unpacking the
        # tarball where the parts live puts it where the manifest expects.
        tarball = os.path.join(where, '%s-%s.tar.gz' % (part, release))
        with tarfile.open(tarball, 'w:gz') as archive:
            archive.add(new, arcname=layout_.where(part, layout),
                        filter=lambda info: None
                        if ('/%s/' % GIT_DIR) in info.name + '/'
                        else info)
        report['whole'].append(part)

    if identity:
        notes.append('')
        notes.append('Produced by %s' % identity)
    with open(os.path.join(staging, 'README'), 'w') as handle:
        handle.write('\n'.join(notes) + '\n')

    bundle = os.path.join(out, 'ddk-%s.tar.gz' % release)
    if os.path.exists(bundle):
        os.remove(bundle)
    with tarfile.open(bundle, 'w:gz') as archive:
        archive.add(staging, arcname=release)
    report['bundle'] = bundle
    report['notes'] = notes
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delivery', required=True,
                        help='this release: the output/ directory')
    parser.add_argument('--previous',
                        help='the release before it, normally output.last')
    parser.add_argument('--out', required=True, help='where the tarball goes')
    parser.add_argument('--release', help='the release id. Read from the '
                                          'delivery when not given.')
    parser.add_argument('--identity', help='who produced it')
    parser.add_argument('--layout', default='',
                        help='PART=DIR,...: where each part sits in the '
                             'delivery and the previous one')
    args = parser.parse_args()

    try:
        layout = layout_.parse(args.layout)
        release = args.release
        if not release:
            for part in parts_of(args.delivery, layout):
                stamp = os.path.join(
                    layout_.part_dir(args.delivery, part, layout),
                    RELEASE_NAME)
                if os.path.isfile(stamp):
                    with open(stamp) as handle:
                        release = handle.read().strip()
                    break
        if not release:
            _fail('no part of %s carries a %s and --release was not given'
                  % (args.delivery, RELEASE_NAME))
        if not os.path.isdir(args.out):
            os.makedirs(args.out)
        report = pack(args.delivery, args.previous, args.out, release,
                      args.identity, layout)
    except (PackError, layout_.LayoutError) as error:
        sys.stderr.write('error: %s\n' % error)
        return 1

    for line in report['notes'][2:]:
        if line:
            print('  %s' % line)
    print('packed %s' % report['bundle'])
    return 0


if __name__ == '__main__':
    sys.exit(main())

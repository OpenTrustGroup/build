#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Turns MTK's commits back into commits against our tree.

The other direction of sync_mirror.py.  MTK works in the mirror's layout and
sends back commits made against it; this reverses each one into ours and
writes it out as a patch we can apply and commit.

Per commit rather than as one diff, because their history is the thing worth
keeping: who changed what and why, in their words, is exactly what gets lost
when a return arrives as a tarball and somebody writes "sync from MTK" over
the top of it.

What makes the reversal trustworthy is not this file.  It is the invariant
partition.py holds -- rendering is deterministic and merging reverses it byte
for byte -- so the difference between two reversed trees is what MTK changed
and nothing else.  Without that, every rename and every rewritten include
would show up as their work.
"""

from __future__ import print_function

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

import partition
import sync_mirror
from sync_mirror import MirrorError, _fail, _git, _makedirs


def reverse_tree(part_dir, rules, out):
    """One mirror tree, back in our layout.

    A single part merges to a partial tree, which is what we want: the paths
    this half owns and no others.  The manifest inside it is what says how to
    undo the rename, so a mirror that lost it cannot be reversed -- and says
    so rather than guessing.
    """
    manifest = os.path.join(part_dir, partition.MANIFEST_NAME)
    if not os.path.isfile(manifest):
        _fail('%s has no %s, so nothing in it says how it was laid out'
              % (part_dir, partition.MANIFEST_NAME))
    return partition.merge([part_dir], out, rules)['entries']


def extract(mirror, revision, out):
    """The mirror's working tree at one commit."""
    _makedirs(out)
    source = subprocess.Popen(
        ['git', '-C', mirror, 'archive', revision],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sink = subprocess.Popen(['tar', '-x', '-C', out], stdin=source.stdout,
                            stderr=subprocess.PIPE)
    # See render_commit: python 2 will not communicate() past a closed pipe.
    source.stdout.close()
    unpack_error = sink.communicate()[1]
    archive_error = source.stderr.read()
    source.stderr.close()
    source.wait()
    if source.returncode != 0:
        _fail('could not read %s: %s'
              % (revision, archive_error.decode('utf-8', 'replace').strip()))
    if sink.returncode != 0:
        _fail('could not unpack %s: %s'
              % (revision, unpack_error.decode('utf-8', 'replace').strip()))
    return out


def inbound_message(text, rename=None):
    """Their commit message, as it should read against our tree.

    The reverse of the outbound rewrite, and only that.  Nothing is added:
    a note saying where it came from belongs in the commit we make from this,
    not inside somebody else's message.
    """
    if rename:
        source, target = rename['to'], rename['from']
        import re
        pattern = re.compile(r'(?<![A-Za-z0-9_./-])%s/' % re.escape(source))
        text = pattern.sub(target + '/', text)
    return text.rstrip('\n') + '\n'


def import_commits(mirror, rules, since, until, out, identity=None):
    """Each of their commits, reversed, as a patch against our tree."""
    rename = rules.get('rename')
    revisions = [line for line in
                 _git(mirror, 'rev-list', '--reverse',
                      '%s..%s' % (since, until)).split('\n') if line]
    if not revisions:
        return []

    staging = tempfile.mkdtemp(prefix='inbound-')
    try:
        # A scratch repository of reversed trees.  Committing each one and
        # letting git produce the patches is what keeps author, date and
        # message attached to the change they belong to; a diff computed by
        # hand would have to carry them separately and could lose the pairing.
        scratch = os.path.join(staging, 'ours')
        _makedirs(scratch)
        _git(scratch, 'init', '-q')
        if identity:
            name, _, email = identity.partition(' <')
            _git(scratch, 'config', 'user.name', name.strip())
            _git(scratch, 'config', 'user.email', email.rstrip('>').strip())

        base = os.path.join(staging, 'base')
        extract(mirror, since, base)
        reverse_tree(base, rules, os.path.join(staging, 'reversed'))
        _sync_into(os.path.join(staging, 'reversed'), scratch)
        _git(scratch, 'add', '-A')
        _git(scratch, 'commit', '-q', '-m', 'base')

        made = []
        for revision in revisions:
            tree = os.path.join(staging, 'tree')
            reversed_tree = os.path.join(staging, 'reversed')
            shutil.rmtree(tree, ignore_errors=True)
            extract(mirror, revision, tree)
            reverse_tree(tree, rules, reversed_tree)
            _sync_into(reversed_tree, scratch)
            _git(scratch, 'add', '-A')
            if not _git(scratch, 'status', '--porcelain').strip():
                continue
            message = inbound_message(
                _git(mirror, 'log', '-1', '--format=%B', revision), rename)
            author = _git(mirror, 'log', '-1', '--format=%an <%ae>',
                          revision).strip()
            date = _git(mirror, 'log', '-1', '--format=%aI', revision).strip()
            handle, path = tempfile.mkstemp(prefix='inbound-msg-')
            try:
                with os.fdopen(handle, 'w') as out_file:
                    out_file.write(message)
                _git(scratch, 'commit', '-q', '--author', author,
                     '--date', date, '-F', path)
            finally:
                os.remove(path)
            made.append(revision)

        if made:
            _makedirs(out)
            _git(scratch, 'format-patch', '--quiet', '-o',
                 os.path.abspath(out), 'HEAD~%d..HEAD' % len(made))
        return made
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _sync_into(source, destination):
    """Mirrors `source` onto `destination`, leaving .git alone."""
    for current, dirs, names in os.walk(destination):
        dirs[:] = [d for d in dirs if d != '.git']
        for name in names:
            path = os.path.join(current, name)
            relative = os.path.relpath(path, destination)
            if not os.path.exists(os.path.join(source, relative)):
                os.remove(path)
    for current, dirs, names in os.walk(source):
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(current, name)
            relative = os.path.relpath(path, source)
            target = os.path.join(destination, relative)
            _makedirs(os.path.dirname(target))
            shutil.copyfile(path, target)


def declared_in(patches):
    """Module descriptions the patches add, which our build does not make.

    Read out of the patches rather than out of a tree, because that is what
    the person running this is about to apply: `--- /dev/null` on a
    description is the sender declaring a module, and it is the last moment
    anybody can act on it before the file is committed and then silently
    overwritten by the next build.
    """
    found = []
    for name in sorted(os.listdir(patches)):
        if not name.endswith('.patch'):
            continue
        previous = ''
        with open(os.path.join(patches, name)) as handle:
            for line in handle:
                if (line.startswith('+++ b/')
                        and line.rstrip('\n').endswith(partition.MODULE_SUFFIX)
                        and previous.startswith('--- /dev/null')):
                    found.append(line[len('+++ b/'):].rstrip('\n'))
                previous = line
    return sorted(set(found))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mirror', required=True,
                        help="the repository MTK returned")
    parser.add_argument('--rules', required=True, help='the partition rules')
    parser.add_argument('--from', dest='since', required=True,
                        help='the commit we last had, exclusive')
    parser.add_argument('--to', default='HEAD', help='up to here')
    parser.add_argument('--out', required=True,
                        help='where to write the patches, in our layout')
    parser.add_argument('--identity',
                        help='committer for the scratch repository, as '
                             '"Name <mail>"')
    args = parser.parse_args()

    try:
        rules = partition.read_rules(args.rules)
        made = import_commits(args.mirror, rules, args.since, args.to,
                              args.out, args.identity)
    except (MirrorError, partition.PartitionError) as error:
        sys.stderr.write('error: %s\n' % error)
        return 1

    if not made:
        print('nothing to import: no commit in that range changed the half')
        return 0
    for revision in made:
        print('  %s' % revision[:12])
    print('imported %d commit(s) -> %s' % (len(made), args.out))
    for line in partition.describe_declarations(declared_in(args.out)):
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())

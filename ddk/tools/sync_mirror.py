#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Replays our commits into the repository MTK works in.

MTK keeps their half under their own git.  Handing them a fresh tree every
release would clobber whatever they had in flight, so what they need is our
changes as commits they can apply.  This is where those come from.

The mirror is a git repository holding exactly what a half's delivery
contains, in the delivered layout.  It is the interchange format, and both
directions go through it:

    garnet  --render-->  mirror  --format-patch-->  MTK
    garnet  <--merge--   mirror  <--their patch--   MTK

Which makes the round trip mean something is the invariant partition.py
already holds: rendering is deterministic and merging reverses it byte for
byte.  A diff taken after a return is therefore what MTK changed, with no
noise from the rename or the include rewriting.

Three things this does not do, each for a reason:

  It does not sanitise commit messages beyond dropping Change-Id, because we
  work closely enough with MTK that our authors and our ticket numbers are
  not the problem.  Change-Id goes because two different commits sharing one
  would confuse Gerrit if a change ever comes back, which is a correctness
  problem rather than a disclosure one.

  It does not mirror the default part.  A part defined by explicit prefixes
  can be extracted from a commit without a build; the default part is
  everything else, which is the whole repository.  MTK does not send patches
  for our half, so there is nothing to mirror.

  It does not decide what the delivery contains.  The delivery does, and the
  mirror is made to equal it -- the replay supplies the granularity, the
  reconcile supplies the truth, and the two are checked against each other
  before anything is written down.
"""

from __future__ import print_function

import argparse
import errno
import os
import re
import shutil
import subprocess
import sys
import tempfile

import partition

# Inside .git, not beside the sources.  The mirror's working tree has to be
# the delivered half and nothing else -- it is handed over as it stands -- and
# a file beside the sources would either show up in MTK's `git status` or need
# a .gitignore commit of its own in the history they read.  Git's own
# directory is the one place that is neither.
STATE_NAME = os.path.join('.git', 'mirror-state.json')
FORMAT_VERSION = 1

# Gerrit's, and only Gerrit's.  Two commits carrying one Change-Id is a
# collision Gerrit resolves by guessing, and a change that goes out and comes
# back would produce exactly that.
CHANGE_ID = re.compile(r'^Change-Id:[ \t]*I[0-9a-f]+[ \t]*$\n?', re.M)


class MirrorError(Exception):
    """A sync that would not produce the delivery."""


def _fail(message):
    raise MirrorError(message)


def _git(repo, *arguments, **kwargs):
    command = ['git', '-C', repo] + list(arguments)
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               stdin=kwargs.get('stdin'))
    out, err = process.communicate(kwargs.get('input'))
    if process.returncode != 0 and not kwargs.get('allow_failure'):
        _fail('%s failed: %s' % (' '.join(command),
                                 err.decode('utf-8', 'replace').strip()))
    return out.decode('utf-8', 'replace')


def _makedirs(path):
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise


# ---------------------------------------------------------------------------
# the message MTK reads


def outbound_message(text, rename=None):
    """Our commit message, as it should read in the delivered layout.

    Two changes and no more.  Change-Id goes, for the collision above.  Paths
    are rewritten because a message saying `garnet/drivers/i2c/...` points at
    a directory MTK's tree does not have -- that is a message that misleads,
    not a message that discloses.

    Only a path is rewritten.  `garnet:` as a subject tag is a module name,
    not a location, and it stays: it is how our own log reads and MTK reads
    the same log.
    """
    text = CHANGE_ID.sub('', text)
    if rename:
        source, target = rename['from'], rename['to']
        pattern = re.compile(r'(?<![A-Za-z0-9_./-])%s/' % re.escape(source))
        text = pattern.sub(target + '/', text)
    return text.rstrip('\n') + '\n'


# ---------------------------------------------------------------------------
# the mirror's own record


def read_state(mirror):
    path = os.path.join(mirror, STATE_NAME)
    if not os.path.isfile(path):
        return None
    with open(path, 'r') as handle:
        import json
        return json.load(handle)


def write_state(mirror, part, digest, revision):
    import json
    state = {
        'format_version': FORMAT_VERSION,
        'part': part,
        'rules_digest': digest,
        'garnet_revision': revision,
    }
    with open(os.path.join(mirror, STATE_NAME), 'w') as handle:
        json.dump(state, handle, indent=2, sort_keys=True,
                  separators=(',', ': '))
        handle.write('\n')
    return state


def check_state(mirror, part, digest):
    """Refuses a mirror that was built for something else.

    A mirror synced under different ownership rules -- or a different rename
    -- holds patches in a layout that no longer exists.  Applying ours on top
    would produce a tree that is neither.
    """
    state = read_state(mirror)
    if state is None:
        return None
    if state.get('format_version') != FORMAT_VERSION:
        _fail('%s: unsupported mirror format' % mirror)
    if state.get('part') != part:
        _fail('%s holds %r, not %r' % (mirror, state.get('part'), part))
    if state.get('rules_digest') != digest:
        _fail('%s was synced under different partition rules; the patches in '
              'it are laid out the old way and ours would not sit on top'
              % mirror)
    return state


# ---------------------------------------------------------------------------
# rendering one commit


def part_prefixes(rules, part):
    """The paths to pull out of a commit for this part, or none for all of it.

    A part that names prefixes is read out of a commit by them.  The default
    part names none -- it is whatever the rules do not assign elsewhere -- so
    there is nothing to limit `git archive` to and the whole scope comes out.
    Either way what is delivered is decided afterwards, by partition.split:
    the prefixes are how little has to be unpacked, not what the part owns.
    """
    if part == rules['default_part']:
        return []
    prefixes = [entry['prefix'] for entry in rules['rules']
                if entry['part'] == part]
    if not prefixes:
        _fail('%r owns no prefix in these rules' % part)
    return sorted(prefixes)


def touches_part(repo, revision, rules, part, scope=''):
    """Whether this commit changed anything the part owns.

    For a part with prefixes `git rev-list -- <prefixes>` answers this and
    answers it faster.  The default part has none, and asking git for every
    commit in range would replay the ones that only ever touched the other
    half.  So each commit's files are read and the rules asked who owns
    them, which is the same question the split answers later.
    """
    files = _git(repo, 'show', '--format=', '--name-only', revision).split('\n')
    for name in files:
        name = name.strip()
        if not name:
            continue
        if scope:
            if name == scope:
                continue
            if name.startswith(scope + '/'):
                name = name[len(scope) + 1:]
        owner, _ = partition.owner_of(name, rules)
        if owner == part:
            return True
    return False


def render_commit(repo, revision, rules, part, scope, out):
    """The part's tree at `revision`, in the delivered layout.

    Straight out of git: the files a half delivers are the repository's, bar
    the module descriptions a build generates, so no build is needed to know
    what a half looked like at any commit.

    A part's prefixes are the ones it owns now, and a commit old enough need
    not have them all: mt8678_i2c arrived long after ufs did.  `git archive`
    fails the whole command on a pathspec that matches nothing, so asking for
    every prefix would lose the commits made before the last one appeared --
    31 of mtk_ddk's 45, mirroring from 2025.  The prefixes are narrowed to
    the ones this commit has.

    None of them is the part having held nothing yet, which `rev-list` lets
    through for a commit that removed the last of them.  It returns rather
    than archiving: `git archive <rev> --` with no pathspec left is a request
    for the whole tree, and the half would swallow the other one.
    """
    prefixes = part_prefixes(rules, part)
    if prefixes:
        prefixes = [name.strip() for name
                    in _git(repo, 'ls-tree', '--name-only', revision, '--',
                            *prefixes).split('\n') if name.strip()]
        if not prefixes:
            return 0
    staging = tempfile.mkdtemp(prefix='mirror-render-')
    try:
        tree = os.path.join(staging, scope) if scope else staging
        _makedirs(tree)
        # Piped, not buffered through python: git archive writes a tar, and
        # a tar decoded to text and re-encoded is a tar with holes in it.
        command = ['git', '-C', repo, 'archive', revision, '--']
        command += prefixes
        source = subprocess.Popen(command, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        sink = subprocess.Popen(['tar', '-x', '-C', tree],
                                stdin=source.stdout,
                                stderr=subprocess.PIPE)
        # Not communicate() on the source: python 2 polls every pipe it
        # opened, including the stdout just closed to let git see SIGPIPE,
        # and raises on the closed one.
        source.stdout.close()
        unpack_error = sink.communicate()[1]
        archive_error = source.stderr.read()
        source.stderr.close()
        source.wait()
        if source.returncode != 0:
            _fail('could not read %s from %s: %s'
                  % (revision, repo,
                     archive_error.decode('utf-8', 'replace').strip()))
        if sink.returncode != 0:
            _fail('could not unpack %s: %s'
                  % (revision, unpack_error.decode('utf-8', 'replace').strip()))
        written = partition.split(staging, rules, out, scope, True)
        return written.get(part, 0)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# the sync


def delivered_files(delivery):
    """What the delivery holds, which is what the mirror has to hold."""
    found = set()
    for current, dirs, names in os.walk(delivery):
        dirs[:] = sorted(d for d in dirs if d != '.git')
        for name in sorted(names):
            relative = os.path.relpath(os.path.join(current, name),
                                       delivery).replace(os.sep, '/')
            found.add(relative)
    if not found:
        _fail('%s is empty' % delivery)
    return found


def install(rendered, mirror, allowed):
    """Puts a rendered tree into the mirror, restricted to `allowed`.

    The restriction is the whole reason this is not an rsync.  A commit
    carries every file under the prefixes it touches -- BUILD.gn, rules.mk,
    the SoC variants this product does not build, seventeen of them here --
    and the delivery carries fifty.  Mirroring the prefix would hand MTK
    files their delivery does not have and patches that do not apply to it.
    """
    changed = 0
    seen = set()
    for current, dirs, names in os.walk(rendered):
        dirs.sort()
        for name in sorted(names):
            source = os.path.join(current, name)
            relative = os.path.relpath(source, rendered).replace(os.sep, '/')
            if relative not in allowed:
                continue
            seen.add(relative)
            target = os.path.join(mirror, relative)
            _makedirs(os.path.dirname(target))
            if not os.path.isfile(target) or not _same(source, target):
                shutil.copyfile(source, target)
                shutil.copystat(source, target)
                changed += 1

    # A file the delivery has and this commit does not: it was added later,
    # or it is generated.  Either way it is not this commit's to remove.
    return changed


def _same(left, right):
    import filecmp
    return filecmp.cmp(left, right, shallow=False)


def reconcile(delivery, mirror, allowed):
    """Makes the mirror equal the delivery, and says what it had to change.

    The replay gives MTK the granularity; this gives it the truth.  Anything
    it has to do is something the per-commit replay could not produce -- the
    module descriptions a build generates, most of the time -- and the
    difference is worth seeing rather than smoothing over.
    """
    changed = []
    for relative in sorted(allowed):
        source = os.path.join(delivery, relative)
        target = os.path.join(mirror, relative)
        if os.path.isfile(target) and _same(source, target):
            continue
        _makedirs(os.path.dirname(target))
        shutil.copyfile(source, target)
        shutil.copystat(source, target)
        changed.append(relative)

    for current, dirs, names in os.walk(mirror):
        dirs[:] = sorted(d for d in dirs if d != '.git')
        for name in sorted(names):
            path = os.path.join(current, name)
            relative = os.path.relpath(path, mirror).replace(os.sep, '/')
            if relative in allowed:
                continue
            os.remove(path)
            changed.append(relative)
    return sorted(set(changed))


def verify(delivery, mirror, allowed):
    """The mirror's tree is the delivery's, file for file and byte for byte.

    Checked rather than assumed, because everything downstream -- every patch
    MTK applies, every diff we take when one comes back -- is only as good as
    this being true.
    """
    problems = []
    for relative in sorted(allowed):
        target = os.path.join(mirror, relative)
        if not os.path.isfile(target):
            problems.append('missing from mirror: %s' % relative)
        elif not _same(os.path.join(delivery, relative), target):
            problems.append('differs: %s' % relative)
    for current, dirs, names in os.walk(mirror):
        dirs[:] = sorted(d for d in dirs if d != '.git')
        for name in sorted(names):
            relative = os.path.relpath(os.path.join(current, name),
                                       mirror).replace(os.sep, '/')
            if relative not in allowed:
                problems.append('not in the delivery: %s' % relative)
    return problems


# ---------------------------------------------------------------------------


def commits_touching(repo, since, until, prefixes, rules=None, part=None,
                     scope=''):
    """Our commits in range that changed anything the part owns, oldest first.

    With prefixes git does the filtering.  Without them -- the default part --
    every commit in range is read and the rules asked, because `rev-list` with
    no paths would hand back the other half's commits too.
    """
    arguments = ['rev-list', '--reverse']
    arguments.append('%s..%s' % (since, until) if since else until)
    if prefixes:
        arguments.append('--')
        arguments.extend(prefixes)
    found = [line for line in _git(repo, *arguments).split('\n') if line]
    if prefixes or rules is None or part is None:
        return found
    return [r for r in found if touches_part(repo, r, rules, part, scope)]


MIRRORED_FROM = 'Mirrored-From'


def commit_into(mirror, repo, revision, rename, message=None):
    """Commits the mirror's working tree, keeping who wrote it and when.

    A replayed commit names the revision it came from, as a trailer: it is
    how a deployment tells which of our commits a repository somewhere else
    already carries, without the Change-Id that outbound_message removes.
    """
    _git(mirror, 'add', '-A')
    if not _git(mirror, 'status', '--porcelain').strip():
        return None
    if message is None:
        message = outbound_message(
            _git(repo, 'log', '-1', '--format=%B', revision), rename)
        message = '%s\n%s: %s\n' % (
            message.rstrip('\n'), MIRRORED_FROM,
            _git(repo, 'rev-parse', revision).strip())
    author = _git(repo, 'log', '-1', '--format=%an <%ae>', revision).strip()
    date = _git(repo, 'log', '-1', '--format=%aI', revision).strip()
    handle, path = tempfile.mkstemp(prefix='mirror-msg-')
    try:
        with os.fdopen(handle, 'w') as out:
            out.write(message)
        _git(mirror, 'commit', '-q', '--author', author, '--date', date,
             '-F', path)
    finally:
        os.remove(path)
    return _git(mirror, 'rev-parse', 'HEAD').strip()


def sync(repo, rules, part, delivery, mirror, until, since=None,
         identity=None, scope=''):
    digest = partition.rules_digest(rules)
    rename = rules.get('rename')

    if not os.path.isdir(os.path.join(mirror, '.git')):
        _makedirs(mirror)
        _git(mirror, 'init', '-q')
        state = None
    else:
        state = check_state(mirror, part, digest)

    since = since or (state or {}).get('garnet_revision')
    # The manifest belongs in the mirror -- MTK receives it -- but not in
    # every commit: it lists the part's entries, so a per-commit copy would
    # rewrite it each time and bury the change that was actually made.  It
    # arrives with the other generated files, in the reconcile.
    allowed = delivered_files(delivery)
    per_commit = allowed - {partition.MANIFEST_NAME}
    prefixes = part_prefixes(rules, part)

    if identity:
        name, _, email = identity.partition(' <')
        _git(mirror, 'config', 'user.name', name.strip())
        _git(mirror, 'config', 'user.email', email.rstrip('>').strip())

    replayed = []
    for revision in commits_touching(repo, since, until, prefixes,
                                     rules, part, scope):
        rendered = tempfile.mkdtemp(prefix='mirror-part-')
        try:
            render_commit(repo, revision, rules, part, scope, rendered)
            install(os.path.join(rendered, part), mirror, per_commit)
        finally:
            shutil.rmtree(rendered, ignore_errors=True)
        made = commit_into(mirror, repo, revision, rename)
        if made:
            replayed.append((revision[:12], made[:12]))

    changed = reconcile(delivery, mirror, allowed)
    reconciled = None
    if changed:
        reconciled = commit_into(
            mirror, repo, until, rename,
            message='Release: what the build produces\n\n'
                    'The module descriptions come from BUILD.gn through the\n'
                    'build, and the manifest describes the half as a whole,\n'
                    'so neither belongs to any one commit above.\n\n'
                    + '\n'.join('  ' + name for name in changed[:40]) + '\n')

    problems = verify(delivery, mirror, allowed)
    if problems:
        _fail('the mirror is not the delivery:\n  %s'
              % '\n  '.join(problems[:20]))

    # Our bookkeeping -- which revision we last replayed, under which rules.
    # It lives in .git, so it is not in the tree MTK receive and not in the
    # history they read.
    write_state(mirror, part, digest,
                _git(repo, 'rev-parse', until).strip())
    return {'replayed': replayed, 'reconciled': changed,
            'files': len(allowed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='the garnet checkout')
    parser.add_argument('--rules', required=True, help='the partition rules')
    parser.add_argument('--part', required=True, help='the half to mirror')
    parser.add_argument('--delivery', required=True,
                        help='the delivered half; the mirror is made equal '
                             'to it')
    parser.add_argument('--mirror', required=True,
                        help='the git repository MTK works in')
    parser.add_argument('--to', default='HEAD', help='replay up to here')
    parser.add_argument('--from', dest='since',
                        help='replay from here, exclusive. Defaults to what '
                             'the mirror last recorded.')
    parser.add_argument('--scope', default='garnet',
                        help='the directory the rules are written against')
    parser.add_argument('--identity',
                        help='committer for the reconcile commit, as '
                             '"Name <mail>"')
    args = parser.parse_args()

    try:
        rules = partition.read_rules(args.rules)
        result = sync(args.repo, rules, args.part, args.delivery,
                      args.mirror, args.to, args.since, args.identity,
                      args.scope)
    except (MirrorError, partition.PartitionError) as error:
        sys.stderr.write('error: %s\n' % error)
        return 1

    for ours, theirs in result['replayed']:
        print('  %s -> %s' % (ours, theirs))
    print('mirror: %d commit(s) replayed, %d file(s) reconciled, %d file(s) '
          'in the delivery' % (len(result['replayed']),
                               len(result['reconciled']), result['files']))
    return 0


if __name__ == '__main__':
    sys.exit(main())

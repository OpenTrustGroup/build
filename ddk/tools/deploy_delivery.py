#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Puts a delivery up for review where MTK build from.

Each part of a delivery is a repo project on the Gerrit the yocto layers live
on -- yocto/src/hypervisor/libddk/aries_v1 and the two halves beside it.  A
release used to reach them by hand: copy the tree over a checkout, write a
commit message, push.  Every one of those steps is decided by what is
already in the delivery, so this does them.

For each part:

  base      what is on the branch, or the tip of the changes already up for
            review under our topic -- a delivery lands on top of the last one
            whether or not that has merged yet.
  deployed  the release the base carries, read from its RELEASE, and the
            RD commits it already holds, read from the history: a replayed
            commit names its garnet source in a Mirrored-From trailer, and a
            base imported whole names, through libddk's build record, the
            garnet revision it was built from.
  series    the RD commits in the delivery's repository that the base does
            not hold yet.  They are the RD's own, replayed by the mirror with
            their CR, ONES id and trailers intact, so they go up as they are,
            one change each, author kept.  What the pipeline generates on top
            -- module descriptions, licence records, the release stamp -- is
            one commit after them, under the ids of the newest RD commit.
  import    when nothing tells what the base holds -- the first delivery
            into an empty project -- the whole tree goes up as one change.

Nothing depends on tags or on the delivery repository's continuity: the
mirror names its sources, so a repository rebuilt from scratch replays the
same commits with the same trailers and the deployment reads the same.

Either way the tree that results is checked against the delivery byte for
byte, and the changes are written to --out as patches.  Nothing is pushed
unless --push says so; then it is to refs/for/<branch> under the topic, with
the RD's who wrote the commits added as reviewers, and what was pushed is
printed as the change URLs Gerrit answers with.  A push Gerrit refuses is
not retried and not forced: the refusal is printed with the RD's address and
the patches stay in --out, for a person to take up.  A delivery the projects
already hold is reported as up to date, and nothing happens.

Nothing here reads a local checkout of the yocto tree.  The repositories are
fetched from Gerrit into --work, and the only state that matters is theirs.
"""

from __future__ import print_function

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys

import layout as layout_

RELEASE_NAME = 'RELEASE'
BUILD_INFO_NAME = 'BUILD_INFO.json'
GIT_DIR = '.git'
# What a rebuild of the same tree changes: the stamp, the build's date, and
# the checksums that follow from them.  A delivery that differs from what is
# deployed in nothing but these is the same delivery.
BOOKKEEPING = frozenset([RELEASE_NAME, BUILD_INFO_NAME, 'SDK_CONTENTS.json'])
MIRRORED_FROM = re.compile(r'^Mirrored-From: ([0-9a-f]{40})$', re.M)
PARTS = ('libddk', 'grt_ddk', 'mtk_ddk')
# Who commits the release change: the same identity the release itself
# publishes under.  A default rather than an argument the caller has to
# quote, because gr-nebula's `execute` evals its command line and a `<`
# in an argument is a redirection by the time it gets there.
IDENTITY = 'GRT Release <release@goldenrivertek.com>'

# The subject line MTK's Commit-Check wants, and what the mirror replays.
CR_SUBJECT = re.compile(r'^\[([A-Za-z0-9]+)\]\s+\[([A-Za-z0-9-]+)\]\s+'
                        r'([^:]+):\s+(.+)$')
RELEASE_ID = re.compile(r'\b(\d{8}-[0-9a-f]{12})\b')
CHANGE_ID = re.compile(r'^Change-Id:.*$', re.M)
TRAILER = re.compile(r'^([A-Z][A-Za-z-]*): (.+)$')

# The pipeline's own commits, which carry no CR of their own.
GENERATED_SUBJECT = 'Release: what the build produces'
STAMP_SUBJECT = re.compile(r'^Release (\d{8}-[0-9a-f]{12})$')


class DeployError(Exception):
    pass


def _fail(message):
    raise DeployError(message)


def _git(repo, *arguments, **kwargs):
    process = subprocess.Popen(('git', '-C', repo) + arguments,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.PIPE)
    out, err = process.communicate(kwargs.get('stdin'))
    if process.returncode != 0 and not kwargs.get('allow_failure'):
        _fail('git %s failed in %s: %s'
              % (' '.join(arguments), repo,
                 err.decode('utf-8', 'replace').strip()))
    return out.decode('utf-8', 'replace')


def _text(value):
    return value if isinstance(value, type(u'')) else value.decode('utf-8')


# ---------------------------------------------------------------------------
# Gerrit


class Gerrit(object):
    """What the deployment asks Gerrit: where a project is, what is open."""

    def __init__(self, url, user=None):
        # ssh://host:port, with or without a user; the user given wins.
        match = re.match(r'^ssh://(?:([^@/]+)@)?([^:/]+)(?::(\d+))?/?$', url)
        if not match:
            _fail('not a Gerrit ssh url: %r' % url)
        self.user = user or match.group(1)
        self.host = match.group(2)
        self.port = match.group(3) or '29418'

    def project_url(self, project):
        who = self.user + '@' if self.user else ''
        return 'ssh://%s%s:%s/%s' % (who, self.host, self.port, project)

    def open_changes(self, project, branch, topic):
        """The open changes under the topic, newest first, with their
        current revision and its parents."""
        command = ['ssh', '-p', self.port,
                   ('%s@%s' % (self.user, self.host)) if self.user
                   else self.host,
                   'gerrit', 'query', '--format=JSON', '--current-patch-set',
                   'project:%s branch:%s topic:%s status:open'
                   % (project, branch, topic)]
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        out, err = process.communicate()
        if process.returncode != 0:
            _fail('gerrit query failed: %s'
                  % err.decode('utf-8', 'replace').strip())
        changes = []
        for line in out.decode('utf-8', 'replace').splitlines():
            record = json.loads(line)
            if 'number' not in record:
                continue
            patch = record.get('currentPatchSet') or {}
            changes.append({
                'number': int(record['number']),
                'subject': record.get('subject', ''),
                'url': record.get('url', ''),
                'revision': patch.get('revision'),
                'ref': patch.get('ref'),
                'parents': patch.get('parents') or [],
            })
        return changes


def user_from_git_config(email=None):
    """The Gerrit ssh user, which is the local part of the address git
    commits under: niyc@goldenrivertek.com pushes as niyc.  Read from the
    git configuration the caller already has, so nothing is configured
    twice."""
    if email is None:
        process = subprocess.Popen(['git', 'config', '--get', 'user.email'],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        out, _ = process.communicate()
        email = out.decode('utf-8', 'replace').strip()
    if not email or '@' not in email:
        return None
    return email.split('@', 1)[0].strip() or None


def chain_tip(changes):
    """The change no other open change builds on.

    Open changes under one topic on one branch form a chain, each on top of
    the last; the tip is the one whose revision is nobody's parent.  Two
    tips would mean two chains, and a delivery cannot land on both.
    """
    parents = set()
    for change in changes:
        parents.update(change['parents'])
    tips = [c for c in changes if c['revision'] not in parents]
    if len(tips) > 1:
        _fail('two chains of open changes, and a delivery can only land on '
              'one: %s' % ', '.join('%d %s' % (t['number'], t['subject'][:50])
                                     for t in tips))
    return tips[0] if tips else None


# ---------------------------------------------------------------------------
# reading the delivery


def read_release(part_dir):
    path = os.path.join(part_dir, RELEASE_NAME)
    if not os.path.isfile(path):
        _fail('%s carries no %s' % (part_dir, RELEASE_NAME))
    with io.open(path, encoding='utf-8') as handle:
        return handle.read().strip()


def release_named_by(message):
    """The release a commit without a RELEASE file was made for.

    A delivery imported before the halves were stamped says so only in its
    message.  The subject names the release it brings; a body may name the
    one it replaces first and the new one after, so the subject wins and
    the body's last mention is the fallback.
    """
    lines = message.rstrip('\n').split('\n')
    found = RELEASE_ID.findall(lines[0] if lines else '')
    if not found:
        found = RELEASE_ID.findall(message)
    return found[-1] if found else None


def mirrored_from(message):
    found = MIRRORED_FROM.findall(message)
    return found[-1] if found else None


def sources_held(repo, revision):
    """The garnet revisions the history up to `revision` says it replayed."""
    return set(MIRRORED_FROM.findall(_git(repo, 'log', '--format=%B',
                                          revision)))


def garnet_revision_of(clone, base):
    """The garnet revision a libddk base was built from, from its record."""
    text = _git(clone, 'show', '%s:%s' % (base, BUILD_INFO_NAME),
                allow_failure=True)
    if not text.strip():
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    garnet = (record.get('revisions') or {}).get('garnet') or {}
    return garnet.get('commit') if isinstance(garnet, dict) else garnet


def rd_commits(delivery_repo):
    """The delivery's history, oldest first: (revision, message, source)."""
    return [(revision, message, source)
            for revision, message, source, _ in dated_commits(delivery_repo)]


def dated_commits(delivery_repo):
    """The same, with each commit's author date, for ordering across
    repositories."""
    entries = []
    text = _git(delivery_repo, 'log', '--reverse',
                '--format=%H %at%n%B%x00')
    for chunk in text.split('\x00'):
        chunk = chunk.strip('\n')
        if not chunk:
            continue
        head, _, message = chunk.partition('\n')
        revision, _, stamp = head.partition(' ')
        entries.append((revision, message, mirrored_from(message),
                        int(stamp or 0)))
    return entries


def series_for(entries, held, garnet, garnet_base, deployed=None):
    """The RD commits the base does not hold, oldest first.

    Held by trailer first: the newest commit whose source the base names
    is the boundary, and everything after it is new.  A base that names
    none -- imported whole -- holds what the delivery had when it stamped
    the base's release, if its history still has that stamp; failing that,
    whatever its garnet revision reaches, which the garnet checkout can
    answer.  None known: None, and the caller imports.

    A source the garnet revision does not reach may still be held: the same
    change, cherry-picked or pushed again under another title, is a
    different commit with the same patch.  Only a commit whose patch the
    base has nowhere is new.
    """
    rd = [e for e in entries if e[2]]
    if held:
        newest = None
        for index, (revision, message, source) in enumerate(rd):
            if source in held:
                newest = index
        if newest is None:
            return None
        return rd[newest + 1:]
    if deployed:
        stamp = 'Release %s' % deployed
        stamped = None
        for index, (revision, message, source) in enumerate(entries):
            if message.split('\n')[0].strip() == stamp:
                stamped = index
        if stamped is not None:
            return [e for e in entries[stamped + 1:] if e[2]]
    if garnet and garnet_base:
        if subprocess.call(['git', '-C', garnet, 'cat-file', '-e',
                            garnet_base + '^{commit}'],
                           stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE) != 0:
            _fail('the base was built from garnet %s, which the garnet '
                  'checkout at %s does not have; fetch it and deploy again'
                  % (garnet_base[:12], garnet))
        new = []
        for revision, message, source in rd:
            code = subprocess.call(['git', '-C', garnet, 'merge-base',
                                    '--is-ancestor', source, garnet_base],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
            if code == 1:
                if not same_patch_held(garnet, source, garnet_base):
                    new.append((revision, message, source))
            elif code != 0:
                _fail('%s is not in the garnet checkout at %s'
                      % (source[:12], garnet))
        return new
    return None


def series_by_content(clone, delivery_repo, entries):
    """The RD commits the base does not hold, read off the base's tree.

    For a base nothing else can read: imported whole, and the delivery no
    longer has the stamp of its release (out/ddk made again) while libddk
    sits at another release.  The newest RD commit whose change the base
    already carries -- its diff comes off the base's tree in reverse -- is
    the boundary, and everything after it is new.  None when no RD commit
    is in the base, and the caller imports.
    """
    rd = [e for e in entries if e[2]]
    for index in range(len(rd) - 1, -1, -1):
        revision = rd[index][0]
        if not _git(delivery_repo, 'rev-list', '--parents', '-1',
                    revision).split()[1:]:
            continue
        # Bytes, not text: a source in another encoding has to reach
        # `git apply` as it is, or the check fails on the copy.
        process = subprocess.Popen(['git', '-C', delivery_repo, 'diff',
                                    '--binary', '--full-index',
                                    revision + '^', revision],
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        diff, err = process.communicate()
        if process.returncode != 0:
            _fail('git diff failed in %s: %s'
                  % (delivery_repo, err.decode('utf-8', 'replace').strip()))
        if not diff.strip():
            continue
        process = subprocess.Popen(['git', '-C', clone, 'apply', '--check',
                                    '-R', '-'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        process.communicate(diff)
        if process.returncode == 0:
            return rd[index + 1:]
    return None


def same_patch_held(garnet, source, garnet_base):
    """Whether garnet_base reaches a commit with the same patch as source.

    `git cherry` compares by patch id against what garnet_base has since
    the two diverged; `-` marks a commit it already holds.  A root commit
    has no parent to bound the range by, and is taken as not held.
    """
    try:
        text = _git(garnet, 'cherry', garnet_base, source, source + '^')
    except DeployError:
        return False
    return text.strip().startswith('-')


def is_dirty(part_dir):
    """A delivery built with --dirty names a tree nobody can check out."""
    path = os.path.join(part_dir, BUILD_INFO_NAME)
    if not os.path.isfile(path):
        return False
    with io.open(path, encoding='utf-8') as handle:
        return bool(json.load(handle).get('dirty'))


def files_under(root):
    found = {}
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != GIT_DIR)
        for name in sorted(names):
            path = os.path.join(current, name)
            relative = os.path.relpath(path, root).replace(os.sep, '/')
            found[relative] = path
    return found


def _digest(path):
    if os.path.islink(path):
        return 'link:' + os.readlink(path)
    sha = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 16), b''):
            sha.update(block)
    return sha.hexdigest()


def tree_differences(ours, theirs):
    """Files that differ between two trees, .git aside."""
    mine, other = files_under(ours), files_under(theirs)
    added = sorted(set(mine) - set(other))
    removed = sorted(set(other) - set(mine))
    changed = sorted(name for name in set(mine) & set(other)
                     if _digest(mine[name]) != _digest(other[name]))
    return added, removed, changed


def substantive(differences):
    """The differences that are not a rebuild's bookkeeping."""
    return tuple([name for name in group
                  if os.path.basename(name) not in BOOKKEEPING]
                 for group in differences)


# ---------------------------------------------------------------------------
# commit messages


def parse_message(text):
    """Subject, CR id, ONES id, feature and trailers of a commit message."""
    lines = text.rstrip('\n').split('\n')
    subject = lines[0] if lines else ''
    match = CR_SUBJECT.match(subject)
    trailers = {}
    for line in lines[1:]:
        found = TRAILER.match(line)
        if found:
            trailers.setdefault(found.group(1), []).append(found.group(2))
    return {
        'subject': subject,
        'cr': match.group(1) if match else None,
        'ones': match.group(2) if match else None,
        'module': match.group(3) if match else None,
        'feature': (trailers.get('Feature') or [None])[0],
        'trailers': trailers,
        'own': subject == GENERATED_SUBJECT or bool(STAMP_SUBJECT.match(
            subject)),
    }


def change_id_for(project, key):
    """A Change-Id that is the same every time this is deployed, so a
    second push updates the change rather than opening another."""
    digest = hashlib.sha1(('%s\n%s\n' % (project, key)).encode('utf-8'))
    return 'I' + digest.hexdigest()


def with_change_id(message, change_id):
    body = CHANGE_ID.sub('', message).rstrip('\n')
    return '%s\nChange-Id: %s\n' % (body, change_id)


def ids_for_own_commits(series_messages, history_messages):
    """The CR and ONES ids the pipeline's own commits go up under.

    The release is made for the RD's work in it, so its commits carry the
    ids of the newest RD commit in the series; a release that replays none
    -- a rebuild -- takes them from the newest RD commit in the history.
    """
    for pool in (series_messages, history_messages):
        for text in reversed(pool):
            parsed = parse_message(text)
            if parsed['cr'] and parsed['ones'] and not parsed['own']:
                return parsed['cr'], parsed['ones'], parsed['feature'] or 'ddk'
    return None


def release_commit_message(part, release, deployed, differences, ids,
                           replayed, identity):
    """The one commit the pipeline makes: what it generated for this release.

    After the RD's commits, the tree still differs from the delivery by
    what the build produced -- module descriptions from BUILD.gn, the
    manifest, licence records, the stamp -- or, for libddk, by everything,
    since the component has no commits of anybody's.  That goes up as one
    change under the ids of the work it was made for.
    """
    cr, ones, feature = ids
    added, removed, changed = differences
    subject = '[%s] [%s] ddk: release %s' % (cr, ones, release)
    if replayed:
        description = ('Release %s ships the %d commit(s) above.  What the '
                       'build generates for them -- module descriptions, '
                       'the manifest, licence records, the release stamp -- '
                       'belongs to no one of them: %d file(s) added, %d '
                       'removed, %d changed.'
                       % (release, replayed, len(added), len(removed),
                          len(changed)))
    else:
        description = ('Release %s replaces %s: %d file(s) added, %d '
                       'removed, %d changed.  %s has no commits of anybody\'s '
                       '-- it is built output -- so a release is one change.'
                       % (release, deployed, len(added), len(removed),
                          len(changed), part))
    lines = [subject, '', '[Description]', description, '', '[Resolution]',
             'Take %s exactly as the release pipeline produced it for '
             'release %s.' % (part, release), '', '[Test]',
             'The tree is the delivery of release %s, byte for byte.'
             % release, '', 'CR-Id: %s' % cr, 'Feature: %s' % feature]
    if identity:
        lines.append('Signed-off-by: %s' % identity)
    return '\n'.join(lines) + '\n'


def removal_message(part, deployed, count, ids, identity):
    """The change that empties a branch before the first delivery lands.

    A branch that carries something else -- the GN-based tree the DDK used
    to be built from -- is emptied in a change of its own, ahead of the
    import, so that the removal and the delivery can be reviewed and
    merged as separate steps rather than as one change whose diff is both
    at once.
    """
    cr, ones, feature = ids
    subject = '[%s] [%s] ddk: remove the previous %s tree' % (cr, ones, part)
    what = ('release %s' % deployed) if deployed else '%d file(s)' % count
    lines = [subject, '', '[Description]',
             'This branch carries %s, which the DDK replaces wholesale: what '
             'lands next is a delivery, not an edit of this, and the two '
             'never coexist.' % what, '', '[Resolution]',
             'Remove the tree in its own change, ahead of the import, so '
             'the removal and the delivery are reviewed and merged as '
             'separate steps.', '', '[Test]',
             'The tree is empty after this change; the delivery on top '
             'restores a complete part.', '', 'CR-Id: %s' % cr,
             'Feature: %s' % feature]
    if identity:
        lines.append('Signed-off-by: %s' % identity)
    return '\n'.join(lines) + '\n'


def import_message(part, release, deployed, differences, ids, identity):
    cr, ones, feature = ids
    added, removed, changed = differences
    subject = '[%s] [%s] ddk: import %s %s' % (cr, ones, part, release)
    if deployed:
        description = ('The tree here is release %s, and nothing says which '
                       'commits it holds: no replayed commit names its '
                       'source, and no build record names a garnet '
                       'revision.  Release %s replaces it whole -- %d '
                       'file(s) added, %d removed, %d changed.'
                       % (deployed, release, len(added), len(removed),
                          len(changed)))
    else:
        description = ('Nothing of the DDK is here yet. Release %s of %s is '
                       'imported whole: %d file(s).'
                       % (release, part, len(added)))
    lines = [subject, '', '[Description]', description, '', '[Resolution]',
             'Take %s exactly as the release pipeline produced it. Its '
             'per-commit history stays in the delivery repository; what '
             'lands here is one reviewable change per release.' % part, '',
             '[Test]', 'The tree is the delivery of release %s, byte for '
             'byte.' % release, '', 'CR-Id: %s' % cr, 'Feature: %s' % feature]
    if identity:
        lines.append('Signed-off-by: %s' % identity)
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# one part


def _clear_worktree(clone):
    for name in os.listdir(clone):
        if name == GIT_DIR:
            continue
        path = os.path.join(clone, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)


def _copy_tree(source, clone):
    for name in os.listdir(source):
        if name == GIT_DIR:
            continue
        src, dst = os.path.join(source, name), os.path.join(clone, name)
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        elif os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)


def _commit(clone, message, identity, author=None, date=None):
    command = ['commit', '-q', '--allow-empty', '-F', '-']
    if author or identity:
        command += ['--author', author or identity]
    if date:
        command += ['--date', date]
    environment = {}
    if identity:
        name, _, mail = identity.partition(' <')
        environment = {'GIT_COMMITTER_NAME': name.strip(),
                       'GIT_COMMITTER_EMAIL': mail.rstrip('>').strip()}
    process = subprocess.Popen(['git', '-C', clone] + command,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               env=dict(os.environ, **environment))
    _, err = process.communicate(message.encode('utf-8'))
    if process.returncode != 0:
        _fail('commit failed in %s: %s'
              % (clone, err.decode('utf-8', 'replace').strip()))
    return _git(clone, 'rev-parse', 'HEAD').strip()


def deploy_part(part, part_dir, project, gerrit, branch, topic, work,
                identity, push, patches_out, garnet=None, context=None):
    """Brings the project up to the delivered part.  Returns a report.

    `context` is shared across the parts of one deployment: libddk goes
    first and leaves the garnet revision its base was built from, which is
    what a half imported whole at the same release holds.
    """
    context = context if context is not None else {}
    report = {'part': part, 'project': project, 'release': read_release(
        part_dir), 'mode': None, 'commits': [], 'reviewers': [], 'urls': []}
    release = report['release']
    if is_dirty(part_dir):
        _fail('%s was built with --dirty: it names a tree nobody can check '
              'out, and is not a release' % part)

    # The clone is kept between runs: libddk is 185 MB and the fetch is
    # incremental the second time.  Nothing in it is trusted -- every run
    # fetches the base again and starts from it.
    url = gerrit.project_url(project)
    clone = os.path.join(work, part)
    if not os.path.isdir(os.path.join(clone, GIT_DIR)):
        shutil.rmtree(clone, ignore_errors=True)
        os.makedirs(clone)
        _git(clone, 'init', '-q')
    else:
        _git(clone, 'am', '--abort', allow_failure=True)
        _git(clone, 'reset', '-q', '--hard')
        _git(clone, 'clean', '-qfdx')

    # The base: the branch, or the tip of what is already up for review.
    _git(clone, 'fetch', '-q', url, branch)
    base = _git(clone, 'rev-parse', 'FETCH_HEAD').strip()
    base_label = branch
    tip = chain_tip(gerrit.open_changes(project, branch, topic))
    if tip:
        _git(clone, 'fetch', '-q', url, tip['ref'])
        base = _git(clone, 'rev-parse', 'FETCH_HEAD').strip()
        base_label = 'change %d (%s)' % (tip['number'], tip['subject'][:40])
    report['base'] = base
    report['base_label'] = base_label
    _git(clone, 'checkout', '-q', '--detach', base)

    # What the base already is.
    deployed = _git(clone, 'show', '%s:%s' % (base, RELEASE_NAME),
                    allow_failure=True).strip() or None
    if not deployed:
        deployed = release_named_by(_git(clone, 'log', '-1', '--format=%B',
                                         base))
    report['deployed'] = deployed
    # Recorded before anything can return: a libddk with nothing to deploy
    # still says which garnet revision the halves' base was built from, and
    # without it a half imported whole cannot tell what it holds.
    if part == 'libddk':
        context['garnet_base'] = garnet_revision_of(clone, base)
        context['deployed'] = deployed
    if deployed == release or not any(substantive(
            tree_differences(part_dir, clone))):
        # The same release, or a rebuild of it: a new stamp and a new
        # build date over the same files is nothing to review.
        report['mode'] = 'up to date'
        return report

    delivery_repo = part_dir if os.path.isdir(os.path.join(part_dir,
                                                           GIT_DIR)) else None
    entries = rd_commits(delivery_repo) if delivery_repo else []
    history = [message for _, message, _ in entries]

    # What the base holds of the RD's work, and so what is new.
    held = sources_held(clone, base)
    garnet_base = context.get('garnet_base') \
        if context.get('deployed') == deployed else None
    series = series_for(entries, held, garnet, garnet_base, deployed) \
        if entries else []
    if series is None:
        series = series_by_content(clone, delivery_repo, entries)
    if series is None and any(source for _, _, source in entries):
        report['mode'] = 'import'
    else:
        # None here means the history names no RD commit at all -- built
        # output, or a half nothing touched since the mirror began -- so
        # there is nothing to replay and the release is the whole change.
        series = series or []
        report['mode'] = 'series'

    if report['mode'] == 'series':
        # libddk has no commits of anybody's, so its release goes up under
        # the ids of the newest RD work in the halves, gathered by main.
        ids = ids_for_own_commits([m for _, m, _ in series], history) \
            or context.get('ids')
        for revision, message, source in series:
            parsed = parse_message(message)
            if not (parsed['cr'] and parsed['ones']):
                _fail('%s: commit %s is not in the review\'s format: %r'
                      % (part, revision[:12], parsed['subject']))
            patch = _git(delivery_repo, 'format-patch', '-1', '--stdout',
                         '--no-signature', '--zero-commit', revision)
            _git(clone, 'am', '-3', '-q', '--keep-non-patch',
                 stdin=patch.encode('utf-8'))
            message = with_change_id(message, change_id_for(project, source))
            author = _git(delivery_repo, 'log', '-1', '--format=%an <%ae>',
                          revision).strip()
            date = _git(delivery_repo, 'log', '-1', '--format=%aI',
                        revision).strip()
            _git(clone, 'commit', '-q', '--amend', '--author', author,
                 '--date', date, '-F', '-', stdin=message.encode('utf-8'))
            report['commits'].append({'source': source[:12],
                                      'subject': message.split('\n')[0],
                                      'author': author})
            mail = author[author.find('<') + 1:author.find('>')]
            if mail and mail not in report['reviewers']:
                report['reviewers'].append(mail)
        # What the build generated on top of them, as one change.
        differences = tree_differences(part_dir, clone)
        if any(differences):
            if not ids:
                _fail('%s: no commit in the delivery carries a CR and ONES '
                      'id to file the release under' % part)
            _clear_worktree(clone)
            _copy_tree(part_dir, clone)
            _git(clone, 'add', '-A')
            message = with_change_id(
                release_commit_message(part, release, deployed, differences,
                                       ids, len(series), identity),
                change_id_for(project, 'release %s' % release))
            _commit(clone, message, identity)
            report['commits'].append({'source': None,
                                      'subject': message.split('\n')[0],
                                      'author': identity})
    else:
        differences = tree_differences(part_dir, clone)
        ids = ids_for_own_commits([], history) or context.get('ids')
        if not ids:
            _fail('%s: no commit in the delivery carries a CR and ONES id '
                  'to file the import under' % part)
        there = files_under(clone)
        if there:
            # Something else is on the branch: emptied first, on its own.
            _clear_worktree(clone)
            _git(clone, 'add', '-A')
            message = with_change_id(
                removal_message(part, deployed, len(there), ids, identity),
                change_id_for(project, 'remove before %s' % release))
            _commit(clone, message, identity)
            report['commits'].append({'source': None,
                                      'subject': message.split('\n')[0],
                                      'author': identity})
        _clear_worktree(clone)
        _copy_tree(part_dir, clone)
        _git(clone, 'add', '-A')
        message = with_change_id(
            import_message(part, release, deployed, differences, ids,
                           identity),
            change_id_for(project, 'import %s' % release))
        _commit(clone, message, identity)
        report['commits'].append({'source': None,
                                  'subject': message.split('\n')[0],
                                  'author': identity})

    # The invariant: what goes up is the delivery.
    added, removed, changed = tree_differences(part_dir, clone)
    if added or removed or changed:
        _fail('%s: after replaying, the tree is not the delivery: %d added, '
              '%d removed, %d changed, e.g. %s'
              % (part, len(added), len(removed), len(changed),
                 ', '.join((added + removed + changed)[:5])))

    if patches_out:
        into = os.path.join(patches_out, part)
        shutil.rmtree(into, ignore_errors=True)
        os.makedirs(into)
        _git(clone, 'format-patch', '-q', '--no-signature', '--zero-commit',
             '-o', into, '%s..HEAD' % base)
        report['patches'] = sorted(os.listdir(into))

    if push:
        refspec = 'HEAD:refs/for/%s%%topic=%s' % (branch, topic)
        for mail in report['reviewers']:
            refspec += ',r=%s' % mail
        report['refspec'] = refspec
        report['urls'], report['push_error'] = push_for_review(clone, url,
                                                               refspec)
        report['pushed'] = report['push_error'] is None
    return report


def push_for_review(clone, url, refspec):
    """Pushes once.  Returns the change URLs Gerrit printed, or what it
    said when it refused -- a refusal is a person's to look at, not
    something to retry or force."""
    process = subprocess.Popen(['git', '-C', clone, 'push', '--no-thin', url,
                                refspec], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    out, err = process.communicate()
    text = (out + err).decode('utf-8', 'replace')
    if process.returncode != 0:
        said = [line.strip() for line in text.splitlines()
                if line.strip() and not line.startswith('**')]
        return [], '\n'.join(said[-8:]) or 'git push failed'
    return re.findall(r'(https?://\S+/\+/\d+)', text), None


# ---------------------------------------------------------------------------


def status_of(report):
    """One line's worth: what happens to this part, in counts not lists.

    The commits themselves are in the patches and in deploy.json; a series
    can run to hundreds and a screen is not where to read it.
    """
    if report['mode'] == 'up to date':
        if report['deployed'] != report['release']:
            return 'up to date (rebuilt, same content)'
        return 'up to date'
    if report['mode'] == 'import':
        if len(report['commits']) == 2:
            return 'remove what is there, then import whole'
        return 'import whole (%s)' % (
            'nothing on the branch yet' if not report['deployed']
            else 'branch holds %s, unreadable' % report['deployed'])
    rd = [c for c in report['commits'] if c['source']]
    own = len(report['commits']) - len(rd)
    pieces = []
    if rd:
        pieces.append('%d from %s' % (len(rd), ', '.join(
            report['reviewers']) or 'RD'))
    if own:
        pieces.append('1 release')
    return '%d change%s (%s)' % (len(report['commits']),
                                 '' if len(report['commits']) == 1 else 's',
                                 ', '.join(pieces))


def print_summary(reports, deploy, pushed, out):
    """What a person reads: one line per part, one line of what happened."""
    release = reports[0]['release'] if reports else ''
    print('DDK release %s  ->  %s  (topic %s)'
          % (release, deploy['branch'], deploy['topic']))
    print('')
    print('  %-8s  %-22s  %s' % ('part', 'deployed', 'status'))
    for report in reports:
        print('  %-8s  %-22s  %s' % (report['part'],
                                     report['deployed'] or '-',
                                     status_of(report)))
        if report['urls']:
            urls = report['urls']
            span = urls[0] if len(urls) == 1 else '%s .. %s' % (
                urls[0], urls[-1].rsplit('/', 1)[-1])
            print('  %-8s  %-22s  pushed: %s' % ('', '', span))
    print('')
    refused = [r for r in reports if r.get('push_error')]
    pending = [r for r in reports if r['commits'] and not r['urls']
               and not r.get('push_error')]
    if refused:
        print('NOT PUSHED: Gerrit refused, and this does not retry or force.')
        for report in refused:
            who = ', '.join(report['reviewers']) or 'nobody: no RD in the series'
            print('  %-8s  tell %s' % (report['part'], who))
            print('  %-8s  %s' % ('', report['push_error'].split('\n')[-1]))
    if pending and not pushed:
        print('not pushed (no --push): %s ready'
              % ', '.join(r['part'] for r in pending))
    if not pending and not refused and not any(r['urls'] for r in reports):
        print('nothing to deploy')
    if out:
        print('details: %s' % os.path.join(out, 'deploy.json'))


def read_policy(path):
    with io.open(path, encoding='utf-8') as handle:
        policy = json.load(handle)
    deploy = policy.get('deploy') or {}
    for key in ('gerrit', 'prefix', 'branch', 'topic'):
        if not deploy.get(key):
            _fail('%s: deploy.%s is not set' % (path, key))
    return layout_.read_policy(path), deploy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delivery', required=True,
                        help='output/: the finished delivery')
    parser.add_argument('--policy', required=True,
                        help='delivery.json: the layout and where to deploy')
    parser.add_argument('--work', required=True,
                        help='where the projects are fetched to')
    parser.add_argument('--out', help='where the patches and the report go')
    parser.add_argument('--user', help='the Gerrit ssh user; default the '
                                       'one in the policy url, else the local '
                                       'part of git config user.email')
    parser.add_argument('--identity', default=IDENTITY,
                        help='committer, as "Name <mail>"; default the '
                             'release identity')
    parser.add_argument('--garnet', help='the garnet checkout, which says '
                                         'what a base imported whole holds')
    parser.add_argument('--part', action='append', default=[],
                        help='only this part; repeat')
    parser.add_argument('--push', action='store_true',
                        help='push the changes for review. Without it the '
                             'patches are written to --out and nothing '
                             'leaves this machine.')
    args = parser.parse_args()

    try:
        layout, deploy = read_policy(args.policy)
        gerrit = Gerrit(deploy['gerrit'], args.user)
        if not gerrit.user:
            gerrit.user = user_from_git_config()
        if not gerrit.user:
            _fail('no Gerrit user: none in %s, none given with --user, and '
                  'git config user.email is not set' % deploy['gerrit'])
        parts = args.part or [p for p in PARTS if os.path.isdir(
            layout_.part_dir(args.delivery, p, layout))]
        if not parts:
            _fail('%s holds no part' % args.delivery)
        reports = []
        # The newest RD work across the halves: what libddk's release, which
        # has no commits of anybody's, is filed under.
        dated = []
        for part in parts:
            part_dir = layout_.part_dir(args.delivery, part, layout)
            if part != 'libddk' and os.path.isdir(os.path.join(part_dir,
                                                               GIT_DIR)):
                dated += [(stamp, m) for _, m, _, stamp
                          in dated_commits(part_dir)]
        dated.sort(key=lambda item: item[0])
        context = {'ids': ids_for_own_commits([], [m for _, m in dated])}
        for part in parts:
            project = '%s/%s' % (deploy['prefix'].rstrip('/'),
                                 layout_.where(part, layout))
            report = deploy_part(
                part, layout_.part_dir(args.delivery, part, layout), project,
                gerrit, deploy['branch'], deploy['topic'], args.work,
                args.identity, args.push, args.out, args.garnet, context)
            reports.append(report)
        refused = [r for r in reports if r.get('push_error')]
        print_summary(reports, deploy, args.push, args.out)
        if args.out:
            if not os.path.isdir(args.out):
                os.makedirs(args.out)
            with io.open(os.path.join(args.out, 'deploy.json'), 'w',
                         encoding='utf-8') as handle:
                handle.write(_text(json.dumps(reports, indent=1,
                                              sort_keys=True) + '\n'))
    except DeployError as error:
        sys.stderr.write('error: %s\n' % error)
        return 1
    if refused:
        return 1
    if not args.push:
        pending = [r['part'] for r in reports if r['commits']]
        if pending:
            print('not pushed (no --push): %s ready%s'
                  % (', '.join(pending),
                     ', patches in %s' % args.out if args.out else ''))
        else:
            print('nothing to deploy: every part is up to date')
    return 0


if __name__ == '__main__':
    sys.exit(main())

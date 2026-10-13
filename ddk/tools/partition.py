#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Splits a source tree into ownership parts, and puts it back together.

The DDK is delivered as two source halves, one owned by GRT and one by MTK,
each built against libddk.  The point of splitting them is to be able to put
them back: MTK returns a changed half, the halves are merged into a whole
garnet again, and diffing that against ours shows exactly what MTK changed.

That only works if the split is reversible, which needs three properties, and
this tool exists to enforce all three rather than to hope for them:

  total       every file in the tree lands in some part
  disjoint    no file lands in two
  path-       a file keeps its tree path in its part, so merging is a union
  preserving  and a file added on either side lands where its author put it

Ownership is declared by longest-prefix rules, which are order-independent and
so can be reviewed one line at a time.  A rule that matches nothing is an
error: a rule that has gone stale is how a partition silently stops being what
its author last read.

Commands:

  check      report the partition without writing anything
  split      write each part as a tree
  merge      union parts back into one tree
  verify     compare two trees byte for byte
  roundtrip  split, merge and verify in a temporary directory
"""

from __future__ import print_function

import argparse
import errno
import filecmp
import hashlib
import json
import os
import re
import shutil
import stat
import sys

FORMAT_VERSION = 1
MANIFEST_NAME = '.partition.json'

# A half's own bookkeeping, not content.  Present in what ships and in
# nothing the merge produces.
#
# LICENSES/ and NOTICE are generated for each half when the delivery is
# assembled, out of a declaration that lives here.  They travel with the half,
# so a half coming back has them -- and merging them into garnet would add a
# generated tree to the source it was generated from, then report it as
# something the other side wrote.
# What a part carries that is not source: the manifest, the release stamp,
# and the build that ships with the half.  None of them is in the tree the
# split came from, so a merge must not put them back into it, and none of
# them is the sender's work when a half comes back with one.  Matched on the
# part-relative path, so a Makefile deeper in the tree is source like
# anything else.
PART_BOOKKEEPING = frozenset([MANIFEST_NAME, 'RELEASE', 'LIBDDK_ABI',
                              'Makefile'])
PART_BOOKKEEPING_DIRS = frozenset(['LICENSES', '.git'])

# NOTICE is bookkeeping only where the licence set was installed.  garnet has
# a NOTICE of its own at its root, so the name alone does not say which one a
# part holds -- and skipping it unconditionally drops garnet's from the merge,
# quietly, in a round trip that otherwise reproduces 5994 entries exactly.
# LICENSES/FILES is written by the same pass that writes the notice and by
# nothing else, so its presence is what distinguishes them.
GENERATED_NOTICE = 'NOTICE'
GENERATED_MARKER = 'LICENSES/FILES'

# A module description.  Generated here from a BUILD.gn target, so one that
# arrives in a returned half is a module the sender declared and our build
# does not: committing it produces a file the next build overwrites.
MODULE_SUFFIX = '.ddk.mk'

# Where a path can appear in a file we deliver.  Rewriting is structural, not
# textual, and that is the whole design: `src/` occurs in English prose in
# this tree -- "clear its src/dst registers", and a comment naming an MTK path
# -- so a rewrite that touched every occurrence could not be undone without
# corrupting comments.  Only an include line, a make field known to hold
# paths, and a JSON string are rewritten; everything else is left alone.
INCLUDE_RE = re.compile(r'(#[ \t]*include[ \t]*[<"])([^">\n]*)([">])')
MAKE_PATH_FIELDS = ('MODULE_SRCS', 'MODULE_INCLUDE_DIRS')
MAKE_FIELD_RE = re.compile(
    r'^([ \t]*(?:%s)[ \t]*:?=)([^\n]*)$' % '|'.join(MAKE_PATH_FIELDS),
    re.M)
JSON_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')

SOURCE_SUFFIXES = ('.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.inc',
                   '.S')
MAKE_SUFFIXES = ('.mk',)
JSON_SUFFIXES = ('.json',)

try:
    STRING_TYPES = (str, unicode)  # noqa: F821  (python 2)
except NameError:
    STRING_TYPES = (str,)


class PartitionError(Exception):
    """A partition that is not total, not disjoint, or not what it claims."""


def _fail(message):
    raise PartitionError(message)


# ---------------------------------------------------------------------------
# rules


def read_rules(path):
    """Reads and validates the ownership rules."""
    with open(path, 'r') as rules_file:
        rules = json.load(rules_file)
    if not isinstance(rules, dict):
        _fail('%s: rules must be an object' % path)
    if rules.get('format_version') != FORMAT_VERSION:
        _fail('%s: unsupported format_version %r'
              % (path, rules.get('format_version')))

    parts = rules.get('parts')
    if not isinstance(parts, dict) or not parts:
        _fail('%s: `parts` must be a non-empty object of name -> reason' % path)
    for name, reason in parts.items():
        if not _is_part_name(name):
            _fail('%s: invalid part name %r' % (path, name))
        if not isinstance(reason, STRING_TYPES) or not reason.strip():
            _fail('%s: part %s needs a reason' % (path, name))

    default_part = rules.get('default_part')
    if default_part not in parts:
        _fail('%s: default_part %r is not a declared part' % (path,
                                                              default_part))

    seen = {}
    entries = rules.get('rules')
    if not isinstance(entries, list):
        _fail('%s: `rules` must be a list' % path)
    for entry in entries:
        if not isinstance(entry, dict):
            _fail('%s: each rule must be an object' % path)
        prefix = entry.get('prefix')
        if not _is_clean_relative(prefix):
            _fail('%s: invalid rule prefix %r' % (path, prefix))
        if prefix in seen:
            _fail('%s: duplicate rule for %r' % (path, prefix))
        if entry.get('part') not in parts:
            _fail('%s: rule %s names undeclared part %r'
                  % (path, prefix, entry.get('part')))
        reason = entry.get('reason')
        if not isinstance(reason, STRING_TYPES) or not reason.strip():
            _fail('%s: rule %s needs a reason' % (path, prefix))
        seen[prefix] = entry

    ignore = rules.get('ignore', [])
    if not isinstance(ignore, list) or not all(_is_clean_relative(i)
                                               for i in ignore):
        _fail('%s: `ignore` must be a list of relative paths' % path)

    rename = rules.get('rename')
    if rename is not None:
        if not isinstance(rename, dict):
            _fail('%s: `rename` must be an object' % path)
        source, target = rename.get('from'), rename.get('to')
        for name, value in (('from', source), ('to', target)):
            if (not isinstance(value, STRING_TYPES) or '/' in (value or '')
                    or not _is_clean_relative(value)):
                _fail('%s: rename.%s must be a single directory name, got %r'
                      % (path, name, value))
        if source == target:
            _fail('%s: rename maps %r onto itself' % (path, source))
        keep = rename.get('keep', [])
        if not isinstance(keep, list) or not all(_is_clean_relative(k)
                                                 for k in keep):
            _fail('%s: rename.keep must be a list of relative paths' % path)
        for entry in keep:
            if not (entry == source or entry.startswith(source + '/')):
                _fail('%s: rename.keep names %r, which is not under %r'
                      % (path, entry, source))
        if rename.get('reason') in (None, ''):
            _fail('%s: rename needs a reason. Renaming a delivered directory '
                  'changes what the customer compiles; it is a decision, not '
                  'a detail.' % path)

    rules['rules'] = entries
    rules['ignore'] = ignore
    return rules


def _is_part_name(name):
    return (isinstance(name, STRING_TYPES) and name
            and all(c.isalnum() or c in '_-' for c in name))


def _is_clean_relative(path):
    """Whether `path` is a non-empty, relative, `..`-free POSIX path."""
    if not isinstance(path, STRING_TYPES) or not path:
        return False
    if '\\' in path or path.startswith('/'):
        return False
    return all(part not in ('', '.', '..') for part in path.split('/'))


def rules_digest(rules):
    """A digest of the ownership decision, so parts cannot be mixed."""
    decision = {
        'default_part': rules['default_part'],
        'parts': sorted(rules['parts']),
        'rules': sorted((r['prefix'], r['part']) for r in rules['rules']),
        # A part split under a different rename cannot be merged with one
        # that was not: the paths in its manifest mean something else.
        'rename': rules.get('rename'),
    }
    encoded = json.dumps(decision, sort_keys=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def swap_leading(path, source, target, require_slash=False):
    """`path` with its first component changed from `source` to `target`.

    `require_slash` is for rewriting inside a file rather than renaming a
    path.  A bare word is not a path: a rules file that renames `garnet` to
    `src` says `"to": "src"` in its own text, and a JSON rewriter that
    swapped every string equal to the name would turn that into
    `"to": "garnet"` when undoing itself.  The invertibility check caught it,
    which is what it is for.
    """
    if path == source:
        return path if require_slash else target
    if path.startswith(source + '/'):
        return target + path[len(source):]
    return path


def file_kind(name):
    """Which rewriter, if any, applies to a file with this name."""
    if name.endswith(SOURCE_SUFFIXES):
        return 'source'
    if name.endswith(MAKE_SUFFIXES):
        return 'make'
    if name.endswith(JSON_SUFFIXES):
        return 'json'
    return None


def _kept(path, source, target, keep):
    """Whether a path is one the rename must leave exactly as it is.

    Some paths in a delivered file do not name anything in the half: the
    generated protobuf headers and the GRT bus API are compiled into libddk
    and installed under its own include tree, so a driver reaches them at the
    path libddk publishes.  Renaming those would point them at a directory
    the delivery does not have.  Spelled under both names because a file is
    checked in both directions.
    """
    for prefix in keep or ():
        for spelling in (prefix,
                         swap_leading(prefix, source, target, True)):
            if path == spelling or path.startswith(spelling + '/'):
                return True
    return False


def rewrite(text, kind, source, target, keep=()):
    """`text` with leading `source` path components changed to `target`."""
    def swap(path):
        if _kept(path, source, target, keep):
            return path
        return swap_leading(path, source, target, True)

    if kind == 'source':
        def one(match):
            return match.group(1) + swap(match.group(2)) + match.group(3)
        return INCLUDE_RE.sub(one, text)
    if kind == 'make':
        def field(match):
            return match.group(1) + ' '.join(
                swap(v) for v in match.group(2).split(' '))
        return MAKE_FIELD_RE.sub(field, text)
    if kind == 'json':
        def string(match):
            return '"%s"' % swap(match.group(1))
        return JSON_STRING_RE.sub(string, text)
    return text


def rename_bytes(data, kind, source, target, keep=()):
    """Rewrites a file's bytes, and proves the rewrite can be undone.

    The check is on the bytes rather than on the pattern, and it runs on every
    file every time.  Reasoning about whether a regex is one-to-one is how a
    round trip quietly stops being one: this tree has `#include <src/lua...>`
    in one file, which no leading-component rule can distinguish from a path
    that was renamed, and the only way to be sure is to undo the rewrite and
    compare.
    """
    if kind is None:
        return data
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return data
    forward = rewrite(text, kind, source, target, keep)
    if rewrite(forward, kind, target, source, keep) != text:
        return None
    return forward.encode('utf-8')


def scoped_prefix(prefix, scope):
    """A rule prefix as it appears inside the tree being split.

    The ownership decision is written once, against source-tree paths.  The
    same decision has to answer for two trees: garnet itself, and the DDK
    source component, whose files keep their source-tree path and so sit one
    level down under `garnet/`.  `scope` is that level, so neither tree needs
    its own copy of the rules -- two copies is how one of them goes stale.
    """
    return prefix if not scope else scope + '/' + prefix


def owner_of(path, rules, scope=''):
    """The part owning `path`, by longest matching prefix.

    Longest-prefix rather than first-match: the answer must not depend on the
    order the rules happen to be written in, or moving a line changes the
    delivery.
    """
    best_prefix = None
    best_part = rules['default_part']
    for entry in rules['rules']:
        prefix = scoped_prefix(entry['prefix'], scope)
        if path == prefix or path.startswith(prefix + '/'):
            if best_prefix is None or len(prefix) > len(best_prefix):
                best_prefix = entry['prefix']
                best_part = entry['part']
    return best_part, best_prefix


# ---------------------------------------------------------------------------
# reading a tree


def _is_ignored(relative, ignore):
    for pattern in ignore:
        if relative == pattern or relative.startswith(pattern + '/'):
            return True
    return False


def scan_tree(root, ignore):
    """Every file, symlink and empty directory under `root`.

    Empty directories are recorded because merging must reproduce the tree
    exactly, and a directory with no files in it is not reproduced by copying
    files.
    """
    entries = {}
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        _fail('not a directory: %s' % root)
    for current, dirs, files in os.walk(root):
        relative_dir = os.path.relpath(current, root).replace(os.sep, '/')
        if relative_dir == '.':
            relative_dir = ''
        dirs[:] = sorted(
            d for d in dirs
            if not _is_ignored(_join(relative_dir, d), ignore))
        kept = []
        for name in sorted(files):
            relative = _join(relative_dir, name)
            if _is_ignored(relative, ignore):
                continue
            absolute = os.path.join(current, name)
            if os.path.islink(absolute):
                entries[relative] = {
                    'kind': 'link',
                    'target': os.readlink(absolute),
                }
            else:
                entries[relative] = {
                    'kind': 'file',
                    'mode': stat.S_IMODE(os.lstat(absolute).st_mode),
                }
            kept.append(name)
        if relative_dir and not kept and not dirs:
            entries[relative_dir] = {
                'kind': 'dir',
                'mode': stat.S_IMODE(os.lstat(current).st_mode),
            }
    return entries


def _join(directory, name):
    return name if not directory else directory + '/' + name


# ---------------------------------------------------------------------------
# the partition itself


def partition(entries, rules, scope=''):
    """Assigns every entry to a part, and reports rules that matched nothing.

    Totality is by construction: default_part answers wherever no rule does.
    Disjointness is by construction too, since owner_of returns one part.  The
    check that is not free is staleness, so that is the one reported.
    """
    assignment = {}
    matched = {}
    for relative in sorted(entries):
        part, prefix = owner_of(relative, rules, scope)
        assignment.setdefault(part, []).append(relative)
        if prefix is not None:
            matched[prefix] = matched.get(prefix, 0) + 1
    stale = [r['prefix'] for r in rules['rules']
             if r['prefix'] not in matched]
    return assignment, matched, stale


def check(tree, rules, scope='', subset=False):
    """Reports the partition, failing on stale rules.

    `subset` says this tree is a part of the tree the rules describe -- the
    DDK source component holds only the files the delivery declares, so a
    driver this product does not build has no files here.  A rule matching
    nothing then means "not in this product", not "gone".  The authoritative
    tree is checked without it, which is where a rule that has really gone
    stale is caught.
    """
    entries = scan_tree(tree, rules['ignore'])
    assignment, matched, stale = partition(entries, rules, scope)
    if stale and not subset:
        _fail('ownership rule(s) match nothing in %s; remove them or fix the '
              'path:\n  %s' % (tree, '\n  '.join(sorted(stale))))
    return entries, assignment, matched


# ---------------------------------------------------------------------------
# writing


def _makedirs(path):
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno != errno.EEXIST:
            raise


def _materialize(entry, source, destination, rename=None, reverse=False):
    _makedirs(os.path.dirname(destination))
    if entry['kind'] == 'link':
        if os.path.lexists(destination):
            os.remove(destination)
        os.symlink(entry['target'], destination)
    elif entry['kind'] == 'dir':
        _makedirs(destination)
        os.chmod(destination, entry['mode'])
    elif rename is None:
        shutil.copyfile(source, destination)
        os.chmod(destination, entry['mode'])
    else:
        frm, to = rename['from'], rename['to']
        if reverse:
            frm, to = to, frm
        with open(source, 'rb') as handle:
            data = handle.read()
        rewritten = rename_bytes(data, file_kind(source), frm, to,
                                 rename.get('keep', ()))
        if rewritten is None:
            _fail('%s cannot be renamed reversibly: undoing the rewrite does '
                  'not reproduce the file. Something in it already starts a '
                  'path with %r, so a merge could not tell the two apart.'
                  % (source, to))
        with open(destination, 'wb') as handle:
            handle.write(rewritten)
        os.chmod(destination, entry['mode'])


def split(tree, rules, out, scope='', subset=False):
    """Writes each part as a tree, with a manifest naming what it holds.

    A part may be laid out under a different top-level name than the tree it
    came from, if the rules ask for one.  The argument for doing so is that
    `garnet` on the top line of a directory listing says which upstream this
    derives from, and where every file sat in it, to anybody who has never
    seen the code.  The argument against is that a reader holding both trees
    then has to translate every path before they can look one up, and that is
    the argument the recipients made, so nothing is renamed today.

    Where one is applied, the manifest records the paths as delivered rather
    than as they were: it travels inside the part and would otherwise say in
    one file what the layout was changed to stop saying.
    """
    entries, assignment, _ = check(tree, rules, scope, subset)
    digest = rules_digest(rules)
    rename = rules.get('rename')
    written = {}
    for part in sorted(rules['parts']):
        part_root = os.path.join(out, part)
        if os.path.exists(part_root):
            shutil.rmtree(part_root)
        _makedirs(part_root)
        held = assignment.get(part, [])
        delivered = {}
        for relative in held:
            target = relative
            if rename is not None:
                target = swap_leading(relative, rename['from'], rename['to'])
            delivered[target] = entries[relative]
            _materialize(entries[relative],
                         os.path.join(tree, relative),
                         os.path.join(part_root, target), rename)
        manifest = {
            'format_version': FORMAT_VERSION,
            'part': part,
            'rules_digest': digest,
            'entries': delivered,
        }
        if rename is not None:
            # A flag, not the mapping.  The manifest travels inside the part,
            # and writing "from": "garnet" in it would put the name back on
            # the one line the rename exists to clear.  What it takes to undo
            # this is in the rules file, which stays here.
            manifest['renamed'] = True
        with open(os.path.join(part_root, MANIFEST_NAME), 'w') as handle:
            # Explicit separators: python 2 writes ", " after a comma and
            # python 3 writes ",".  A part's manifest travels with it, so it
            # must be the same bytes whichever python split it.
            json.dump(manifest, handle, indent=2, sort_keys=True,
                      separators=(',', ': '))
            handle.write('\n')
        written[part] = len(delivered)
    return written


def render(tree, out, rename):
    """Copies a tree into the layout a delivery uses, without partitioning it.

    The split renders each half as it writes it.  This is the same rendering
    for a tree that is not being split -- the whole-tree self-test and the
    trim's two builds -- because libddk publishes its headers under the
    delivered name, and a sandbox that compiles the tree's own spelling
    against it would not find them.
    """
    if os.path.exists(out):
        shutil.rmtree(out)
    _makedirs(out)
    entries = scan_tree(tree, [])
    for relative in sorted(entries):
        target = swap_leading(relative, rename['from'], rename['to'])
        _materialize(entries[relative], os.path.join(tree, relative),
                     os.path.join(out, target), rename)
    return len(entries)


def merge(part_roots, out, rules=None):
    """Unions parts back into one tree.

    Refuses parts that came from different ownership rules, and refuses a path
    claimed twice.  Both would produce a tree that looks plausible and is not
    the one that was split.

    `rules` is required when a part was delivered under a renamed layout: the
    part says that one was applied and not what it was, so undoing it needs
    the rules file, which never leaves here.
    """
    if os.path.exists(out):
        shutil.rmtree(out)
    _makedirs(out)

    digests = set()
    claimed = {}
    total = removed = 0
    added = []
    for part_root in part_roots:
        manifest_path = os.path.join(part_root, MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            _fail('%s is not a partition part: no %s'
                  % (part_root, MANIFEST_NAME))
        with open(manifest_path, 'r') as handle:
            manifest = json.load(handle)
        if manifest.get('format_version') != FORMAT_VERSION:
            _fail('%s: unsupported manifest format' % part_root)
        digests.add(manifest.get('rules_digest'))
        if len(digests) > 1:
            _fail('parts were split under different ownership rules; merging '
                  'them would not reproduce either tree')
        rename = None
        if manifest.get('renamed'):
            rename = (rules or {}).get('rename')
            if rename is None:
                _fail('%s was delivered under a renamed layout and the rules '
                      'that say how to undo it were not given. Pass --rules.'
                      % part_root)
            if rules_digest(rules) != manifest.get('rules_digest'):
                _fail('%s was split under different rules than the ones '
                      'given; undoing its rename with these would not '
                      'reproduce the tree.' % part_root)
        # What is on disk, not what the manifest lists.  The manifest says
        # what we sent; a half comes back with what they did to it, and
        # merging the manifest silently drops a file they added -- a whole
        # driver, reported as "merged 645 entries" and no complaint.  It is
        # still what says how each entry was laid out, so it supplies the
        # kind and the mode for everything it knows about.
        entries = manifest['entries']
        on_disk = _part_contents(part_root)
        for delivered in sorted(on_disk):
            if not _is_clean_relative(delivered):
                _fail('%s: invalid path: %r' % (part_root, delivered))
            relative = delivered
            if rename is not None:
                relative = swap_leading(delivered, rename['to'],
                                        rename['from'])
            if relative in claimed:
                _fail('%s is claimed by both %s and %s'
                      % (relative, claimed[relative], manifest['part']))
            claimed[relative] = manifest['part']
            entry = entries.get(delivered)
            if entry is None:
                entry = on_disk[delivered]
                added.append(relative)
            _materialize(entry, os.path.join(part_root, delivered),
                         os.path.join(out, relative), rename, reverse=True)
            total += 1
        removed += len(set(entries) - set(on_disk))
    return {'entries': total, 'added': sorted(added), 'removed': removed,
            'declared': sorted(a for a in added
                               if a.endswith(MODULE_SUFFIX))}


def _part_contents(part_root):
    """Every file, link and empty directory a part actually holds."""
    found = {}
    bookkeeping = set(PART_BOOKKEEPING)
    if os.path.isfile(os.path.join(part_root, *GENERATED_MARKER.split('/'))):
        bookkeeping.add(GENERATED_NOTICE)
    for current, dirs, names in os.walk(part_root):
        if current == part_root:
            dirs[:] = [d for d in dirs if d not in PART_BOOKKEEPING_DIRS]
        dirs.sort()
        relative_dir = os.path.relpath(current, part_root).replace(os.sep, '/')
        if relative_dir == '.':
            relative_dir = ''
        kept = []
        for name in sorted(names):
            relative = _join(relative_dir, name)
            if relative in bookkeeping:
                continue
            path = os.path.join(current, name)
            if os.path.islink(path):
                found[relative] = {'kind': 'link',
                                   'target': os.readlink(path)}
            else:
                found[relative] = {
                    'kind': 'file',
                    'mode': stat.S_IMODE(os.lstat(path).st_mode),
                }
            kept.append(name)
        if relative_dir and not kept and not dirs:
            found[relative_dir] = {
                'kind': 'dir',
                'mode': stat.S_IMODE(os.lstat(current).st_mode),
            }
    return found


# ---------------------------------------------------------------------------
# verifying


def describe_declarations(declared):
    """What to say when a returned half declares a module we do not.

    A module description is derived here, from a product target the delivery
    policy ships.  One that comes back is a module the sender added, and
    committing the file as it stands produces something the next release
    overwrites without noticing --
    the module then disappears from the delivery and nobody is told why.
    Said out loud, with what to do, because the person running the merge is
    the last one who can act on it.
    """
    if not declared:
        return []
    lines = ['',
             '  %d module description(s) came back that this build does not '
             'generate:' % len(declared)]
    for relative in declared:
        lines.append('    %s' % relative)
    lines.append('  Each needs a product target here that '
                 'policy/delivery.json delivers,')
    lines.append('  so the next release derives it.')
    lines.append('  Committing the file on its own leaves it to be '
                 'overwritten, and the')
    lines.append('  module then leaves the delivery with nothing said.')
    return lines


def verify(original, merged, ignore):
    """Every difference between two trees, as a list of human-readable lines."""
    left = scan_tree(original, ignore)
    right = scan_tree(merged, ignore)
    problems = []
    for relative in sorted(set(left) - set(right)):
        problems.append('missing from merge: %s' % relative)
    for relative in sorted(set(right) - set(left)):
        problems.append('appeared in merge: %s' % relative)
    for relative in sorted(set(left) & set(right)):
        a, b = left[relative], right[relative]
        if a['kind'] != b['kind']:
            problems.append('%s: %s became %s'
                            % (relative, a['kind'], b['kind']))
            continue
        if a['kind'] == 'link':
            if a['target'] != b['target']:
                problems.append('%s: link target %s became %s'
                                % (relative, a['target'], b['target']))
            continue
        if a['mode'] != b['mode']:
            problems.append('%s: mode %o became %o'
                            % (relative, a['mode'], b['mode']))
        if a['kind'] == 'file' and not filecmp.cmp(
                os.path.join(original, relative),
                os.path.join(merged, relative), shallow=False):
            problems.append('%s: content differs' % relative)
    return problems


# ---------------------------------------------------------------------------
# command line


def _report(assignment, matched, rules, stream):
    for part in sorted(rules['parts']):
        stream.write('  %-10s %5d entries\n'
                     % (part, len(assignment.get(part, []))))
    for entry in sorted(rules['rules'], key=lambda r: r['prefix']):
        stream.write('    %-40s -> %-10s %5d\n'
                     % (entry['prefix'], entry['part'],
                        matched.get(entry['prefix'], 0)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',
                        choices=['check', 'split', 'merge', 'render',
                                 'verify', 'roundtrip'])
    parser.add_argument('--tree', help='the tree to split or check')
    parser.add_argument('--rules', help='the ownership rules')
    parser.add_argument('--out', help='where to write')
    parser.add_argument('--prefix', default='',
                        help='directory the rule prefixes sit under in this '
                             'tree, for a tree that carries source-tree paths')
    parser.add_argument('--subset', action='store_true',
                        help='this tree holds only part of what the rules '
                             'describe, so a rule may match nothing')
    parser.add_argument('--part', action='append', default=[],
                        help='a part directory, for merge')
    parser.add_argument('--original', help='the tree a merge should reproduce')
    parser.add_argument('--merged', help='the merged tree')
    args = parser.parse_args()

    try:
        rules = None
        if args.command in ('check', 'split', 'render', 'roundtrip'):
            if not args.tree or not args.rules:
                parser.error('%s needs --tree and --rules' % args.command)
            rules = read_rules(args.rules)
        elif args.rules:
            # merge needs them only when a part was renamed, and says so
            # itself if they are missing.
            rules = read_rules(args.rules)

        if args.command == 'check':
            entries, assignment, matched = check(args.tree, rules,
                                                 args.prefix, args.subset)
            print('%s: %d entries' % (args.tree, len(entries)))
            _report(assignment, matched, rules, sys.stdout)
            return 0

        if args.command == 'split':
            if not args.out:
                parser.error('split needs --out')
            written = split(args.tree, rules, args.out, args.prefix,
                            args.subset)
            for part in sorted(written):
                print('%s: %d entries -> %s'
                      % (part, written[part], os.path.join(args.out, part)))
            return 0

        if args.command == 'render':
            if not args.out:
                parser.error('render needs --out')
            rename = rules.get('rename')
            if rename is None:
                parser.error('%s declares no rename' % args.rules)
            total = render(args.tree, args.out, rename)
            print('rendered %d entries -> %s' % (total, args.out))
            return 0

        if args.command == 'merge':
            if not args.part or not args.out:
                parser.error('merge needs --part (repeatable) and --out')
            result = merge(args.part, args.out, rules)
            print('merged %d entries -> %s' % (result['entries'], args.out))
            # Said out loud.  A returned half that added or removed something
            # is the normal case and the reason any of this exists, but it is
            # also the case where a silent number is worth nothing.
            if result['added'] or result['removed']:
                print('  the sender added %d and removed %d'
                      % (len(result['added']), result['removed']))
            for line in describe_declarations(result['declared']):
                print(line)
            return 0

        if args.command == 'verify':
            if not args.original or not args.merged:
                parser.error('verify needs --original and --merged')
            ignore = []
            if args.rules:
                ignore = read_rules(args.rules)['ignore']
            problems = verify(args.original, args.merged, ignore)
            if problems:
                sys.stderr.write('merge does not reproduce %s:\n  %s\n'
                                 % (args.original, '\n  '.join(problems)))
                return 1
            print('merge reproduces %s exactly' % args.original)
            return 0

        if args.command == 'roundtrip':
            return _roundtrip(args, rules)
    except PartitionError as error:
        sys.stderr.write('error: %s\n' % error)
        return 1
    return 0


def _roundtrip(args, rules):
    """The invariant: merge(split(tree)) is tree, byte for byte."""
    import tempfile
    workspace = args.out or tempfile.mkdtemp(prefix='ddk-partition-')
    made = args.out is None
    try:
        parts_dir = os.path.join(workspace, 'parts')
        merged_dir = os.path.join(workspace, 'merged')
        written = split(args.tree, rules, parts_dir, args.prefix,
                        args.subset)
        for part in sorted(written):
            print('%s: %d entries' % (part, written[part]))
        total = merge([os.path.join(parts_dir, p) for p in sorted(written)],
                      merged_dir, rules)['entries']
        problems = verify(args.tree, merged_dir, rules['ignore'])
        if problems:
            sys.stderr.write('round trip changed the tree:\n  %s\n'
                             % '\n  '.join(problems[:40]))
            if len(problems) > 40:
                sys.stderr.write('  ... and %d more\n' % (len(problems) - 40))
            return 1
        print('round trip is exact: %d entries split and merged back into %s'
              % (total, args.tree))
        return 0
    finally:
        if made:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())

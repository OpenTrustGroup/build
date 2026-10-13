#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Materializes a DDK tree from a declared SDK manifest.

The input is the portable SDK manifest produced by `sdk_molecule` and
`create_portable_manifest.py`: every atom lists the files it contributes and
every source is a root-relative locator (`out://...` or `source://...`).

The layout file is the single contract describing where each atom lands inside
the DDK tree.  Nothing is inferred from producer paths: an atom that is not
named by the layout is an error, and a layout entry that no atom matches is an
error too.  The customer-visible directory structure therefore changes only
when the layout file changes.

This script performs no dependency discovery of its own.  What is shipped is
exactly what the build graph declared.
"""

import argparse
import errno
import hashlib
import json
import os
import shutil
import sys


LAYOUT_FORMAT_VERSION = 1
CONTENTS_FORMAT_VERSION = 1
import build_info
import collect_licenses

CONTENTS_FILE = 'SDK_CONTENTS.json'
LOCATOR_SEPARATOR = '://'

# What a component may be made of.  A DDK component ships an interface and the
# binaries implementing it: headers to compile against, archives and shared
# libraries to link.  Anything else is source, build glue or metadata that the
# consumer has no use for and should not have to reason about.
HEADER_SUFFIXES = frozenset([
    '.def', '.defs', '.h', '.hh', '.hpp', '.hxx', '.inc', '.inl', '.ipp',
])
BINARY_SUFFIXES = frozenset(['.a', '.o', '.so'])
CONTENT_KINDS = {
    'header': HEADER_SUFFIXES,
    'binary': BINARY_SUFFIXES,
    # A source set is source; it carries whatever the tree carries.  Only a
    # delivered component is restricted to an interface plus binaries.
    'source': None,
}

try:
    STRING_TYPES = (basestring,)
except NameError:
    STRING_TYPES = (str,)


class MaterializeError(Exception):
    """A declaration or layout problem that must fail the build."""


def _is_clean_relative(path):
    """Whether `path` is a non-empty, relative, `..`-free POSIX path."""
    if not isinstance(path, STRING_TYPES) or not path:
        return False
    if '\\' in path or path.startswith('/'):
        return False
    parts = path.split('/')
    return all(part not in ('', '.', '..') for part in parts)


def _normalize_destination(path):
    """Drops `.` segments and empty ones from a destination path.

    Producers spell the same destination in more than one way -- an atom that
    globs its own include directory emits `include/./foo.h`.  Normalising
    before the check keeps that from being an error, and makes the collision
    check see two spellings of one path as the collision it is.  `..` is not
    normalised away: it is rejected.
    """
    if not isinstance(path, STRING_TYPES):
        return path
    parts = [part for part in path.split('/') if part not in ('', '.')]
    return '/'.join(parts)


def resolve_locator(locator, roots):
    """Resolves an `out://x` / `source://y` locator to an absolute path."""
    if not isinstance(locator, STRING_TYPES) or LOCATOR_SEPARATOR not in locator:
        raise MaterializeError('invalid source locator: %r' % (locator,))
    root_id, relative = locator.split(LOCATOR_SEPARATOR, 1)
    if root_id not in roots:
        raise MaterializeError('unknown source root %r in locator %r'
                               % (root_id, locator))
    if not _is_clean_relative(relative):
        raise MaterializeError('invalid source locator: %r' % (locator,))
    root = os.path.realpath(roots[root_id])
    resolved = os.path.realpath(os.path.join(root, relative))
    if resolved != root and not resolved.startswith(root + os.sep):
        raise MaterializeError('source escapes root %r: %r' % (root_id, locator))
    if not os.path.isfile(resolved):
        raise MaterializeError('declared source does not exist: %s (%s)'
                               % (locator, resolved))
    return resolved


def read_layout(path):
    """Reads and validates the DDK layout contract."""
    with open(path, 'r') as layout_file:
        layout = json.load(layout_file)
    if not isinstance(layout, dict):
        raise MaterializeError('layout must be an object: %s' % path)
    if layout.get('format_version') != LAYOUT_FORMAT_VERSION:
        raise MaterializeError('unsupported layout format in %s: %r'
                               % (path, layout.get('format_version')))
    expected = set(['format_version', 'component', 'closed_source_roots',
                    'content', 'roots', 'atoms'])
    if set(layout) != expected:
        raise MaterializeError(
            'unexpected layout fields in %s: %s'
            % (path, ', '.join(sorted(set(layout) ^ expected))))

    component = layout['component']
    if not isinstance(component, STRING_TYPES) or not component:
        raise MaterializeError('layout component must be a non-empty string')

    roots = layout['roots']
    if (not isinstance(roots, list) or not roots
            or len(roots) != len(set(roots))
            or not all(_is_clean_relative(root) and '/' not in root
                       for root in roots)):
        raise MaterializeError('layout roots must be unique top-level names')

    content = layout['content']
    if not isinstance(content, dict) or set(content) != set(roots):
        raise MaterializeError(
            'layout content must name exactly the declared roots: %s'
            % ', '.join(sorted(roots)))
    for root, kinds in content.items():
        if (not isinstance(kinds, list) or not kinds
                or len(kinds) != len(set(kinds))
                or any(kind not in CONTENT_KINDS for kind in kinds)):
            raise MaterializeError(
                'layout content for %s must be a set of %s'
                % (root, '/'.join(sorted(CONTENT_KINDS))))

    closed = layout['closed_source_roots']
    if (not isinstance(closed, list)
            or len(closed) != len(set(closed))
            or any(not _is_clean_relative(root) for root in closed)):
        raise MaterializeError(
            'layout closed_source_roots must be unique relative paths')

    atoms = layout['atoms']
    if not isinstance(atoms, dict) or not atoms:
        raise MaterializeError('layout must declare at least one atom')
    for atom_id, entry in atoms.items():
        if not isinstance(atom_id, STRING_TYPES) or atom_id.count('/') != 1:
            raise MaterializeError('layout atom id must be "domain/name": %r'
                                   % (atom_id,))
        if not isinstance(entry, dict):
            raise MaterializeError('layout entry must be an object: %s'
                                   % atom_id)
        entry_fields = set(entry)
        required = set(['prefix', 'reason'])
        allowed = required | set(['optional', 'drop', 'exclude'])
        if not required.issubset(entry_fields) or not entry_fields <= allowed:
            raise MaterializeError('invalid layout entry fields for %s: %s'
                                   % (atom_id, ', '.join(sorted(entry_fields))))
        prefix = entry['prefix']
        if prefix != '' and not _is_clean_relative(prefix):
            raise MaterializeError('invalid layout prefix for %s: %r'
                                   % (atom_id, prefix))
        if not isinstance(entry['reason'], STRING_TYPES) or not entry['reason']:
            raise MaterializeError('layout entry for %s needs a reason'
                                   % atom_id)
        if not isinstance(entry.get('optional', False), bool):
            raise MaterializeError('layout "optional" must be a boolean: %s'
                                   % atom_id)
        if not isinstance(entry.get('exclude', False), bool):
            raise MaterializeError('layout "exclude" must be a boolean: %s'
                                   % atom_id)
        if entry.get('exclude') and entry.get('drop'):
            raise MaterializeError(
                'layout entry %s both excludes the atom and drops files from '
                'it; say one or the other' % atom_id)
        drop = entry.get('drop', [])
        if (not isinstance(drop, list)
                or len(drop) != len(set(drop))
                or not all(_is_clean_relative(item.rstrip('/'))
                           for item in drop
                           if isinstance(item, STRING_TYPES))
                or not all(isinstance(item, STRING_TYPES) for item in drop)):
            raise MaterializeError('invalid layout "drop" list for %s'
                                   % atom_id)
    return layout


def read_manifest(path):
    """Reads a portable SDK manifest."""
    with open(path, 'r') as manifest_file:
        manifest = json.load(manifest_file)
    if not isinstance(manifest, dict):
        raise MaterializeError('SDK manifest must be an object: %s' % path)
    atoms = manifest.get('atoms')
    metadata = manifest.get('meta')
    if not isinstance(atoms, list) or not isinstance(metadata, dict):
        raise MaterializeError('SDK manifest is missing atoms or metadata: %s'
                               % path)
    if metadata.get('source-locator-schema') != '1':
        raise MaterializeError(
            'SDK manifest is not portable: its sources are not out:// or '
            'source:// locators: %s' % path)
    return manifest


def atom_key(atom):
    identifier = atom.get('id')
    if not isinstance(identifier, dict):
        raise MaterializeError('SDK atom is missing an id')
    domain = identifier.get('domain')
    name = identifier.get('name')
    if not isinstance(domain, STRING_TYPES) or not domain:
        raise MaterializeError('SDK atom is missing a domain')
    if not isinstance(name, STRING_TYPES) or not name:
        raise MaterializeError('SDK atom is missing a name')
    return '%s/%s' % (domain, name)


def _drop_match(destination, pattern):
    """Whether an atom-relative destination is covered by a drop pattern.

    A pattern ending in "/" drops a whole directory; anything else names one
    exact destination.  Every pattern must match something, so a drop list
    cannot quietly go stale.
    """
    if pattern.endswith('/'):
        return destination.startswith(pattern)
    return destination == pattern


def _is_source_locator_under(locator, roots):
    """Whether a locator names a file in the source tree under one of `roots`."""
    if not isinstance(locator, STRING_TYPES):
        return None
    root_id, separator, relative = locator.partition(LOCATOR_SEPARATOR)
    if not separator or root_id != 'source':
        return None
    for root in roots:
        if relative == root or relative.startswith(root + '/'):
            return relative
    return None


def _content_allowed(destination, kinds):
    """Whether a destination is one of the kinds a root accepts."""
    if 'source' in kinds:
        return True
    suffix = destination[destination.rfind('.'):] if '.' in destination else ''
    for kind in kinds:
        if suffix in CONTENT_KINDS[kind]:
            return True
    return False


def plan_tree(manifest, layout, roots):
    """Computes destination -> source for the whole DDK tree.

    Raises on any atom the layout does not cover, any layout entry no atom
    uses, any destination outside the declared roots, and any two atoms
    claiming the same destination.
    """
    layout_atoms = layout['atoms']
    allowed_roots = set(layout['roots'])

    plan = {}
    provenance = {}
    used = set()
    undeclared = []

    for atom in manifest['atoms']:
        key = atom_key(atom)
        entry = layout_atoms.get(key)
        if entry is None:
            undeclared.append('%s (%s)' % (key, atom.get('gn-label', '?')))
            continue
        used.add(key)
        if entry.get('exclude', False):
            # Named, with a reason, but contributing nothing: an atom that
            # arrives through another's package_deps and has no place in the
            # component.  Naming it is still required, so it cannot slip in
            # unnoticed later.
            continue
        prefix = entry['prefix']
        drop_patterns = entry.get('drop', [])
        used_drops = set()
        for item in atom.get('files', []):
            if not isinstance(item, dict):
                raise MaterializeError('invalid file entry in atom %s' % key)
            destination = _normalize_destination(item.get('destination'))
            if not _is_clean_relative(destination):
                raise MaterializeError('invalid destination in atom %s: %r'
                                       % (key, destination))
            # Do not reuse `item` here: a Python 2 comprehension leaks its
            # variable into the enclosing scope and would clobber the file.
            dropped = [pattern for pattern in drop_patterns
                       if _drop_match(destination, pattern)]
            if dropped:
                used_drops.update(dropped)
                continue
            final = '%s/%s' % (prefix, destination) if prefix else destination
            root = final.split('/')[0]
            if root in allowed_roots:
                if not _content_allowed(final, layout['content'][root]):
                    raise MaterializeError(
                        'atom %s ships %s, which is neither %s under %s/. A '
                        'component carries an interface and the binaries '
                        'implementing it; drop it or say why it belongs.'
                        % (key, final,
                           ' nor '.join(sorted(layout['content'][root])),
                           root))
            if final.split('/')[0] not in allowed_roots:
                raise MaterializeError(
                    'atom %s installs outside the declared roots: %s '
                    '(roots: %s)'
                    % (key, final, ', '.join(sorted(allowed_roots))))
            closed_origin = _is_source_locator_under(
                item.get('source'), layout['closed_source_roots'])
            if closed_origin is not None and not _content_allowed(
                    destination, ['header', 'binary']):
                raise MaterializeError(
                    'atom %s exports %s from %s, which is closed source. Only '
                    'its headers and its compiled form may leave the tree; '
                    'ship the archive instead.'
                    % (key, closed_origin, closed_origin.split('/')[0]))
            source = resolve_locator(item.get('source'), roots)
            if final in plan:
                if plan[final] == source:
                    continue
                raise MaterializeError(
                    'DDK destination claimed twice: %s\n  %s <- %s\n  %s <- %s'
                    % (final, provenance[final], plan[final], key, source))
            plan[final] = source
            provenance[final] = key

        stale_drops = sorted(set(drop_patterns) - used_drops)
        if stale_drops:
            raise MaterializeError(
                'DDK layout drops nothing for atom %s: %s'
                % (key, ', '.join(stale_drops)))

    if undeclared:
        raise MaterializeError(
            'SDK atom(s) not covered by the DDK layout; say where they '
            'install, or leave them out of the manifest:\n  '
            + '\n  '.join(sorted(undeclared)))

    stale = []
    for key, entry in layout_atoms.items():
        if key not in used and not entry.get('optional', False):
            stale.append(key)
    if stale:
        raise MaterializeError(
            'DDK layout declares atom(s) the build graph does not provide:\n  '
            + '\n  '.join(sorted(stale)))

    return plan, provenance


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def make_dirs(path):
    try:
        os.makedirs(path)
    except OSError as error:
        if error.errno != errno.EEXIST or not os.path.isdir(path):
            raise


def install_file(source, destination):
    """Installs one file, preferring a hard link over a copy."""
    make_dirs(os.path.dirname(destination))
    if os.path.lexists(destination):
        os.remove(destination)
    try:
        os.link(source, destination)
        return
    except OSError:
        pass
    shutil.copyfile(source, destination)
    shutil.copymode(source, destination)


def write_tree(out_dir, plan, provenance, component, metadata, source_root,
               licenses=None):
    """Replaces `out_dir` with the planned tree and returns its contents.

    A component that carries no licence text is not deliverable: BSD and MIT
    both ask only that the notice travel with the code, and 257 of this one's
    files carry no inline notice at all.  So the licences go in here, before
    the manifest is written, and the manifest lists them like anything else --
    an export that forgot them would otherwise look complete.
    """
    staging = out_dir + '.staging'
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    make_dirs(staging)

    entries = []
    for destination in sorted(plan):
        source = plan[destination]
        install_file(source, os.path.join(staging, destination))
        entries.append({
            'path': destination,
            'atom': provenance[destination],
            'executable': bool(os.stat(source).st_mode & 0o111),
            'sha256': file_sha256(source),
        })

    # Which build this came out of.  Written before the manifest, so the
    # manifest covers it: a component that cannot say where it came from is
    # one nobody can answer a question about six months later.
    info = build_info.write(
        staging, component, source_root,
        build_info.repositories_for(source_root, plan.values()))
    entries.append({
        'path': build_info.BUILD_INFO_FILE,
        'atom': 'build-info',
        'executable': False,
        'sha256': file_sha256(os.path.join(staging,
                                           build_info.BUILD_INFO_FILE)),
    })

    if licenses is not None:
        declared = collect_licenses.read_declaration(licenses, source_root)
        coverage = collect_licenses.scan(staging, declared, set())
        against = collect_licenses.contradictions(staging, coverage)
        if against:
            raise MaterializeError(
                '%d file(s) disagree with the licence they are declared '
                'under:\n  %s'
                % (len(against),
                   '\n  '.join('%s (under %s): %s' % (path, name, why)
                                for path, name, why in against[:20])))
        collect_licenses.write(staging, coverage)
        for relative in sorted(_installed_licences(staging)):
            path = os.path.join(staging, relative)
            entries.append({
                'path': relative,
                'atom': 'licence',
                'executable': False,
                'sha256': file_sha256(path),
            })
        entries.sort(key=lambda entry: entry['path'])

    contents = {
        'format_version': CONTENTS_FORMAT_VERSION,
        'component': component,
        'target_arch': metadata.get('target-arch'),
        'host_arch': metadata.get('host-arch'),
        'files': entries,
    }
    contents_path = os.path.join(staging, CONTENTS_FILE)
    with open(contents_path, 'w') as contents_file:
        # Explicit separators: python 2's json writes ", " after a comma and
        # python 3 writes ",".  This file is the delivery's integrity record,
        # so it has to come out the same bytes whichever runs the export.
        json.dump(contents, contents_file, indent=2, sort_keys=True,
                  separators=(',', ': '))
        contents_file.write('\n')

    previous = out_dir + '.previous'
    if os.path.isdir(previous):
        shutil.rmtree(previous)
    if os.path.isdir(out_dir):
        os.rename(out_dir, previous)
    make_dirs(os.path.dirname(os.path.abspath(out_dir)))
    os.rename(staging, out_dir)
    if os.path.isdir(previous):
        shutil.rmtree(previous)
    return contents


def _installed_licences(staging):
    """The licence files collect_licenses just wrote into the staging tree."""
    found = [collect_licenses.NOTICE_NAME]
    licenses_dir = os.path.join(staging, collect_licenses.LICENSES_DIR)
    for name in sorted(os.listdir(licenses_dir)):
        found.append('%s/%s' % (collect_licenses.LICENSES_DIR, name))
    return found


def licence_inputs(licenses, source_root):
    """The declaration and every licence text it names, for the depfile."""
    if licenses is None:
        return []
    declared = collect_licenses.read_declaration(licenses, source_root)
    return [licenses] + sorted({entry['license_path'] for entry in declared})


def seal(component_dir):
    """Rewrites a component's manifest from the tree as it actually is.

    The manifest has to be the last thing written, and in a pipeline it is
    not: the trim writes it, then writes its own report, and the licence step
    then rewrites LICENSES/ for what is left.  A manifest written in the
    middle of that claimed two licence texts whose files had been trimmed away
    and missed the report entirely.

    So the last step re-seals: every file on disk is listed, and nothing else
    is.  Called after everything that puts a file in the component, and the
    check below then has something true to check.
    """
    contents_path = os.path.join(component_dir, CONTENTS_FILE)
    if not os.path.isfile(contents_path):
        raise MaterializeError('%s is not a component: no %s'
                               % (component_dir, CONTENTS_FILE))
    with open(contents_path, 'r') as handle:
        contents = json.load(handle)

    previous = dict((entry['path'], entry)
                    for entry in contents.get('files', []))
    entries = []
    for current, dirs, names in os.walk(component_dir):
        dirs.sort()
        for name in sorted(names):
            path = os.path.join(current, name)
            relative = os.path.relpath(path, component_dir).replace(os.sep,
                                                                    '/')
            if relative == CONTENTS_FILE:
                continue
            entries.append({
                'path': relative,
                'atom': previous.get(relative, {}).get('atom', 'delivery'),
                'executable': bool(os.stat(path).st_mode & 0o111),
                'sha256': file_sha256(path),
            })
    entries.sort(key=lambda entry: entry['path'])
    contents['files'] = entries
    with open(contents_path, 'w') as handle:
        json.dump(contents, handle, indent=2, sort_keys=True,
                  separators=(',', ': '))
        handle.write('\n')
    return contents


def check_manifest(component_dir):
    """Paths the manifest and the tree disagree about."""
    contents_path = os.path.join(component_dir, CONTENTS_FILE)
    with open(contents_path, 'r') as handle:
        listed = set(entry['path'] for entry in json.load(handle)['files'])
    on_disk = set()
    for current, dirs, names in os.walk(component_dir):
        dirs.sort()
        for name in sorted(names):
            relative = os.path.relpath(os.path.join(current, name),
                                       component_dir).replace(os.sep, '/')
            if relative != CONTENTS_FILE:
                on_disk.add(relative)
    return (sorted(listed - on_disk), sorted(on_disk - listed))


def write_depfile(path, stamp_name, sources):
    with open(path, 'w') as depfile:
        depfile.write('%s: %s\n' % (stamp_name, ' '.join(sorted(sources))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',
                        help='portable SDK manifest describing the component')
    parser.add_argument('--layout',
                        help='JSON layout contract for the component')
    parser.add_argument('--out',
                        help='directory to materialize the component into')
    parser.add_argument('--out-root',
                        help='build output root for "out://" locators')
    parser.add_argument('--source-root',
                        help='source root for "source://" locators')
    parser.add_argument('--licenses',
                        help='licence declaration; the component is covered '
                             'against it and carries the texts it names')
    parser.add_argument('--seal', metavar='COMPONENT',
                        help='rewrite this component\'s manifest from the '
                             'tree as it is, and stop if the two then still '
                             'disagree. Run last, after everything that puts '
                             'a file in it')
    parser.add_argument('--stamp', help='file to touch on success')
    parser.add_argument('--depfile', help='Ninja depfile to write')
    parser.add_argument('--depname', help='target name used inside --depfile')
    args = parser.parse_args()

    if args.depfile and not (args.depname and args.stamp):
        parser.error('--depfile requires --depname and --stamp')

    if args.seal:
        try:
            contents = seal(args.seal)
            missing, extra = check_manifest(args.seal)
        except MaterializeError as error:
            sys.stderr.write('error: %s\n' % error)
            return 1
        if missing or extra:
            sys.stderr.write(
                'error: the manifest and the tree disagree after sealing:\n'
                '  listed but absent: %s\n  present but unlisted: %s\n'
                % (missing or 'none', extra or 'none'))
            return 1
        sys.stdout.write('sealed %s: %d files\n'
                         % (args.seal, len(contents['files'])))
        return 0

    for required in ('manifest', 'layout', 'out', 'out_root', 'source_root'):
        if not getattr(args, required):
            parser.error('--%s is required' % required.replace('_', '-'))

    roots = {
        'out': args.out_root,
        'source': args.source_root,
    }
    try:
        layout = read_layout(args.layout)
        manifest = read_manifest(args.manifest)
        plan, provenance = plan_tree(manifest, layout, roots)
        if not plan:
            raise MaterializeError('the DDK manifest declares no files')
        contents = write_tree(os.path.abspath(args.out), plan, provenance,
                              layout['component'], manifest['meta'],
                              args.source_root, args.licenses)
    except (MaterializeError, collect_licenses.LicenseError) as error:
        sys.stderr.write('error: %s\n' % error)
        return 1

    if args.depfile:
        write_depfile(args.depfile, args.depname,
                      [args.manifest, args.layout]
                      + licence_inputs(args.licenses, args.source_root)
                      + sorted(set(plan.values())))
    if args.stamp:
        with open(args.stamp, 'w') as stamp:
            stamp.write('')
    sys.stdout.write('materialized %s: %d files -> %s\n'
                     % (layout['component'], len(contents['files']), args.out))
    return 0


if __name__ == '__main__':
    sys.exit(main())

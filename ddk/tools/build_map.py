#!/usr/bin/env python
# Copyright 2026 The GoldenRiverTek Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Reads how the product builds, from the build itself, and writes it down.

The DDK used to be declared in garnet: every delivered driver and library had
its BUILD.gn changed to say so, and restated its own flags, libraries and
headers for the standalone build.  That is forty-odd patches to garnet, and
porting the DDK to another branch meant porting all of them first.

Everything those declarations said is already known to the build.  GN has
evaluated every template, config and conditional by the time it writes its
Ninja files, and will say what it concluded -- `gn gen --ide=json` for the
graph, `gn desc --blame` for which config contributed each flag.  Ninja's own
dependency log says which headers every object actually read.  Together they
cover the lot, with nothing added to the tree being read.

So this reads them, into one JSON document: the build map.  For every target
that compiles or links, on every toolchain -- its sources, the headers its
objects read, its dependencies, the libraries it links, and each define,
flag and include directory together with where it came from.  It records;
it decides nothing about what ships.  That is the policy's job, and the map
is what the policy is applied to.

Where a value came from is the part that matters.  A driver's standalone
build needs its own `-DMT8678_I2C=1` and its own include directories, and
must not restate `-Wall` or the sysroot, which the delivered Makefile already
carries for everything.  GN merges all of them into one list; `--blame` is
what keeps them apart, and the map keeps them apart too.

Commands:

  map      read a product build and write its map
  modules  derive the module description of targets the map holds
  compare  derive modules for a delivery that was declared the old way, and
           report every difference from what it shipped
"""

from __future__ import print_function

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import partition  # noqa: E402

FORMAT_VERSION = 1

# What produces an object or links one.  Actions, copies and groups are how
# those get wired together; they are walked through, never recorded.
LINK_TYPES = ('executable', 'shared_library', 'loadable_module',
              'static_library', 'source_set')
COMPILE_SUFFIXES = ('.c', '.cc', '.cpp', '.cxx', '.S', '.s')
ATTRIBUTED = ('defines', 'cflags', 'cflags_c', 'cflags_cc', 'include_dirs')

# The standalone build's names for what GN calls these.  A source_set becomes
# an archive: the standalone build has no way to hand a consumer loose
# objects, and that is also what the declared build did.
MODULE_TYPES = {
    'loadable_module': 'driver',
    'executable': 'executable',
    'shared_library': 'shared_library',
    'static_library': 'static_library',
    'source_set': 'static_library',
}

# Paths the map keeps apart from source, since none of them is anything a
# half could ship: build output, and the compiler and sysroot the build ran.
TOOLCHAIN_PREFIXES = ('buildtools/', 'zircon/prebuilt/')

BUILD_PLACEHOLDER = '$build'


class MapError(Exception):
    """A build that cannot be read, or a map that does not say enough."""


def _fail(message):
    raise MapError(message)


def _run(command, cwd=None):
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    out, err = process.communicate()
    if process.returncode != 0:
        _fail('%s failed (%d): %s'
              % (' '.join(command), process.returncode,
                 err.decode('utf-8', 'replace').strip()[-2000:]))
    return out.decode('utf-8', 'replace')


# ---------------------------------------------------------------------------
# labels and paths


def strip_toolchain(label):
    return label.split('(', 1)[0]


def toolchain_of(label, default):
    if '(' in label:
        return label[label.index('(') + 1:label.rindex(')')]
    return default


def label_dir(label):
    """`//garnet/drivers/i2c:drv(...)` -> `garnet/drivers/i2c`."""
    return strip_toolchain(label).split(':', 1)[0][2:].rstrip('/')


def label_name(label):
    plain = strip_toolchain(label)
    if ':' in plain:
        return plain.split(':', 1)[1]
    return os.path.basename(plain.rstrip('/'))


def source_path(spelling, build_dir):
    """A GN path spelling, root-relative, with the build directory abstracted.

    `//garnet/lib/x.cc` -> `garnet/lib/x.cc`; `//out/ddk-map/gen/x.h` ->
    `$build/gen/x.h`.  The map is read on one build directory and applied to
    another, so the directory's own name must not be in it.
    """
    if not spelling.startswith('//'):
        return spelling
    relative = spelling[2:]
    if build_dir and (relative == build_dir or
                      relative.startswith(build_dir + '/')):
        rest = relative[len(build_dir):].lstrip('/')
        return BUILD_PLACEHOLDER + ('/' + rest if rest else '')
    return relative.rstrip('/') if relative.endswith('/') else relative


def classify(path):
    """What a root-relative path is: source, build output, or toolchain."""
    if path.startswith(BUILD_PLACEHOLDER) or path.startswith('out/'):
        return 'build'
    if path.startswith('/') or path.startswith('..'):
        return 'outside'
    if path.startswith(TOOLCHAIN_PREFIXES):
        return 'toolchain'
    return 'source'


# ---------------------------------------------------------------------------
# reading the build


def read_json_output(text):
    """JSON out of a tool that prints other things before it.

    `gn desc` runs the build's exec_script calls, and this build's print what
    signing keys they found; the document starts at the first brace.
    """
    start = text.find('{')
    if start < 0:
        _fail('no JSON in the output: %r' % text[:200])
    return json.loads(text[start:])


def parse_blame(lines):
    """`--blame` output for one field, grouped by the config it came from.

    GN writes `From <label>`, then `(Added by <where>)`, then the values, for
    each contributor in turn.  Both are kept.  `From` says whose config it
    is; `Added by` says who put it on this target -- BUILDCONFIG.gn for the
    toolchain's defaults, the target's own BUILD.gn, or the BUILD.gn of a
    dependency whose public config travelled up.  That second one is what
    tells a driver's own flags from ones it inherited.  A target's own values
    come with no `Added by` at all.
    """
    groups = []
    current = None
    for line in lines:
        stripped = line.strip()
        if line.startswith('From '):
            current = {'from': line[len('From '):].strip(), 'added_by': None,
                       'values': []}
            groups.append(current)
            continue
        if stripped.startswith('(Added by'):
            if current is not None:
                current['added_by'] = stripped[len('(Added by'):].rstrip(
                    ')').strip()
            continue
        if not stripped:
            continue
        if current is None:
            current = {'from': None, 'added_by': None, 'values': []}
            groups.append(current)
        current['values'].append(stripped)
    return groups


def parse_ninja_deps(text, build_dir, root):
    """`ninja -t deps` output: object -> the files its compilation read.

    Paths in the log are relative to the build directory, or absolute; both
    come back root-relative, or absolute when outside the root.
    """
    objects = {}
    current = None
    header = re.compile(r'^(\S.*): #deps \d+, deps mtime \d+ \((\w+)\)$')
    for raw in text.splitlines():
        match = header.match(raw)
        if match:
            current = {'status': match.group(2), 'files': []}
            objects[match.group(1)] = current
            continue
        if current is None or not raw.startswith('    '):
            continue
        spelling = raw.strip()
        if not spelling:
            continue
        absolute = spelling if os.path.isabs(spelling) else \
            os.path.normpath(os.path.join(build_dir, spelling))
        relative = os.path.relpath(absolute, root)
        current['files'].append(absolute if relative.startswith('..')
                                else relative.replace(os.sep, '/'))
    return objects


def describe(gn, gn_out, toolchains, default_toolchain):
    """`gn desc --blame` for every target on every toolchain named."""
    described = {}
    for toolchain in toolchains:
        pattern = '//*' if toolchain == default_toolchain \
            else '//*(%s)' % toolchain
        text = _run([gn, 'desc', '.', pattern, '--format=json', '--blame'],
                    cwd=gn_out)
        described.update(read_json_output(text))
    return described


# ---------------------------------------------------------------------------
# the map


def build_map(project, described, deps, gn_out_label=None):
    """The map, from what GN and Ninja said.

    `project` is project.json, `described` the merged `gn desc --blame`
    output, `deps` the parsed Ninja dependency log.  Nothing is read from the
    tree or run here, so the whole derivation can be tested on documents.
    """
    settings = project.get('build_settings', {})
    default_toolchain = settings.get('default_toolchain', '')
    build_dir = (gn_out_label or settings.get('build_dir', ''))
    build_dir = build_dir[2:].rstrip('/') if build_dir.startswith('//') \
        else build_dir.rstrip('/')

    targets = {}
    no_depfile = 0
    for label in sorted(project.get('targets', {})):
        target = project['targets'][label]
        if target.get('type') not in LINK_TYPES:
            continue
        blame = described.get(label)
        if blame is None:
            _fail('%s is in project.json but gn desc said nothing about it; '
                  'the toolchain it builds on was not described' % label)
        sources = [source_path(s, build_dir)
                   for s in target.get('sources', [])]

        headers = set()
        unread = []
        for spelling, outputs in sorted(target.get('source_outputs',
                                                   {}).items()):
            if not spelling.endswith(COMPILE_SUFFIXES):
                continue
            objects = [o for o in outputs if o.endswith(('.o', '.obj'))]
            found = False
            for obj in objects:
                record = deps.get(obj)
                if record is None:
                    continue
                found = True
                for path in record['files']:
                    headers.add(path)
            if not found:
                unread.append(source_path(spelling, build_dir))
                no_depfile += 1
        compiled = set(s for s in sources if s.endswith(COMPILE_SUFFIXES))

        objects = sorted(set(
            o for outputs in target.get('source_outputs', {}).values()
            for o in outputs if o.endswith(('.o', '.obj'))))
        entry = {
            'objects': objects,
            'outputs': [source_path(o, build_dir)
                        for o in target.get('outputs', [])],
            'testonly': bool(target.get('testonly')),
            'type': target['type'],
            'toolchain': toolchain_of(label, default_toolchain),
            'dir': label_dir(label),
            'name': label_name(label),
            'output_name': target.get('output_name') or label_name(label),
            'sources': sources,
            'headers': sorted(h for h in headers if h not in compiled),
            'unread': unread,
            'deps': sorted(target.get('deps', [])),
            'libs': [source_path(l, build_dir)
                     for l in target.get('libs', [])],
        }
        for field in ATTRIBUTED:
            groups = parse_blame(blame.get(field, []))
            if field == 'include_dirs':
                for group in groups:
                    group['values'] = [source_path(v, build_dir)
                                       for v in group['values']]
            entry[field] = groups
        targets[label] = entry

    # Every target's dependencies, whatever it is.  A driver reaches Zircon's
    # ddk library through a group, and a closure that stopped at the first
    # target that does not compile would lose it there.
    graph = {}
    for label, target in project.get('targets', {}).items():
        graph[label] = {'type': target.get('type'),
                        'deps': sorted(target.get('deps', [])),
                        'libs': [source_path(l, build_dir)
                                 for l in target.get('libs') or []]}

    return {
        'format_version': FORMAT_VERSION,
        'default_toolchain': default_toolchain,
        'build_dir': build_dir,
        'targets': targets,
        'graph': graph,
        'objects_without_depfile': no_depfile,
    }


# ---------------------------------------------------------------------------
# deriving a module


BUILDCONFIG = '//build/config/BUILDCONFIG.gn'

# Zircon is closed: what its public configs add is what libddk's headers and
# the delivered Makefile already carry, and nothing of it belongs in a
# module's own description.
CLOSED_PREFIXES = ('//zircon/',)


def origin_kind(group, directory):
    """Who put a group of values on a target in `directory`.

      default    the toolchain's defaults, which every module gets anyway
      local      the target itself, or a config declared beside it
      applied    a config declared elsewhere that its own BUILD.gn applies,
                 such as //garnet/public:config
      closed     a Zircon config: libddk and the Makefile provide it
      inherited  a dependency's public config
    """
    added_by = group.get('added_by')
    source = group.get('from') or ''
    if source.startswith(CLOSED_PREFIXES):
        return 'closed'
    if added_by is None:
        return 'local' if label_dir(source) == directory else 'inherited'
    if added_by.split(':')[0] == BUILDCONFIG:
        return 'default'
    where = added_by.split(':')[0]
    if where.endswith('.gn') or where.endswith('.gni'):
        where = os.path.dirname(where)
    if where.startswith(CLOSED_PREFIXES):
        return 'closed'
    if label_dir(where + ':x') == directory:
        return 'local' if label_dir(source) == directory else 'applied'
    return 'inherited'


def values_of(entry, field, kinds):
    values = []
    for group in entry.get(field, []):
        if origin_kind(group, entry['dir']) in kinds:
            for value in group['values']:
                if value not in values:
                    values.append(value)
    return values


def library_name(spelling):
    """`$build/obj/zircon/public/lib/ddk/libddk.a` -> `ddk`; `zircon` stays."""
    base = os.path.basename(spelling)
    for suffix in ('.a', '.so'):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    if base.startswith('lib') and base != spelling:
        base = base[3:]
    return base


# What the delivered Makefile names its own directories: a module may not.
RESERVED_NAMES = frozenset(['lib', 'bin', 'drivers', 'build'])


def default_module_name(entry):
    """What a module is called when the policy does not say.

    Its output name, without the `lib` a shared library's file name carries.
    A module's name is its `-l` spelling and its installed file name, so it is
    part of what the delivery promises; this is only the suggestion.  GN
    target names collide freely -- five libraries here are called `cpp` --
    and where they do, or where the name is too plain to ship, the policy
    names the module itself.
    """
    name = entry['output_name']
    if entry['type'] in ('shared_library', 'static_library') and \
            name.startswith('lib') and len(name) > 3:
        name = name[3:]
    return name


def provided_name(entry, provided, label=None):
    """The libddk archive a target is, if it is one.

    `provided` holds archive names, and may map a label to one: libddk's
    libnbl_fwk_hee.a is built from //third_party/hee-trusty/nbl_fwk:nbl_fwk,
    and nothing about either name says so.  A label entry wins over matching
    by name.
    """
    if label is not None and hasattr(provided, 'get'):
        named = provided.get(strip_toolchain(label))
        if named:
            return named
    for name in (default_module_name(entry), entry['name']):
        for spelling in (name, name[3:] if name.startswith('lib') else None):
            if spelling and spelling in provided:
                return provided[spelling] if hasattr(provided, 'get') \
                    else spelling
    return None


def module_of(modules, label):
    """The module a label is, whatever toolchain it was reached on.

    A library is one target per toolchain it builds on, and a driver on the
    shared toolchain depends on the shared variant of it.  Being a module is
    a property of the target, not of the variant.
    """
    return modules.get(label) or modules.get(strip_toolchain(label))


FOLD = set()


def is_foldable(the_map, label, modules, provided=()):
    """A source set that compiles into whatever depends on it.

    GN links a source set's objects straight into its dependent; there is no
    archive in between.  liblua is a shared library with no sources of its
    own and three source sets under it, and a module description for it has
    to name their sources.  Not a module in its own right, not Zircon's, not
    something libddk ships, and with something to compile.
    """
    if module_of(modules, label):
        return False
    entry = the_map['targets'].get(label)
    if entry is None:
        return False
    if strip_toolchain(label) in FOLD and entry['type'] in (
            'static_library', 'shared_library', 'source_set'):
        return True
    if entry['type'] != 'source_set':
        return False
    if strip_toolchain(label).startswith(CLOSED_PREFIXES):
        return False
    if provided_name(entry, provided, label):
        return False
    return any(s.endswith(COMPILE_SUFFIXES) for s in entry['sources'])


def _reach(the_map, label, modules, provided):
    """Foldable source sets under `label`, with their distance, stopping at
    modules and at archives libddk provides."""
    targets = the_map['targets']
    graph = the_map.get('graph', {})
    found = {}
    frontier = [(d, 1) for d in targets[label]['deps']]
    seen = set([label])
    while frontier:
        dependency, depth = frontier.pop(0)
        if dependency in seen:
            continue
        seen.add(dependency)
        if module_of(modules, dependency):
            continue
        target = targets.get(dependency)
        if target is not None and provided_name(target, provided,
                                                dependency):
            continue
        if target is not None and is_foldable(the_map, dependency, modules,
                                              provided):
            key = strip_toolchain(dependency)
            found[key] = min(found.get(key, depth), depth)
        onward = target['deps'] if target is not None \
            else graph.get(dependency, {}).get('deps', [])
        frontier.extend((o, depth + 1) for o in onward)
    return found


def assign_folds(the_map, modules, provided=()):
    """Which module each foldable source set compiles into.

    A source file can belong to one module only: fold it into two and the
    standalone link has the same symbols twice.  fxl_lib sits under fxl and
    is also a direct dependency of fsl_io, and it is fxl's.

    A source set in a module's own directory is that module's.  One only a
    single module reaches is that module's.  One several reach goes to the
    nearest, and is reported, because which module a shared source set
    belongs to is a decision the build does not make and the policy should.
    """
    reach = {}
    for label in sorted(modules):
        if label not in the_map['targets']:
            continue
        # Only a library can own a source set on another module's behalf.
        # Two drivers are two .so files and neither links the other, so a
        # source set both of them use is compiled into each.
        kind = MODULE_TYPES.get(the_map['targets'][label]['type'])
        if kind not in ('static_library', 'shared_library'):
            continue
        for key, depth in _reach(the_map, label, modules, provided).items():
            reach.setdefault(key, []).append((depth, modules[label], label))
    owners, ambiguous = {}, {}
    for key, candidates in sorted(reach.items()):
        names = sorted(set(name for _, name, _ in candidates))
        same_dir = sorted(set(name for _, name, label in candidates
                              if label_dir(label) == label_dir(key)))
        if same_dir:
            owners[key] = same_dir[0]
        elif len(names) == 1:
            owners[key] = names[0]
        else:
            owners[key] = sorted(candidates)[0][1]
            ambiguous[key] = names
    return owners, ambiguous


def walk(the_map, label, modules, provided=(), owners=None):
    """What a module is made of and what it links.

    Returns the source sets folded into it, the modules it links, and the
    other libraries it links.  Walks through groups and anything else that is
    only wiring, and stops at a module -- whose own libraries are its own;
    the standalone build closes over MODULE_DEPS.  A source set another
    module owns is a dependency on that module, not a second copy.

    Below an archive libddk provides, the walk goes on, but only to collect
    what that archive links in turn: vm_config is a prebuilt archive and
    still needs protobuf_full beside it.  Nothing under one is folded -- its
    sources are already inside it -- and nothing under one makes a module a
    dependency.  An archive is a finished binary; what it needs to link is a
    question about libddk, answered by its undefined symbols, not by what its
    BUILD.gn happened to list.  blk_crypto lists fxl and uses none of it, and
    taking that at its word made MTK's UFS driver depend on GRT's source.
    Those edges are returned, so they can be reported rather than lost.
    """
    targets = the_map['targets']
    graph = the_map.get('graph', {})
    me = module_of(modules, label)
    folded, module_deps, libraries, through_archive = [], [], [], []
    seen = set()
    stack = [(d, False) for d in reversed(targets[label]['deps'])]

    def depend(name):
        if name and name != me and name not in module_deps:
            module_deps.append(name)

    def link(name):
        if name and name not in libraries:
            libraries.append(name)

    while stack:
        dependency, inside_archive = stack.pop()
        if (dependency, inside_archive) in seen:
            continue
        seen.add((dependency, inside_archive))
        name = module_of(modules, dependency)
        if name:
            if inside_archive:
                if name != me and name not in through_archive:
                    through_archive.append(name)
            else:
                depend(name)
            continue
        target = targets.get(dependency)
        node = graph.get(dependency, {})
        entry = target or {'type': node.get('type'), 'sources': [],
                           'name': label_name(dependency),
                           'output_name': label_name(dependency)}
        archive = provided_name(entry, provided, dependency)

        # A Zircon library reaches GN as a group whose libs name the archive
        # and what it needs; static libraries do not collect those, so a
        # library module has to read them where they are.
        for spelling in node.get('libs', []):
            if strip_toolchain(dependency).startswith(CLOSED_PREFIXES) or \
                    library_name(spelling) in provided:
                link(library_name(spelling))

        if archive is not None:
            link(archive)
            inside_archive = True
        elif inside_archive:
            pass
        elif target is not None and is_foldable(the_map, dependency, modules,
                                                provided):
            owner = (owners or {}).get(strip_toolchain(dependency), me)
            if owner != me:
                depend(owner)
                continue
            folded.append(dependency)
        elif target is not None and target['type'] in (
                'static_library', 'shared_library') and \
                any(x.endswith(COMPILE_SUFFIXES) for x in target['sources']):
            link(default_module_name(target))
        # A library with nothing of its own to compile is only an aggregate
        # -- //garnet/public/lib/fsl bundles the fsl pieces -- and is looked
        # through like a group.
        onward = target['deps'] if target is not None else node.get('deps', [])
        stack.extend((o, inside_archive) for o in reversed(onward))
    through_archive = [n for n in through_archive if n not in module_deps]
    return folded, module_deps, libraries, through_archive


def derive_module(the_map, label, modules, rules, abi_defines=(),
                  abi_cxx_flags=(), provided=(), owners=None,
                  scope='garnet'):
    """The module description `label` implies, as the delivered Makefile reads it.

    `modules` maps the label of every target that ships as a module to its
    name.  It decides which dependencies are other modules and which are
    libraries, and where a module's name comes from.
    """
    entry = the_map['targets'].get(label)
    if entry is None:
        _fail('%s is not a compiling target in the map' % label)
    module_type = MODULE_TYPES.get(entry['type'])
    if module_type is None:
        _fail('%s is a %s, which is not a module' % (label, entry['type']))

    owner = 'grt'
    directory = entry['dir']
    if rules is not None and (directory == scope or
                              directory.startswith(scope + '/')):
        inside = directory[len(scope):].lstrip('/')
        part, _ = partition.owner_of(inside, rules)
        owner = part[:-len('_ddk')] if part.endswith('_ddk') else part

    folded, module_deps, libraries, through_archive = walk(
        the_map, label, modules, provided, owners)
    members = [entry] + [the_map['targets'][f] for f in folded]

    sources, include_dirs, cflags = [], [], []

    def add(sequence, value):
        if value not in sequence:
            sequence.append(value)

    for member in members:
        for source in member['sources']:
            if source.endswith(COMPILE_SUFFIXES):
                add(sources, source)
        for directory_value in values_of(member, 'include_dirs', ('local',)):
            if classify(directory_value) == 'source':
                add(include_dirs, directory_value)
        # Defines change what the code is, wherever they came from: a
        # library's public define decides how its headers compile in the
        # consumer.  Flags do too, except warnings, which change nothing
        # about the object -- fdt's public -Wno-sign-compare stays with fdt.
        for define in values_of(member, 'defines',
                                ('local', 'applied', 'inherited')):
            if '-D%s' % define not in abi_defines:
                add(cflags, '-D%s' % define)
        for field in ('cflags', 'cflags_c', 'cflags_cc'):
            for flag in values_of(member, field, ('local', 'applied')):
                add(cflags, flag)
            for flag in values_of(member, field, ('inherited',)):
                if not flag.startswith('-W'):
                    add(cflags, flag)

    # The Makefile gives every C++ module the product's defaults, and a
    # target that took one off in its BUILD.gn has to say so the other way:
    # guest drops no_exceptions, so its module needs -fexceptions.
    has_cxx = any(s.endswith(('.cc', '.cpp', '.cxx')) for s in sources)
    if has_cxx:
        merged = set()
        for group in entry.get('cflags_cc', []) + entry.get('cflags', []):
            merged.update(group['values'])
        for flag in abi_cxx_flags:
            if flag.startswith('-fno-') and flag not in merged:
                add(cflags, '-f' + flag[len('-fno-'):])

    for spelling in entry['libs']:
        add(libraries, library_name(spelling))

    # Only what can be linked.  The product links librng.a beside
    # libcrypto.so because boringssl's group names both; libddk ships crypto
    # as a shared library with rng already inside it, and has no librng.a to
    # give.  A -l nothing answers fails the link, so it is left out and said
    # -- whether it is genuinely not needed, or libddk is missing something,
    # is the policy's to decide and the link's to prove.
    names = set(module_of(modules, l) for l in modules) | set(
        modules.values())
    linkable, unsatisfied = [], []
    for library in libraries:
        if not provided or library in provided or library in names:
            linkable.append(library)
        else:
            unsatisfied.append(library)

    return {
        'MODULE_NAME': module_of(modules, label) or
        default_module_name(entry),
        'MODULE_OWNER': owner,
        'MODULE_TYPE': module_type,
        'MODULE_DEPS': module_deps,
        'MODULE_SRCS': sources,
        'MODULE_INCLUDE_DIRS': include_dirs,
        'MODULE_LIBS': linkable,
        'MODULE_CFLAGS': cflags,
        'folded': folded,
        'unsatisfied': unsatisfied,
        'through_archive': through_archive,
    }


# ---------------------------------------------------------------------------
# choosing what ships


POLICY_KEYS = ('deliver', 'exclude', 'binary', 'source')
ROOT_TYPES = ('loadable_module', 'executable')
LIBRARY_TYPES = ('static_library', 'shared_library', 'source_set')
HOST_TOOLCHAINS = ('host',)


def read_policy(path):
    with io.open(path, encoding='utf-8') as handle:
        policy = json.load(handle)
    if policy.get('format_version') != FORMAT_VERSION:
        _fail('%s is format %r' % (path, policy.get('format_version')))
    for key in POLICY_KEYS:
        patterns = policy.get(key, [])
        if not isinstance(patterns, list):
            _fail('%s: %s must be a list' % (path, key))
        for pattern in patterns:
            if not pattern.startswith('//') or '(' in pattern:
                _fail('%s: %r is not a label pattern without a toolchain'
                      % (path, pattern))
        policy[key] = patterns
    return policy


def specificity(pattern, label):
    """How closely `pattern` names `label`, or None if it does not.

    An exact label is closer than a directory, and a directory closer than a
    subtree, so a narrower rule can override a broad one whatever order the
    file lists them in: //third_party/* is binary, and
    //third_party/littlefs:littlefs is still source.
    """
    plain = strip_toolchain(label)
    directory = plain.split(':', 1)[0]
    if pattern.endswith('/*'):
        base = pattern[:-2]
        if directory == base or directory.startswith(base + '/'):
            return 3 * len(base)
        return None
    if pattern.endswith(':*'):
        return 3 * len(pattern) + 1 if directory == pattern[:-2] else None
    return 3 * len(pattern) + 2 if plain == pattern else None


def best(patterns, label):
    found = [specificity(p, label) for p in patterns]
    found = [f for f in found if f is not None]
    return max(found) if found else None


def is_host(label, default_toolchain):
    toolchain = toolchain_of(label, default_toolchain)
    return any(h in toolchain.split(':')[-1] for h in HOST_TOOLCHAINS)


def compiles(entry):
    return any(s.endswith(COMPILE_SUFFIXES) for s in entry['sources'])


def is_generated(entry):
    """Code GN generated -- FIDL and protobuf bindings.  Always shipped
    compiled: the source is an artefact of a generator the customer does not
    have, and regenerating it is how two ABIs end up with one name."""
    compiled = [s for s in entry['sources'] if s.endswith(COMPILE_SUFFIXES)]
    return bool(compiled) and all(classify(s) == 'build' for s in compiled)


def object_symbols(the_map, labels, build_dir, nm, cache):
    """The global symbols the objects of `labels` define."""
    found = set()
    for label in labels:
        for obj in the_map['targets'][label].get('objects', []):
            path = os.path.join(build_dir, obj)
            if path not in cache:
                cache[path] = set()
                if os.path.isfile(path):
                    text = _run([nm, '--defined-only', '--extern-only', '-P',
                                 path])
                    cache[path] = set(line.split()[0]
                                      for line in text.splitlines()
                                      if len(line.split()) >= 2 and
                                      not line.split()[0].endswith(':'))
            found |= cache[path]
    return found


LIBDDK = 'libddk'


def lock_surface(lock):
    """Every path the last release shipped -> where its symbols are keyed.

    Splitting libddk left most of it inside grt_ddk, where the lock records
    it under `half_components` instead of `files`.  Those archives are still
    released: a module that linked libfbl.a still links it, and a binary is
    still named after the released library exporting its symbols.  Reading
    `files` alone forgot 34 libraries at once -- every module dropped them
    from MODULE_LIBS, and the sealed builds failed to link.  The symbol
    table keys a half's copy by where it lives, so the key travels with the
    path.
    """
    surface = dict((path, path) for path in lock.get('files', {}))
    for half in sorted(lock.get('half_components', {})):
        for path in lock['half_components'][half]:
            surface.setdefault(path, '%s/%s/%s' % (half, LIBDDK, path))
    return surface


def lock_libraries(lock, symbols):
    """name -> exported symbols, for every library the last release shipped."""
    libraries = {}
    for path, key in lock_surface(lock).items():
        if path.startswith(('lib/', 'sysroot/')) and \
                path.endswith(('.a', '.so')):
            libraries[library_name(path)] = set(symbols.get(key, []))
    return libraries


def select(the_map, policy, lock=None, symbols=None, build_dir=None, nm=None):
    """Which targets ship as modules, which as libddk libraries, by what name.

    Roots are the drivers and programs the policy delivers, and any library
    it names outright.  Everything else follows from what the roots link:

      compiled, if the policy makes it binary or GN generated it -- named
      after the library in the last release that exports the same symbols,
      so libnbl_fwk_hee.a is still that even though its target is nbl_fwk

      a module, if it is a library in its own right: a static or shared
      library, or a source set more than one program or library links

      folded into what uses it, otherwise -- a source set one target uses, or
      one only drivers use, or one a library beside it already contains

    A module the last release shipped keeps its name.  A new one is named by
    rule, and qualified by its directory where the rule would collide.
    """
    targets = the_map['targets']
    graph = the_map.get('graph', {})
    default = the_map.get('default_toolchain', '')
    lock = lock or {'modules': {}, 'files': {}}
    released = lock_libraries(lock, symbols or {})

    # One variant per target: the one the product builds for the device.
    variants = {}
    rank = {'loadable_module': 0, 'executable': 1, 'shared_library': 2,
            'static_library': 3, 'source_set': 4}
    for label, entry in targets.items():
        if is_host(label, default):
            continue
        plain = strip_toolchain(label)
        current = variants.get(plain)
        if current is None or rank[entry['type']] < rank[
                targets[current]['type']] or (
                entry['type'] == targets[current]['type'] and '(' not in label):
            variants[plain] = label

    roots = []
    for plain, label in sorted(variants.items()):
        entry = targets[label]
        if entry.get('testonly'):
            continue
        if best(policy['deliver'], label) is None or \
                best(policy['exclude'], label) is not None:
            continue
        # A directory or a subtree delivers its drivers and programs; a
        # library is a root only when the policy names it outright.
        named = any(not p.endswith('*') and specificity(p, label)
                    for p in policy['deliver'])
        if entry['type'] in ROOT_TYPES or (entry['type'] in LIBRARY_TYPES and
                                           named):
            roots.append(label)

    # Everything the roots reach, and the reverse edges to walk back up.
    reached = set()
    parents = {}
    stack = list(roots)
    while stack:
        label = stack.pop()
        if label in reached:
            continue
        reached.add(label)
        entry = targets.get(label)
        deps = entry['deps'] if entry else graph.get(label, {}).get('deps', [])
        for dependency in deps:
            if is_host(dependency, default):
                continue
            parents.setdefault(dependency, set()).add(label)
            stack.append(dependency)

    def foldable_children(label):
        entry = targets[label]
        return [d for d in entry['deps']
                if d in targets and targets[d]['type'] == 'source_set' and
                compiles(targets[d]) and label_dir(d) == entry['dir']]

    def is_aggregate(label):
        """A library with nothing of its own: //garnet/public/lib/fsl bundles
        pieces that live elsewhere and is looked through.  liblua has no
        sources either, but owns the source sets beside it, and is a library."""
        entry = targets.get(label)
        return entry is not None and \
            entry['type'] in ('static_library', 'shared_library') and \
            not compiles(entry) and not foldable_children(label)

    reached_variants = {}
    for label in reached:
        reached_variants.setdefault(strip_toolchain(label), []).append(label)

    def users_of(label):
        """The link targets that use `label`, on any toolchain, looking
        through groups, copies and aggregates -- a program reaches fsl_io
        through the fsl group, and fxl_lib is used by libfxl.so on the shared
        toolchain while its default variant is the one chosen."""
        found = set()
        seen = set()
        stack = []
        for variant in reached_variants.get(strip_toolchain(label), [label]):
            stack.extend(parents.get(variant, ()))
        while stack:
            parent = stack.pop()
            if parent in seen:
                continue
            seen.add(parent)
            if parent in targets and not is_aggregate(parent):
                found.add(strip_toolchain(parent))
                continue
            stack.extend(parents.get(parent, ()))
        return found

    # Names.  The last release's first, by directory and then by sources.
    by_dir = {}
    for name, record in lock.get('modules', {}).items():
        by_dir.setdefault(record['dir'], []).append((name, record))
    names = {}
    taken = set()

    def sibling_names(entry, label):
        """What the other targets in a directory are called."""
        names = set()
        for other, record in targets.items():
            if other != label and record['dir'] == entry['dir']:
                names.add(default_module_name(record))
                names.add(record['name'])
        return names

    def released_name(label):
        """The module the last release shipped this target as, if any.

        Matched in its own directory, by name first and then by how much of
        its sources the released module had.  Not by type: fxl is a shared
        library in the product and was released as an archive, and it is
        still fxl.  Sources are counted with the source sets beside it that
        it contains, since those are what the released module compiled.
        """
        entry = targets.get(label)
        if entry is None:
            return None
        candidates = by_dir.get(entry['dir'], [])
        compiled = set(s for s in entry['sources'] if s.endswith(COMPILE_SUFFIXES))
        if entry['type'] != 'source_set':
            for child in foldable_children(label):
                compiled |= set(s for s in targets[child]['sources']
                                if s.endswith(COMPILE_SUFFIXES))
        best_name, best_overlap = None, 0.0
        best_covered, best_coverage = None, 0.0
        for name, record in candidates:
            if name in (default_module_name(entry), entry['name']):
                return name
            recorded = set(record['sources'])
            if recorded and compiled:
                overlap = len(recorded & compiled) / float(len(recorded | compiled))
                if overlap > best_overlap:
                    best_name, best_overlap = name, overlap
                # The record is the module as shipped, with everything
                # folded into it: guest_lib carries callback's eight files
                # beside guest:lib's own four.  The target is still that
                # module when the record has all of its sources, however
                # much else the record holds -- unless a target beside it
                # is that module by name: fxl's record holds fxl_logging's
                # sources because fxl_logging was folded into fxl, and fxl
                # is still there to be fxl.
                if name not in sibling_names(entry, label):
                    coverage = len(recorded & compiled) / float(len(compiled))
                    if coverage > best_coverage:
                        best_covered, best_coverage = name, coverage
        if best_overlap >= 0.5:
            return best_name
        if best_coverage >= 0.9:
            return best_covered
        return None


    root_plains = set(strip_toolchain(r) for r in roots)
    binary, modules, folded, notes = {}, {}, [], []
    nm_cache = {}

    candidates = []
    for plain in sorted(set(strip_toolchain(l) for l in reached)):
        label = variants.get(plain)
        if label is None or plain in root_plains:
            continue
        entry = targets[label]
        if entry['type'] not in LIBRARY_TYPES or entry.get('testonly'):
            continue
        if not compiles(entry) and not (
                entry['type'] != 'source_set' and foldable_children(label)):
            continue
        closed = best(policy['binary'], label)
        opened = best(policy['source'], label)
        if is_generated(entry) or (closed is not None and
                                   (opened is None or closed > opened)):
            binary[plain] = entry
            continue
        candidates.append((plain, label, entry))

    # First, source sets a library beside them contains: lua_core is liblua's
    # and fxl_lib is fxl's, however much they look like modules of their own.
    owner_of_set = {}
    for plain, label, entry in candidates:
        if entry['type'] != 'source_set':
            continue
        owners = sorted(u for u in users_of(label)
                        if label_dir(u) == entry['dir'] and variants.get(u) and
                        targets[variants[u]]['type'] in
                        ('static_library', 'shared_library'))
        if owners:
            owner_of_set[plain] = owners[0]
            folded.append(plain)

    def effective(user):
        return owner_of_set.get(user, user)

    for plain, label, entry in candidates:
        if plain in owner_of_set:
            continue
        near = set(effective(u) for u in users_of(label))
        # What the last release shipped as a module stays one.
        if released_name(label):
            modules[plain] = label
            continue
        if entry['type'] in ('static_library', 'shared_library'):
            # A library of a module's own, used by that module alone and not
            # shipped before, is part of it: fxl_logging is a shared library
            # beside fxl that only fxl links, and the release it follows
            # compiled it into fxl.
            only = list(near)[0] if len(near) == 1 else None
            if only and label_dir(only) == entry['dir']:
                folded.append(plain)
            else:
                modules[plain] = label
            continue
        linkers = [u for u in near if variants.get(u) and
                   targets[variants[u]]['type'] != 'loadable_module']
        if len(linkers) < 2:
            folded.append(plain)
        else:
            modules[plain] = label
    folded.sort()

    everything = [(strip_toolchain(r), r) for r in roots] + sorted(modules.items())
    pending = []
    for plain, label in everything:
        name = released_name(label)
        if name and name not in taken:
            names[label] = name
            taken.add(name)
        else:
            pending.append(label)
    for label in pending:
        entry = targets[label]
        name = default_module_name(entry)
        if name in taken or name in released or name in RESERVED_NAMES:
            # Qualified by the nearest directory that says something the name
            # does not: svc/cpp:cpp is svc_cpp, fsl/io:io is fsl_io and
            # guest:lib is guest_lib -- the convention the released names
            # already follow.  A name the Makefile uses for a directory is
            # taken too: a module called lib builds into build/lib/, beside
            # every other module's library.
            parts = [p for p in entry['dir'].split('/') if p != name]
            name = '%s_%s' % (parts[-1] if parts else 'ddk', name)
        suffix = 2
        base = name
        while name in taken:
            name = '%s%d' % (base, suffix)
            suffix += 1
        names[label] = name
        taken.add(name)
        notes.append('new module %s, named %s by rule' % (label, name))

    # Binary libraries: named after the released library with their symbols.
    archives = {}
    for plain, entry in sorted(binary.items()):
        label = variants[plain]
        archive = None
        if released and build_dir and nm:
            defined = object_symbols(the_map, [label], build_dir, nm, nm_cache)
            if defined:
                # Both ways: libcrypto.so contains libc-trusty's symbols
                # too, and is not libc-trusty.a.  Overlap over the union
                # prefers the library that is these objects and no more.
                scored = sorted(
                    ((len(defined & exported) /
                      float(len(defined | exported)), name)
                     for name, exported in released.items() if exported),
                    reverse=True)
                if scored and scored[0][0] >= 0.5:
                    archive = scored[0][1]
                else:
                    # A shared library exports what its objects mark
                    # visible and no more: boringssl's ssl objects define
                    # 1439 symbols and libssl.so exports 577 of them, and
                    # over the union that is 0.40.  Over what the released
                    # library exports it is 0.99, and that is the library.
                    covered = sorted(
                        ((len(defined & exported) / float(len(exported)),
                          len(exported), name)
                         for name, exported in released.items() if exported),
                        reverse=True)
                    if covered and covered[0][0] >= 0.9:
                        archive = covered[0][2]
        if archive is None:
            archive = default_module_name(entry)
            # A name the last release gave another library, or one already
            # given here: taken.  features/fidl:fidl is not zircon's
            # libfidl.a, and shipping it under that name displaced the one
            # every fidl proxy links.  Qualified the way the released names
            # are -- machina/fidl:fidl is fidl_machina.
            if archive in released or archive in taken or \
                    archive in archives.values():
                parts = [p for p in entry['dir'].split('/') if p != archive]
                archive = '%s_%s' % (archive, parts[-1] if parts else 'ddk')
            suffix = 2
            base = archive
            while archive in released or archive in taken or \
                    archive in archives.values():
                archive = '%s%d' % (base, suffix)
                suffix += 1
            notes.append('binary %s has no released library with its '
                         'symbols; named %s by rule' % (plain, archive))
        archives[plain] = archive

    types = {}
    for label, name in names.items():
        record = lock.get('modules', {}).get(name)
        kind = MODULE_TYPES.get(targets[label]['type'])
        if record and record['type'] != kind:
            types[label] = record['type']
            notes.append('%s is a %s in the product and was released as a %s;'
                         ' kept' % (name, kind, record['type']))

    return {
        'format_version': FORMAT_VERSION,
        'roots': sorted(strip_toolchain(r) for r in roots),
        'types': types,
        'modules': dict((l, n) for l, n in names.items()),
        'archives': archives,
        'folded': sorted(folded),
        'notes': notes,
    }


MODULE_FIELDS = ('MODULE_NAME', 'MODULE_OWNER', 'MODULE_TYPE', 'MODULE_DEPS',
                 'MODULE_SRCS', 'MODULE_INCLUDE_DIRS', 'MODULE_LIBS',
                 'MODULE_CFLAGS')


def render_module(module, origin):
    """A module description, as text -- unicode on python 2 as well, since
    it is written through io.open, which accepts nothing else there."""
    lines = [u'# Generated from %s by build_map.py.' % origin]
    for field in MODULE_FIELDS:
        value = module[field]
        if isinstance(value, list):
            value = u' '.join(value)
        lines.append((u'%s := %s' % (field, value)).rstrip())
    return u'\n'.join(lines) + u'\n'


# ---------------------------------------------------------------------------
# comparing with a delivery declared the old way


def read_module_file(path):
    fields = {}
    origin = None
    with io.open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.rstrip('\n')
            match = re.match(r'#\s*Generated from (\S+)', line)
            if match:
                origin = match.group(1)
                continue
            match = re.match(r'^(MODULE_[A-Z_]+)\s*:=\s*(.*)$', line)
            if match:
                fields[match.group(1)] = match.group(2).split()
    for field in ('MODULE_NAME', 'MODULE_OWNER', 'MODULE_TYPE'):
        fields[field] = ' '.join(fields.get(field, []))
    return origin, fields


def read_abi_defines(makefile):
    """The -D flags the delivered Makefile gives every module already."""
    defines = set()
    in_block = False
    with io.open(makefile, encoding='utf-8') as handle:
        for line in handle:
            if line.startswith('ABI_DEFINES'):
                in_block = True
            if in_block:
                defines.update(re.findall(r'(-D\S+)', line))
                if not line.rstrip().endswith('\\'):
                    in_block = False
    return defines


def read_abi_cxx_flags(makefile):
    """The C++ flags the delivered Makefile gives every C++ module."""
    flags = []
    in_block = False
    with io.open(makefile, encoding='utf-8') as handle:
        for line in handle:
            if line.startswith('ABI_CXX_FLAGS'):
                in_block = True
            if in_block:
                flags.extend(re.findall(r'(-f\S+)', line))
                if not line.rstrip().endswith('\\'):
                    in_block = False
    return flags


def provided_archives(libddk_root):
    """The libraries a consumer can link against libddk, by link name.

    Its own lib/, and the sysroot's -- libzircon.so is how every driver
    reaches the kernel, and it is in the sysroot, not in lib/.
    """
    with io.open(os.path.join(libddk_root, 'SDK_CONTENTS.json'),
                 encoding='utf-8') as handle:
        shipped = [f['path'] for f in json.load(handle)['files']]
    return dict((library_name(p), library_name(p)) for p in shipped
                if (p.startswith('lib/') or p.startswith('sysroot/'))
                and p.endswith(('.a', '.so')) and '/' in p)


def declared_archives(project):
    """The label each of the old `ddk_archive` declarations shipped, by name.

    Only for comparing against the delivery those declarations made: they
    exist on this branch and nowhere else, and on a branch without them the
    same correspondence has to be written into the policy.
    """
    aliases = {}
    for label, target in project.get('targets', {}).items():
        if not strip_toolchain(label).endswith('_ddk_archive'):
            continue
        if target.get('type') != 'action':
            continue
        files = [a for a in target.get('args', []) if '.a=' in a]
        deps = target.get('deps', [])
        if len(files) != 1 or len(deps) != 1:
            continue
        name = library_name(files[0].split('=', 1)[0].split(':')[-1])
        aliases[strip_toolchain(deps[0])] = name
    return aliases


def find_label(the_map, directory, target_name, module_type):
    """The map's label for a module the old declarations named.

    The same target exists once per toolchain it is built on.  A driver is the
    loadable module; anything else prefers the default toolchain, as the
    product build does.
    """
    candidates = [l for l, e in the_map['targets'].items()
                  if e['dir'] == directory and e['name'] == target_name]
    if not candidates:
        return None
    wanted = [l for l in candidates
              if MODULE_TYPES.get(the_map['targets'][l]['type']) ==
              module_type]
    pool = wanted or candidates
    default = [l for l in pool if '(' not in l]
    return sorted(default or pool)[0]


def sha256_of(path, cache):
    if path not in cache:
        digest = hashlib.sha256()
        try:
            with open(path, 'rb') as handle:
                for chunk in iter(lambda: handle.read(1 << 16), b''):
                    digest.update(chunk)
            cache[path] = digest.hexdigest()
        except (IOError, OSError):
            cache[path] = None
    return cache[path]


def compare(the_map, oracle_root, libddk_root, root, rules, makefile,
            aliases=None):
    """Every difference between derived modules and the declared ones.

    Two separate questions.  Module by module, does the map imply the same
    description the declarations wrote?  And across all of them, is every
    file an object actually read either shipped in the half or provided by
    libddk -- which is what the declarations' header lists were for.
    """
    abi_defines = read_abi_defines(makefile)
    abi_cxx_flags = read_abi_cxx_flags(makefile)
    declared = []
    for current, dirs, names in os.walk(oracle_root):
        dirs.sort()
        for name in sorted(names):
            if name.endswith('.ddk.mk'):
                path = os.path.join(current, name)
                origin, fields = read_module_file(path)
                directory = os.path.relpath(current, oracle_root).replace(
                    os.sep, '/')
                declared.append((directory, origin, fields))

    labels = {}
    unmatched = []
    for directory, origin, fields in declared:
        target_name = (origin or '')
        if target_name.endswith('_ddk_src'):
            target_name = target_name[:-len('_ddk_src')]
        label = find_label(the_map, directory, target_name,
                           fields['MODULE_TYPE'])
        if label is None:
            unmatched.append((directory, origin))
            continue
        labels[label] = fields['MODULE_NAME']
    keyed = dict((strip_toolchain(l), n) for l, n in labels.items())
    keyed.update(labels)

    provided = provided_archives(libddk_root)
    provided.update(aliases or {})
    owners, ambiguous = assign_folds(the_map, labels, provided)

    modules = []
    for directory, origin, fields in declared:
        target_name = (origin or '')[:-len('_ddk_src')] \
            if (origin or '').endswith('_ddk_src') else (origin or '')
        label = find_label(the_map, directory, target_name,
                           fields['MODULE_TYPE'])
        if label is None:
            continue
        derived = derive_module(the_map, label, keyed, rules, abi_defines,
                                abi_cxx_flags, provided, owners)
        differences = {}
        for field in MODULE_FIELDS:
            want = fields.get(field, [])
            got = derived[field]
            if field == 'MODULE_INCLUDE_DIRS':
                want = [w.rstrip('/') for w in want]
            if isinstance(got, list):
                missing = [v for v in want if v not in got]
                extra = [v for v in got if v not in want]
                if missing or extra:
                    differences[field] = {'declared_only': missing,
                                          'derived_only': extra}
            elif want != got:
                differences[field] = {'declared': want, 'derived': got}
        modules.append({'module': fields['MODULE_NAME'], 'label': label,
                        'folded': derived['folded'],
                        'unsatisfied': derived['unsatisfied'],
                        'through_archive': derived['through_archive'],
                        'differences': differences})

    # What each module finally links.  The Makefile closes over MODULE_DEPS
    # and takes every module's MODULE_LIBS along, so a library named on a
    # dependency instead of on the module itself links the same, and the
    # comparison that matters for whether anything fails to link is over the
    # closure, not the line.
    declared_by_name = dict((f['MODULE_NAME'], f) for _, _, f in declared)
    derived_by_name = {}
    for module in modules:
        label = module['label']
        derived_by_name[module['module']] = derive_module(
            the_map, label, keyed, rules, abi_defines, abi_cxx_flags,
            provided, owners)

    def closure(name, table, stack=()):
        if name not in table or name in stack:
            return set()
        entry = table[name]
        found = set(entry['MODULE_LIBS'])
        for dependency in entry['MODULE_DEPS']:
            found.add(dependency)
            found |= closure(dependency, table, stack + (name,))
        return found

    for module in modules:
        name = module['module']
        want = closure(name, declared_by_name)
        got = closure(name, derived_by_name)
        module['link_missing'] = sorted(want - got)
        module['link_extra'] = sorted(got - want)

    # The file set.
    def manifest(directory):
        with io.open(os.path.join(directory, 'SDK_CONTENTS.json'),
                     encoding='utf-8') as handle:
            return json.load(handle)['files']
    half = set(f['path'] for f in manifest(oracle_root))
    provided = set(f['sha256'] for f in manifest(libddk_root))
    cache = {}
    read_by = {}
    folded_by = dict((m['label'], m['folded']) for m in modules)
    for label in labels:
        for member in [label] + folded_by.get(label, []):
            entry = the_map['targets'][member]
            for path in entry['sources'] + entry['headers']:
                if classify(path) == 'source':
                    read_by.setdefault(path, set()).add(labels[label])
    in_half, from_libddk, missing = [], [], []
    for path in sorted(read_by):
        if path in half:
            in_half.append(path)
        elif sha256_of(os.path.join(root, path), cache) in provided:
            from_libddk.append(path)
        else:
            missing.append({'path': path,
                            'read_by': sorted(read_by[path])})
    shipped_unread = sorted(p for p in half
                            if p not in read_by and not p.endswith('.ddk.mk')
                            and p not in ('BUILD_INFO.json',
                                          'SDK_CONTENTS.json'))
    return {
        'ambiguous_folds': ambiguous,
        'modules_declared': len(declared),
        'modules_matched': len(labels),
        'unmatched': unmatched,
        'modules': modules,
        'files': {
            'read': len(read_by),
            'in_half': len(in_half),
            'from_libddk': len(from_libddk),
            'missing': missing,
            'shipped_unread': shipped_unread,
        },
    }


def summarize(report, stream):
    agree = [m for m in report['modules'] if not m['differences']]
    stream.write('modules: %d declared, %d found in the map, %d derived '
                 'identically\n'
                 % (report['modules_declared'], report['modules_matched'],
                    len(agree)))
    for directory, origin in report['unmatched']:
        stream.write('  not in the map: %s (%s)\n' % (directory, origin))
    for key, names in sorted(report.get('ambiguous_folds', {}).items()):
        stream.write('  source set reached by several modules: %s <- %s\n'
                     % (key, ', '.join(names)))
    by_field = {}
    for module in report['modules']:
        for field in module['differences']:
            by_field.setdefault(field, []).append(module['module'])
    for field in MODULE_FIELDS:
        if field in by_field:
            stream.write('  %-20s differs in %2d: %s\n'
                         % (field, len(by_field[field]),
                            ' '.join(sorted(by_field[field]))))
    unsatisfied = {}
    for m in report['modules']:
        for library in m.get('unsatisfied', []):
            unsatisfied.setdefault(library, []).append(m['module'])
    for library, users in sorted(unsatisfied.items()):
        stream.write('libddk does not provide %s, which the product links '
                     'into: %s\n' % (library, ' '.join(users)))
    for m in report['modules']:
        if m.get('through_archive'):
            stream.write('%s reaches module(s) %s only through an archive '
                         'libddk provides; not made dependencies\n'
                         % (m['module'], ' '.join(m['through_archive'])))
    short = [(m['module'], m['link_missing']) for m in report['modules']
             if m.get('link_missing')]
    stream.write('link closure: %d module(s) would link something less than '
                 'declared\n' % len(short))
    for name, missing in short:
        stream.write('  %-24s missing %s\n' % (name, ' '.join(missing)))
    files = report['files']
    stream.write('files the delivered modules read: %d -- %d in the half, '
                 '%d from libddk, %d from neither\n'
                 % (files['read'], files['in_half'], files['from_libddk'],
                    len(files['missing'])))
    for item in files['missing'][:30]:
        stream.write('  neither: %s (read by %s)\n'
                     % (item['path'], ', '.join(item['read_by'])))
    if files['shipped_unread']:
        stream.write('shipped in the half but read by no object: %d\n'
                     % len(files['shipped_unread']))


# ---------------------------------------------------------------------------


def _write_json(path, document):
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    text = json.dumps(document, indent=1, sort_keys=True) + '\n'
    if not isinstance(text, type(u'')):
        text = text.decode('utf-8')
    with io.open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest='command')

    one = sub.add_parser('map', help='read a product build')
    one.add_argument('--root', required=True, help='the checkout')
    one.add_argument('--gn', required=True, help='the pinned gn')
    one.add_argument('--ninja', required=True, help='the pinned ninja')
    one.add_argument('--gn-out', required=True,
                     help='a build directory `gn gen --ide=json` was run in, '
                          'with the product arguments')
    one.add_argument('--build-dir', required=True,
                     help='the product build directory whose objects were '
                          'built: its dependency log is read')
    one.add_argument('--out', required=True, help='where the map goes')

    pick = sub.add_parser('select', help='choose what ships, from the policy')
    pick.add_argument('--map', required=True)
    pick.add_argument('--policy', required=True, help='policy/delivery.json')
    pick.add_argument('--lock', help='lock/libddk.lock.json: names to keep')
    pick.add_argument('--symbols', help='lock/libddk.symbols')
    pick.add_argument('--build-dir', help='where the objects are, to match '
                                          'binary libraries by symbol')
    pick.add_argument('--nm', help='the pinned llvm-nm')
    pick.add_argument('--out', required=True)

    two = sub.add_parser('modules', help='derive module descriptions')
    two.add_argument('--map', required=True)
    two.add_argument('--rules', required=True, help='policy/partition.json')
    two.add_argument('--makefile', required=True, help='selftest.mk')
    two.add_argument('--module', action='append', default=[],
                     metavar='LABEL=NAME')
    two.add_argument('--selection',
                     help='what `select` chose; replaces --module and '
                          '--provide')
    two.add_argument('--out', required=True, help='directory for .ddk.mk')
    two.add_argument('--libddk',
                     help='the component whose lib/ says which archives '
                          'are provided')
    two.add_argument('--lock', help='lock/libddk.lock.json, in place of '
                                    '--libddk')
    two.add_argument('--declared-archives-from', metavar='PROJECT_JSON')
    two.add_argument('--provide', action='append', default=[],
                     metavar='LABEL=ARCHIVE')

    three = sub.add_parser('compare', help='compare with a declared delivery')
    three.add_argument('--map', required=True)
    three.add_argument('--root', required=True)
    three.add_argument('--declared', required=True,
                       help='the source set the declarations produced')
    three.add_argument('--libddk', required=True)
    three.add_argument('--rules', required=True)
    three.add_argument('--makefile', required=True)
    three.add_argument('--report', help='write the full report as JSON')
    three.add_argument('--declared-archives-from',
                       metavar='PROJECT_JSON',
                       help='take label-to-archive names from the ddk_archive '
                            'declarations in this project.json')
    three.add_argument('--provide', action='append', default=[],
                       metavar='LABEL=ARCHIVE',
                       help='a target libddk ships under a name of its own')

    args = parser.parse_args()
    try:
        if args.command == 'map':
            gn_out = os.path.abspath(args.gn_out)
            with io.open(os.path.join(gn_out, 'project.json'),
                         encoding='utf-8') as handle:
                project = json.load(handle)
            default = project['build_settings']['default_toolchain']
            toolchains = sorted(set(
                toolchain_of(label, default)
                for label, target in project['targets'].items()
                if target.get('type') in LINK_TYPES))
            described = describe(os.path.abspath(args.gn), gn_out,
                                 toolchains, default)
            build_dir = os.path.abspath(args.build_dir)
            deps = parse_ninja_deps(
                _run([os.path.abspath(args.ninja), '-C', build_dir, '-t',
                      'deps']),
                build_dir, os.path.abspath(args.root))
            the_map = build_map(project, described, deps)
            _write_json(args.out, the_map)
            print('build map: %d target(s) on %d toolchain(s), %d object(s) '
                  'with no dependency record -> %s'
                  % (len(the_map['targets']), len(toolchains),
                     the_map['objects_without_depfile'], args.out))
            return 0

        with io.open(args.map, encoding='utf-8') as handle:
            the_map = json.load(handle)
        rules = partition.read_rules(args.rules) \
            if getattr(args, 'rules', None) else None

        if args.command == 'select':
            policy = read_policy(args.policy)
            lock, symbols = None, None
            if args.lock:
                with io.open(args.lock, encoding='utf-8') as handle:
                    lock = json.load(handle)
                symbols = {}
                if args.symbols and os.path.isfile(args.symbols):
                    with io.open(args.symbols, encoding='utf-8') as handle:
                        for line in handle:
                            library, _, symbol = line.rstrip('\n').partition(
                                '\t')
                            symbols.setdefault(library, []).append(symbol)
            chosen = select(the_map, policy, lock, symbols,
                            os.path.abspath(args.build_dir)
                            if args.build_dir else None, args.nm)
            _write_json(args.out, chosen)
            print('selected %d root(s), %d module(s), %d binary librar(y/ies),'
                  ' %d source set(s) folded -> %s'
                  % (len(chosen['roots']), len(chosen['modules']),
                     len(chosen['archives']), len(chosen['folded']), args.out))
            for note in chosen['notes']:
                print('  %s' % note)
            return 0

        if args.command == 'modules':
            modules = {}
            aliases = {}
            kept_types = {}
            if args.selection:
                with io.open(args.selection, encoding='utf-8') as handle:
                    chosen = json.load(handle)
                modules.update(chosen['modules'])
                aliases.update(chosen['archives'])
                FOLD.update(chosen.get('folded', []))
                kept_types.update(chosen.get('types', {}))
            for spelling in args.module:
                label, _, name = spelling.rpartition('=')
                modules[label] = name
            if not modules:
                parser.error('modules needs --selection or --module')
            abi = read_abi_defines(args.makefile)
            abi_cxx = read_abi_cxx_flags(args.makefile)
            if args.lock:
                with io.open(args.lock, encoding='utf-8') as handle:
                    lock_files = lock_surface(json.load(handle))
                provided = dict((library_name(p), library_name(p))
                                for p in lock_files
                                if p.startswith(('lib/', 'sysroot/')) and
                                p.endswith(('.a', '.so')))
            elif args.libddk:
                provided = provided_archives(args.libddk)
            else:
                parser.error('modules needs --lock or --libddk')
            # Labels map to the names libddk ships them under.  What counts
            # as provided is what this delivery will have: the last release's
            # libraries, from the lock, and every archive the selection
            # ships -- the derivation plans each of those into lib/, so a
            # module may link it.  Held to the lock alone, a library new to
            # this release -- mt8668's fidl_features bindings, in a tree
            # seeded with another product's lock -- was left off every link
            # line and the delivery failed to link.
            provided.update(aliases)
            provided.update(dict((name, name) for name in aliases.values()))
            if args.declared_archives_from:
                with io.open(args.declared_archives_from,
                             encoding='utf-8') as handle:
                    provided.update(declared_archives(json.load(handle)))
            provided.update(dict(p.rsplit('=', 1) for p in args.provide))
            keyed = dict((strip_toolchain(l), n) for l, n in modules.items())
            keyed.update(modules)
            owners, ambiguous = assign_folds(the_map, modules, provided)
            for key, names in sorted(ambiguous.items()):
                sys.stderr.write('warning: %s is reached by %s; given to %s\n'
                                 % (key, ', '.join(names), owners[key]))
            left_out = {}
            for label, name in sorted(modules.items()):
                module = derive_module(the_map, label, keyed, rules, abi,
                                       abi_cxx, provided, owners)
                if label in kept_types:
                    module['MODULE_TYPE'] = kept_types[label]
                for library in module['unsatisfied']:
                    left_out.setdefault(library, []).append(name)
                directory = os.path.join(args.out,
                                         the_map['targets'][label]['dir'])
                if not os.path.isdir(directory):
                    os.makedirs(directory)
                with io.open(os.path.join(directory, name + '.ddk.mk'), 'w',
                             encoding='utf-8') as handle:
                    handle.write(render_module(module, label))
            print('derived %d module description(s) -> %s'
                  % (len(modules), args.out))
            # What the product links and libddk does not ship: trimmed as
            # unused, or missing.  The link says which.
            for library, users in sorted(left_out.items()):
                print('  not in libddk, left out: %-14s wanted by %d module(s)'
                      % (library, len(users)))
            return 0

        if args.command == 'compare':
            aliases = {}
            if args.declared_archives_from:
                with io.open(args.declared_archives_from,
                             encoding='utf-8') as handle:
                    aliases.update(declared_archives(json.load(handle)))
            aliases.update(dict(p.rsplit('=', 1) for p in args.provide))
            report = compare(the_map, os.path.abspath(args.declared),
                             os.path.abspath(args.libddk),
                             os.path.abspath(args.root), rules,
                             args.makefile, aliases)
            if args.report:
                _write_json(args.report, report)
            summarize(report, sys.stdout)
            return 0
    except (MapError, partition.PartitionError) as error:
        sys.stderr.write('error: %s\n' % error)
        return 1
    parser.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())

<%include file="header.mako" />

import("//build/sdk/sdk_atom.gni")

config("${data.name}_config") {
  include_dirs = [
    % for include in sorted(data.include_dirs):
    "${include}",
    % endfor
  ]

  visibility = [
    ":*",
  ]
}

source_set("${data.name}") {
  sources = [
    % for _, source in sorted(data.sources.iteritems()):
    "${source}",
    % endfor
    % for _, source in sorted(data.includes.iteritems()):
    "${source}",
    % endfor
  ]

  public_deps = [
    % for dep in sorted(data.deps):
    "../${dep}",
    % endfor
  ]

  libs = [
    % for lib in sorted(data.libs):
    "${lib}",
    % endfor
  ]

  public_configs = [
    ":${data.name}_config",
  ]

  defines = [
    "_ALL_SOURCE=1",
  ]
}

sdk_atom("${data.name}_sdk") {
  domain = "c-pp"
  name = "${data.name}"

  tags = [
    "type:sources",
  ]

  files = [
    % for dest, source in sorted(data.includes.iteritems()):
    {
      source = "${source}"
      dest = "include/${dest}"
    },
    % endfor
    % for dest, source in sorted(data.sources.iteritems()):
    {
      source = "${source}"
      dest = "${dest}"
    },
    % endfor
  ]

  package_deps = [
    % for dep in sorted(data.deps):
    "../${dep}:${dep}_sdk",
    % endfor
  ]
}

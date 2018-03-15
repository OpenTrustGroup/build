<%include file="header.mako" />

import("//build/gn/config.gni")
import("//build/sdk/sdk_atom.gni")

_lib = "$target_out_dir/${data.lib_name}"

copy("${data.name}_copy_lib") {
  sources = [
    "${data.prebuilt}",
  ]

  outputs = [
    _lib,
  ]
}

config("${data.name}_config") {
  include_dirs = [
    % for include in sorted(data.include_dirs):
    "${include}",
    % endfor
  ]

  libs = [
    _lib,
  ]

  visibility = [
    ":*",
  ]
}

group("${data.name}") {

  public_deps = [
    ":${data.name}_copy_lib",
    % for dep in sorted(data.deps):
    "../${dep}",
    % endfor
  ]

  public_configs = [
    ":${data.name}_config",
  ]
}

sdk_atom("${data.name}_sdk") {
  domain = "c-pp"
  name = "${data.name}"

  tags = [
    "type:compiled_static",
    "arch:target",
  ]

  files = [
    % for dest, source in sorted(data.includes.iteritems()):
    {
      source = "${source}"
      dest = "include/${dest}"
    },
    % endfor
    {
      source = _lib
      dest = "lib/${data.lib_name}"
    },
  ]

  package_deps = [
    % for dep in sorted(data.deps):
    "../${dep}:${dep}_sdk",
    % endfor
  ]

  non_sdk_deps = [
    ":${data.name}",
  ]
}

#!/bin/bash
# Copyright 2020 The GoldenRiver Authors. All rights reserved.
# Copyright 2018 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

set -eo pipefail; [[ "${TRACE}" ]] && set -x

FUCHSIA_DIR="${FUCHSIA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

workspaces=(host ree tee)

for ws in ${workspaces[@]}; do
  ws_src="${FUCHSIA_DIR}/build/bazel/${ws}"
  ws_dst="${FUCHSIA_DIR}/bazel/${ws}"
  mkdir -p "${ws_dst}"
  # Remove all but bazal outputs simply because they're
  # symlinked to FUCHSIA_OUT_DIR and reference by others.
  find "${ws_dst}" -type l ! -name "bazel-*" -exec rm -rf {} \;
  for f in $(find ${ws_src} -type f); do
    ln -snf "${f}" "${ws_dst}"
  done
done

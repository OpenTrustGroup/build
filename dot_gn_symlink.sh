#!/bin/bash
# Copyright 2018 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

FUCHSIA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec ln -snf build/gn/.gn "${FUCHSIA_DIR}/.gn"

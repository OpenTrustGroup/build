#!/bin/bash
# Copyright 2026 Open Trust Group. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# compile-dts.sh OUTPUT.dtb INPUT.dts
# Compile a devicetree source with the `dtc` on PATH (open-ddk's prepare.sh
# puts the one build-env ships first), as garnet's gen_dtb.sh does.
set -eo pipefail
exec dtc -I dts -O dtb -o "$1" "$2"

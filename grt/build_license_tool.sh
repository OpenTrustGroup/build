#!/bin/bash
# Copyright 2020 The GoldenRiver Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

source_dir=$1
shift

output_dir=$1
shift

echo -e "\nBuild license tool start\n"
pushd ${source_dir}
ant -Dout_path=${output_dir}
popd
echo -e "\nBuild license tool finish\n"

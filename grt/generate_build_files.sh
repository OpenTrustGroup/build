#!/bin/bash

set -x

path=$1
shift

package="$(basename ${path})"
out=BUILD.${package}.generated.gni

buildfile="$(find "${path}" -maxdepth 1 -type f -name "*.xml" | awk '{print "  \"//"$1"\","}')"
otherfiles="$(find "${path}/src/" -type f | awk '{print "  \"//"$1"\","}')"

cat > "${out}" << EOF
# This file is created by generate_build_files.sh. Do not edit manually.

${package}_sources = [
${buildfile}
${otherfiles}
]
EOF


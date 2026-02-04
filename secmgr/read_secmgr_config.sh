#!/bin/bash

# cd the directory given by argument of this script,
# and list all files except BUILD.gn and list_files.sh.
# - DON'T COPY THIS FILE INTO SDK.

cd $*

if [ -e "./secmgr_build_config" ]; then
  cat ./secmgr_build_config | while read line
  do
  echo ${line#*=}
  done

else
  echo ""
fi

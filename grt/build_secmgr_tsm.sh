#!/bin/bash
#Copyright 2018 GoldenRiver Technology Co., Ltd. All rights reserved.

echo -e "\nBuild secmgr tsm start\n"

set -eo pipefail; [[ "${TRACE}" ]] && set -x

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export FUCHSIA_DIR="${SCRIPT_DIR}/../../"
export FUCHSIA_BUILD_DIR="${SCRIPT_DIR}/../../out/arm64"
export FUCHSIA_OUT_DIR="${SCRIPT_DIR}/../../out/"

TSM_DIR=${FUCHSIA_DIR}/third_party/otrp/nbl_tsm_server
TSM_TEST_DIR=$TSM_DIR/WebContent/test_suite

# copy $from to $to.
# $1 : from
# $2 : to
function copy_and_mkdir()
{
    DIR=`dirname $2`
    if [ ! -d $DIR ]; then
        mkdir -p $DIR
    fi
    cp $1 $2
}

# parse the input line, and copy test exe to sdk.
# $1 : each line in manifest file, format is "to=from".
function copy_test()
{
    # split by "="
    local words=(${1//=/ })
    local to=`echo "system/${words[0]}"`
    local from="${FUCHSIA_BUILD_DIR}/${words[1]}"

    copy_and_mkdir $from ${2}/$to
}

function add_bazel_file()
{
    # split by "="
    local words=(${1//=/ })
    local file_path="system/${words[0]}"
    local dir="$(dirname ${file_path})"
    local file_name="$(basename ${file_path})"
    cat >> ${2} << EOF
filegroup(
    name = "__${file_name}",
    srcs = ["${file_path}"],
)

pkg_bin(
    name = "${file_name}",
    exec = ":__${file_name}",
)

EOF
}

rm -rf ${TSM_TEST_DIR}/system/test/secmgr_test/*

cat ${FUCHSIA_BUILD_DIR}/system.manifest | while read line; do
    if [[ "$line" =~ ^test/secmgr_test/.*= ]]; then # copy all files in /system/test
        copy_test $line ${TSM_TEST_DIR}
    fi
done

cd ${TSM_DIR}
ant
cd ${FUCHSIA_DIR}

#build sample
#copy data
rm -rf $TSM_DIR/nbl_tsm_server_sample/src/*
mkdir -p $TSM_DIR/nbl_tsm_server_sample/src/com/goldenrivertek/tsm/sample
cp -r $TSM_DIR/src/com/goldenrivertek/tsm/sample/* $TSM_DIR/nbl_tsm_server_sample/src/com/goldenrivertek/tsm/sample/

rm -rf $TSM_DIR/nbl_tsm_server_sample/WebContent/elf_tool
mkdir -p $TSM_DIR/nbl_tsm_server_sample/WebContent/elf_tool
cp -r $TSM_DIR/WebContent/elf_tool/* $TSM_DIR/nbl_tsm_server_sample/WebContent/elf_tool/

rm -rf $TSM_DIR/nbl_tsm_server_sample/WebContent/key_store_test
mkdir -p $TSM_DIR/nbl_tsm_server_sample/WebContent/key_store_test
cp -r $TSM_DIR/WebContent/key_store_test/* $TSM_DIR/nbl_tsm_server_sample/WebContent/key_store_test/

rm -rf $TSM_DIR/nbl_tsm_server_sample/WebContent/test_suite
mkdir -p $TSM_DIR/nbl_tsm_server_sample/WebContent/test_suite
cp -r $TSM_DIR/WebContent/test_suite/* $TSM_DIR/nbl_tsm_server_sample/WebContent/test_suite/

rm -rf $TSM_DIR/nbl_tsm_server_sample/WebContent/WEB-INF/lib
mkdir -p $TSM_DIR/nbl_tsm_server_sample/WebContent/WEB-INF/lib
cp -r $TSM_DIR/WebContent/WEB-INF/lib/* $TSM_DIR/nbl_tsm_server_sample/WebContent/WEB-INF/lib/
cp -r ${FUCHSIA_OUT_DIR}/secmgr/build/nebula_tsm.jar $TSM_DIR/nbl_tsm_server_sample/WebContent/WEB-INF/lib/nebula_tsm.jar

rm -rf $TSM_DIR/nbl_tsm_server_sample/tomcat_lib
mkdir -p $TSM_DIR/nbl_tsm_server_sample/tomcat_lib/lib
cp -r $TSM_DIR/tomcat_lib/lib/* $TSM_DIR/nbl_tsm_server_sample/tomcat_lib/lib

cd ${TSM_DIR}/nbl_tsm_server_sample
ant
cd ${FUCHSIA_DIR}

echo -e "\nBuild secmgr tsm finish\n"

echo -e "\nGenerate secmgr provision test data start\n"
#Generate provision test HA data
${FUCHSIA_DIR}/garnet/public/nebula_sdk/run/otrp/provision_tool/provision_secmgr_test_ha.sh
echo -e "\nGenerate secmgr provision test data finish\n"


SDK_DIR=${FUCHSIA_BUILD_DIR}/sdks/nebula
#################################################
# copy otrp's scripts
cp ${FUCHSIA_OUT_DIR}/secmgr/build/secmgr_debug_tool.jar $SDK_DIR/run/otrp/debug_tool/
cp ${FUCHSIA_OUT_DIR}/secmgr/build/secmgr_provision_tool.jar $SDK_DIR/run/otrp/provision_tool/
cp ${FUCHSIA_OUT_DIR}/secmgr/build/secmgr_sign_tool.jar $SDK_DIR/run/otrp/sign_tool/
cp ${FUCHSIA_OUT_DIR}/secmgr/build/nebula_elf_tool.jar $SDK_DIR/run/otrp/elf_tool/

SDK_TEST_DIR=$SDK_DIR/test_suite
BAZEL_BUILD_FILE=$SDK_TEST_DIR/BUILD.bazel
rm -rf $SDK_TEST_DIR
mkdir -p $SDK_TEST_DIR
cat > $SDK_TEST_DIR/sd.privilege << EOF
{
    "NBL_APP_TUI":"1",
    "NBL_APP_NEW_PROCESS":"1"
}
EOF

cat > $BAZEL_BUILD_FILE << EOF
load("@io_bazel_rules_nbl//nbl:pkgs.bzl", "pkg_bin")

EOF

# k ut all: modules in kernel, cannot test.
# process-test : cannot create process.
# zircon-benchmarks : cannot create process.
cat ${FUCHSIA_BUILD_DIR}/system.manifest | while read line; do
    if [[ "$line" =~ ^test/.*= ]]; then # copy all files in /system/test
        copy_test $line ${SDK_TEST_DIR}
        add_bazel_file $line ${BAZEL_BUILD_FILE}
    elif [[ $line =~ ^bin/zircon_benchmarks= ||
         $line =~ ^bin/ipctest-main= ||
         $line =~ ^bin/ipctest-srv= ||
         $line =~ ^bin/memreftest-main= ||
         $line =~ ^bin/memreftest-proxy= ||
         $line =~ ^bin/memreftest-leaf= ||
         $line =~ ^pkgs/nbl_attest_srv=
         ]]; then # copy /system/bin/zircon_benchmarks
        copy_test $line ${SDK_TEST_DIR}
        add_bazel_file $line ${BAZEL_BUILD_FILE}
    fi
done

${SDK_DIR}/run/otrp/provision_tool/provision_create_ha.sh -e ${FUCHSIA_OUT_DIR}/arm64/secmgr_test_ree_ha1 \
    -o ${FUCHSIA_OUT_DIR}/arm64/provision/secmgr_test_ree_ha1

${SDK_DIR}/run/otrp/provision_tool/provision_create_ha.sh -e ${FUCHSIA_OUT_DIR}/arm64/secmgr_test_ree_ha2 \
    -o ${FUCHSIA_OUT_DIR}/arm64/provision/secmgr_test_ree_ha2

${SDK_DIR}/run/otrp//provision_tool/provision_create_ha_by_config.sh -c ${SDK_DIR}/run/otrp//key_store_test \
    -e ${FUCHSIA_OUT_DIR}/arm64/package/nbl_attest_srv/nbl_attest_srv.far \
    -o ${FUCHSIA_OUT_DIR}/arm64/provision/nbl_attest_srv

rm -rf ${FUCHSIA_OUT_DIR}/arm64/provision/nbl_attest_srv/delete_sd
cp ${FUCHSIA_OUT_DIR}/arm64/provision/nbl_attest_srv/*/*.json ${FUCHSIA_OUT_DIR}/arm64/provision/nbl_attest_srv/

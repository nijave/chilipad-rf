# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(chilipad_sniffer)

target_sources(app PRIVATE src/main.c)

# Build id for the '?' probe.  The build host gets an rsync copy without .git,
# so this is the UTC configure time (honours SOURCE_DATE_EPOCH).
string(TIMESTAMP CHILI_BUILD_ID "%Y%m%d-%H%MZ" UTC)
target_compile_definitions(app PRIVATE CHILI_BUILD_ID="${CHILI_BUILD_ID}")

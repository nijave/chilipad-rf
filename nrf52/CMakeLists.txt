# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(chilipad_sniffer)

target_sources(app PRIVATE src/main.c)

# Build id for the '?' probe.  The build host gets an rsync copy without .git,
# so this is the UTC configure time (honours SOURCE_DATE_EPOCH).
string(TIMESTAMP CHILI_BUILD_ID "%Y%m%d-%H%MZ" UTC)
target_compile_definitions(app PRIVATE CHILI_BUILD_ID="${CHILI_BUILD_ID}")

# Device ID bytes of the remote to imitate (src/chili_encoder.h); the default
# is 40 40.  Example: west build ... -- -DCHILI_ID0=0x41 -DCHILI_ID1=0x42
foreach(id CHILI_ID0 CHILI_ID1)
  if(DEFINED ${id})
    target_compile_definitions(app PRIVATE ${id}=${${id}})
  endif()
endforeach()

# Generated by devtools/yamaker.

LIBRARY()

LICENSE(Apache-2.0 WITH LLVM-exception)

LICENSE_TEXTS(.yandex_meta/licenses.list.txt)

SUBSCRIBER(g:cpp-contrib)

VERSION(20.1.8)

PEERDIR(
    contrib/libs/afl/llvm_mode
)

NO_COMPILER_WARNINGS()

NO_UTIL()

NO_SANITIZE()

SRCS(
    afl_driver.cpp
)

END()

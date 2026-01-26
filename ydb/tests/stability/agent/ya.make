PY3_PROGRAM()

PY_SRCS(
    __main__.py
)

DATA(
    arcadia/ydb/tests/stability/agent/static
)

DEPENDS(
    ydb/apps/ydb
    ydb/tools/cfg/bin
    ydb/tests/tools/nemesis/driver
)

BUNDLE(
    ydb/apps/ydb NAME ydb_cli
)

RESOURCE(
    ydb_cli ydb_cli
)


PEERDIR(
    ydb/tests/library
    ydb/tests/library/wardens
    contrib/python/aiocache
    contrib/python/fastapi
    contrib/python/pydantic
)

END()


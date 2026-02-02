PY3_PROGRAM()

PY_SRCS(
    __main__.py
    install.py
    config.py
    models.py
    defaults.py
    agent_app.py
    orchestrator_app.py
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
    # contrib/python/pydantic
    contrib/python/pydantic-settings
    contrib/python/python-dotenv
)

END()


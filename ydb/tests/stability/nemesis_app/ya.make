PY3_PROGRAM()

PY_SRCS(
    __init__.py
    internal/install.py
    internal/config.py
    internal/models.py
    internal/defaults.py
    apps/agent_app.py
    routers/agent_router.py
    apps/orchestrator_app.py
    routers/orchestrator_router.py
    app.py
)

DATA(
    arcadia/ydb/tests/stability/nemesis_app/static
)

DEPENDS(
    ydb/tools/cfg/bin
    ydb/tests/tools/nemesis/driver
)

PEERDIR(
    ydb/tests/tools/nemesis/library
    ydb/tests/library
    ydb/tests/library/stability
    ydb/tests/library/wardens
    contrib/python/aiocache
    contrib/python/fastapi
    # contrib/python/pydantic
    contrib/python/pydantic-settings
    contrib/python/python-dotenv
)

END()


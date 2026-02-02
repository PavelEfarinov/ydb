import uvicorn
from functools import lru_cache
from ydb.tests.stability.agent import config


@lru_cache
def get_settings():
    settings = config.Settings()
    print(settings)
    return settings


if __name__ == "__main__":
    settings = get_settings()
    app_path = "ydb.tests.stability.agent.orchestrator_app:app"
    if settings.nemesis_type == 'agent':
        app_path = "ydb.tests.stability.agent.agent_app:app"

    # workers=1 is important because we store state in memory
    uvicorn.run(
        app_path, host=settings.app_host, port=31434, workers=1
    )

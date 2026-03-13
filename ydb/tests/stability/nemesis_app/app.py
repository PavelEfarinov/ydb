from contextlib import asynccontextmanager
from functools import lru_cache
import json
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from ydb.tests.stability.nemesis_app.internal import config
from ydb.tests.stability.nemesis_app.internal.install import get_hosts_from_yaml
from ydb.tests.library.stability.healthcheck.healthcheck_reporter import HealthCheckReporter
from ydb.tests.stability.nemesis_app.internal.agent_warden_checker import AgentWardenChecker
from ydb.tests.stability.nemesis_app.internal.orchestrator_warden_checker import OrchestratorWardenChecker


@lru_cache
def get_settings():
    settings = config.Settings()
    print(settings)
    return settings


# Global state for orchestrator mode
hosts = []
healthcheck_reporter = None
nemesis_config = {}


def load_nemesis_config():
    global nemesis_config
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nemesis_config.json')
    try:
        with open(config_path, 'r') as f:
            nemesis_config = json.load(f)
    except FileNotFoundError:
        print(f"Config file not found at {config_path}, using defaults")
        nemesis_config = {}
    except Exception as e:
        print(f"Failed to load config: {e}")
        nemesis_config = {}
    return nemesis_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    global hosts, healthcheck_reporter
    settings = get_settings()

    # Initialize agent WardenChecker (always, for both agent and orchestrator modes)
    from ydb.tests.stability.nemesis_app.routers import agent_router
    agent_router.warden_checker = AgentWardenChecker()

    # Orchestrator-specific initialization
    if settings.nemesis_type != 'agent':
        # Load hosts from config (no installation here - that's done via 'install' command)
        hosts = get_hosts_from_yaml(settings.yaml_config_location)
        print(f"Loaded hosts: {hosts}")

        # Load config
        load_nemesis_config()

        # Start healthcheck reporter
        healthcheck_reporter = HealthCheckReporter(hosts, store_results=True)
        healthcheck_reporter.start_healthchecks()

        # Share state with orchestrator router
        from ydb.tests.stability.nemesis_app.routers import orchestrator_router
        orchestrator_router.hosts = hosts
        orchestrator_router.orchestrator_warden_checker = OrchestratorWardenChecker(hosts=hosts, mon_port=orchestrator_router.mon_port)

    yield

    # Orchestrator-specific cleanup
    if settings.nemesis_type != 'agent':
        if healthcheck_reporter:
            healthcheck_reporter.stop_healthchecks()

        # Cancel all scheduled tasks
        from ydb.tests.stability.nemesis_app.routers import orchestrator_router
        for task_info in orchestrator_router.scheduled_tasks.values():
            if 'task' in task_info:
                task_info['task'].cancel()


def create_app():
    settings = get_settings()
    app = FastAPI(lifespan=lifespan)

    # Common health endpoint
    @app.get("/health")
    async def get_health():
        return {"status": "ok"}

    # Always include agent router (available in both modes)
    from ydb.tests.stability.nemesis_app.routers.agent_router import router as agent_router
    app.include_router(agent_router)

    # Include routers based on configuration
    if settings.nemesis_type == 'agent':
        # Agent mode: only agent endpoints
        print("Running in AGENT mode")
    else:
        # Orchestrator mode: include orchestrator router and static files
        from ydb.tests.stability.nemesis_app.routers.orchestrator_router import router as orchestrator_router
        app.include_router(orchestrator_router)
        app.mount("/static", StaticFiles(directory=settings.static_location), name="static")
        print("Running in ORCHESTRATOR mode (with agent endpoints)")

    return app


app = create_app()

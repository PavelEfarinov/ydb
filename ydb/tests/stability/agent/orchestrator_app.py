from contextlib import asynccontextmanager
from functools import lru_cache
from typing import List, Dict, Any

import requests
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from ydb.tests.stability.agent import config
from ydb.tests.stability.agent.defaults import PROCESS_TYPES
from ydb.tests.stability.agent.install import get_hosts_from_yaml, install_on_hosts, stop_agent_services
from ydb.tests.stability.agent.models import CreateProcessRequest, ProcessInfo


@lru_cache
def get_settings():
    settings = config.Settings()
    print(settings)
    return settings


hosts = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global hosts
    # Unpack static, deploy binary to hosts if launch type is not agent
    if get_settings().nemesis_type != 'agent':
        hosts = get_hosts_from_yaml(get_settings().yaml_config_location)
        print(hosts)
        install_on_hosts(hosts)
    yield
    # Stop services on hosts
    stop_agent_services(hosts)


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=get_settings().static_location), name="static")


@app.get("/health")
async def get_health():
    return {"status": "ok"}


@app.get("/api/{host}/processes", response_model=List[ProcessInfo])
async def get_all_host_processes(host: str):
    return requests.get(f"http://{host}:31434/api/processes").json()


@app.get("/api/process_types", response_model=List[str])
async def get_process_types():
    return list(PROCESS_TYPES.keys())


@app.get("/api/hosts/health", response_model=Dict[str, Any])
async def get_hosts_health():
    aggregated_health = {}
    for host in hosts:
        try:
            resp = requests.get(f"http://{host}:31434/health")
            aggregated_health[host] = resp.json()
        except Exception as e:
            aggregated_health[host] = {"status": "error", "message": str(e)}
    return aggregated_health


@app.post("/api/hosts/processes", response_model=Dict[str, Any])
async def create_process_on_host(req: CreateProcessRequest):
    if req.type not in PROCESS_TYPES:
        return {"status": "error", "message": "Invalid process type"}
    for host in hosts:
        try:
            requests.post(f"http://{host}:31434/api/processes",  json={'type': req.type})
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "ok"}
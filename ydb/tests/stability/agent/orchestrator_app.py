from contextlib import asynccontextmanager
from functools import lru_cache
from typing import List, Dict, Any

import requests
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from ydb.tests.stability.agent import config
from ydb.tests.stability.agent.defaults import PROCESS_TYPES
from ydb.tests.stability.agent.install import get_hosts_from_yaml, install_on_hosts, stop_agent_services
from ydb.tests.stability.agent.models import ProcessInfo, SetScheduleRequest, CreateHostProcessRequest
from ydb.tests.library.stability.healthcheck.healthcheck_reporter import HealthCheckReporter
import asyncio


@lru_cache
def get_settings():
    settings = config.Settings()
    print(settings)
    return settings


hosts = []
scheduled_tasks = {}
healthcheck_reporter = None


async def run_process_on_host(host, process_type):
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: requests.post(f"http://{host}:31434/api/processes", json={'type': process_type}, timeout=5))
    except Exception as e:
        print(f"Failed to start process {process_type} on {host}: {e}")


async def schedule_process(process_type: str):
    while True:
        if process_type not in scheduled_tasks or not scheduled_tasks[process_type]['enabled']:
            break

        interval = PROCESS_TYPES[process_type].get('schedule', 60)

        # Execute process on all hosts simultaneously
        tasks = [run_process_on_host(host, process_type) for host in hosts]
        if tasks:
            await asyncio.gather(*tasks)

        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global hosts, healthcheck_reporter
    # Unpack static, deploy binary to hosts if launch type is not agent
    if get_settings().nemesis_type != 'agent':
        hosts = get_hosts_from_yaml(get_settings().yaml_config_location)
        print(hosts)
        install_on_hosts(hosts)

    # Start healthcheck reporter
    healthcheck_reporter = HealthCheckReporter(hosts, store_results=True)
    healthcheck_reporter.start_healthchecks()

    yield
    
    if healthcheck_reporter:
        healthcheck_reporter.stop_healthchecks()

    # Stop services on hosts
    stop_agent_services(hosts)
    # Cancel all scheduled tasks
    for task_info in scheduled_tasks.values():
        if 'task' in task_info:
            task_info['task'].cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=get_settings().static_location), name="static")


@app.get("/health")
async def get_health():
    return {"status": "ok"}


@app.get("/api/hosts/{host}/processes", response_model=List[ProcessInfo])
async def get_all_host_processes(host: str):
    return requests.get(f"http://{host}:31434/api/processes").json()


async def fetch_host_processes(host):
    try:
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, lambda: requests.get(f"http://{host}:31434/api/processes", timeout=5))
        return host, resp.json()
    except Exception as e:
        print(f"Failed to fetch processes from {host}: {e}")
        return host, []


@app.get("/api/hosts/processes", response_model=Dict[str, List[ProcessInfo]])
async def get_all_processes():
    tasks = [fetch_host_processes(host) for host in hosts]
    results = await asyncio.gather(*tasks)
    return {host: procs for host, procs in results}


@app.post("/api/hosts/process", response_model=Dict[str, Any])
async def create_host_process(req: CreateHostProcessRequest):
    if req.type not in PROCESS_TYPES:
        return {"status": "error", "message": "Invalid process type"}
    if req.host not in hosts:
        return {"status": "error", "message": "Invalid host"}

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: requests.post(f"http://{req.host}:31434/api/processes", json={'type': req.type}, timeout=5))
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


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


@app.post("/api/schedule", response_model=Dict[str, Any])
async def set_schedule(req: SetScheduleRequest):
    if req.type not in PROCESS_TYPES:
        return {"status": "error", "message": "Invalid process type"}
    
    if req.enabled:
        if req.type in scheduled_tasks and scheduled_tasks[req.type]['enabled']:
            return {"status": "ok", "message": "Already enabled"}

        scheduled_tasks[req.type] = {'enabled': True}
        task = asyncio.create_task(schedule_process(req.type))
        scheduled_tasks[req.type]['task'] = task
    else:
        if req.type in scheduled_tasks:
            scheduled_tasks[req.type]['enabled'] = False
            if 'task' in scheduled_tasks[req.type]:
                scheduled_tasks[req.type]['task'].cancel()
            del scheduled_tasks[req.type]
            
    return {"status": "ok"}


@app.get("/api/schedule", response_model=Dict[str, bool])
async def get_schedule():
    return {pt: (pt in scheduled_tasks and scheduled_tasks[pt]['enabled']) for pt in PROCESS_TYPES}


@app.get("/api/healthcheck", response_model=Dict[str, Any])
async def get_healthcheck():
    if healthcheck_reporter:
        return healthcheck_reporter.last_results
    return {}
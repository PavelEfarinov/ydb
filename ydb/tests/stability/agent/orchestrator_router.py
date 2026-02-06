from typing import List, Dict, Any
import socket

import requests
from fastapi import APIRouter
from ydb.tests.stability.agent.defaults import PROCESS_TYPES
from ydb.tests.stability.agent.models import ProcessInfo, SetScheduleRequest, CreateHostProcessRequest
import asyncio


router = APIRouter()

# Module-level state
hosts = []
scheduled_tasks = {}
scheduled_executions_history = []  # List of {type, action, host, timestamp}


def is_local_host(host: str) -> bool:
    """Check if the host is the local machine"""
    try:
        # Get configured app_host from settings
        from ydb.tests.stability.agent.config import Settings
        settings = Settings()
        app_host = settings.app_host

        # Check if it's localhost or the configured app_host
        if host in ('localhost', '127.0.0.1', '::1', app_host):
            return True

        # Check if it resolves to the same IP as app_host
        try:
            host_ip = socket.gethostbyname(host)
            app_host_ip = socket.gethostbyname(app_host)
            return host_ip == app_host_ip
        except Exception:
            return False
    except Exception:
        return False


async def run_process_on_host(host, process_type, action='run', track_history=False):
    """Run process on host, using direct call if it's the local host to avoid deadlock"""
    try:
        # Check if this is a call to ourselves
        if is_local_host(host):
            # Direct call to avoid HTTP deadlock with single worker
            from ydb.tests.stability.agent.agent_router import create_process
            from ydb.tests.stability.agent.models import CreateProcessRequest

            req = CreateProcessRequest(type=process_type, action=action)
            result = await create_process(req)
            print(f"Started process {process_type} locally with action {action}: {result}")
        else:
            # Remote call via HTTP
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: requests.post(f"http://{host}:31434/api/processes", json={'type': process_type, 'action': action}, timeout=5))
        
        # Track in history if requested (for scheduled executions)
        if track_history:
            from datetime import datetime
            scheduled_executions_history.append({
                "type": process_type,
                "action": action,
                "host": host,
                "timestamp": datetime.utcnow().isoformat() + "Z"
            })
            # Keep only last 50 entries
            if len(scheduled_executions_history) > 50:
                scheduled_executions_history.pop(0)
                
    except Exception as e:
        print(f"Failed to start process {process_type} on {host}: {e}")


async def schedule_process(process_type: str, nemesis_config: dict, custom_interval: int = None):
    while True:
        if process_type not in scheduled_tasks or not scheduled_tasks[process_type]['enabled']:
            break

        # Get config for this process type
        p_config = nemesis_config.get(process_type, {})
        process_def = PROCESS_TYPES[process_type]
        
        # Use custom interval if provided, otherwise fall back to config
        interval = custom_interval if custom_interval is not None else p_config.get('schedule', process_def.get('schedule', 60))

        if 'runner' in process_def:
            runner = process_def['runner']
            # Delegate logic to the runner
            action, target_hosts = runner.prepare_fault(hosts, p_config)

            if action and target_hosts:
                tasks = []
                for host in target_hosts:
                    tasks.append(run_process_on_host(host, process_type, action=action, track_history=True))

                if tasks:
                    await asyncio.gather(*tasks)

        await asyncio.sleep(interval)


@router.get("/api/hosts/{host}/processes", response_model=List[ProcessInfo])
async def get_all_host_processes(host: str):
    if is_local_host(host):
        # Direct call to avoid HTTP deadlock
        from ydb.tests.stability.agent.agent_router import get_all_processes
        return await get_all_processes()
    else:
        return requests.get(f"http://{host}:31434/api/processes").json()


async def fetch_host_processes(host):
    try:
        if is_local_host(host):
            # Direct call to avoid HTTP deadlock
            from ydb.tests.stability.agent.agent_router import get_all_processes
            return host, await get_all_processes()
        else:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: requests.get(f"http://{host}:31434/api/processes", timeout=5))
            return host, resp.json()
    except Exception as e:
        print(f"Failed to fetch processes from {host}: {e}")
        return host, []


@router.get("/api/hosts/processes", response_model=Dict[str, List[ProcessInfo]])
async def get_all_processes():
    tasks = [fetch_host_processes(host) for host in hosts]
    results = await asyncio.gather(*tasks)
    return {host: procs for host, procs in results}


@router.post("/api/hosts/process", response_model=Dict[str, Any])
async def create_host_process(req: CreateHostProcessRequest):
    if req.type not in PROCESS_TYPES:
        return {"status": "error", "message": "Invalid process type"}
    if req.host not in hosts:
        return {"status": "error", "message": "Invalid host"}
    
    # Check if this nemesis type is currently scheduled
    if req.type in scheduled_tasks and scheduled_tasks[req.type].get('enabled', False):
        return {"status": "error", "message": f"Cannot manually run {req.type}: it is currently scheduled. Disable scheduling first."}

    try:
        action = req.action if req.action else 'inject'

        if is_local_host(req.host):
            # Direct call to avoid HTTP deadlock
            from ydb.tests.stability.agent.agent_router import create_process
            from ydb.tests.stability.agent.models import CreateProcessRequest

            process_req = CreateProcessRequest(type=req.type, action=action)
            result = await create_process(process_req)
            return result
        else:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: requests.post(f"http://{req.host}:31434/api/processes", json={'type': req.type, 'action': action}, timeout=5))
            return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/process_types")
async def get_process_types():
    """Return process types with their descriptions"""
    result = []
    for name, definition in PROCESS_TYPES.items():
        runner = definition.get('runner')
        description = runner.nemesis_description if runner and hasattr(runner, 'nemesis_description') else ""
        result.append({
            "name": name,
            "description": description
        })
    return result


@router.get("/api/hosts/health", response_model=Dict[str, Any])
async def get_hosts_health():
    aggregated_health = {}
    for host in hosts:
        try:
            if is_local_host(host):
                # Direct response for local host
                aggregated_health[host] = {"status": "ok"}
            else:
                resp = requests.get(f"http://{host}:31434/health")
                aggregated_health[host] = resp.json()
        except Exception as e:
            aggregated_health[host] = {"status": "error", "message": str(e)}
    return aggregated_health


@router.post("/api/schedule", response_model=Dict[str, Any])
async def set_schedule(req: SetScheduleRequest):
    if req.type not in PROCESS_TYPES:
        return {"status": "error", "message": "Invalid process type"}

    # Import here to get the current nemesis_config
    from ydb.tests.stability.agent.app import nemesis_config

    if req.enabled:
        if req.type in scheduled_tasks and scheduled_tasks[req.type]['enabled']:
            return {"status": "ok", "message": "Already enabled"}

        scheduled_tasks[req.type] = {
            'enabled': True,
            'interval': req.interval
        }
        task = asyncio.create_task(schedule_process(req.type, nemesis_config, req.interval))
        scheduled_tasks[req.type]['task'] = task
    else:
        if req.type in scheduled_tasks:
            scheduled_tasks[req.type]['enabled'] = False
            if 'task' in scheduled_tasks[req.type]:
                scheduled_tasks[req.type]['task'].cancel()
            del scheduled_tasks[req.type]

    return {"status": "ok"}


@router.get("/api/schedule")
async def get_schedule():
    """Return schedule status with intervals"""
    result = {}
    for pt in PROCESS_TYPES:
        if pt in scheduled_tasks and scheduled_tasks[pt]['enabled']:
            result[pt] = {
                "enabled": True,
                "interval": scheduled_tasks[pt].get('interval')
            }
        else:
            result[pt] = {"enabled": False, "interval": None}
    return result


@router.get("/api/schedule/history")
async def get_schedule_history():
    """Return last scheduled executions"""
    # Return last 5 in reverse order (newest first)
    return scheduled_executions_history[-15:][::-1]


@router.get("/api/healthcheck", response_model=Dict[str, Any])
async def get_healthcheck():
    # Import here to get the current healthcheck_reporter
    from ydb.tests.stability.agent.app import healthcheck_reporter

    if healthcheck_reporter:
        return healthcheck_reporter.last_results
    return {}


@router.post("/api/config/reload", response_model=Dict[str, Any])
async def reload_config():
    # Import here to get the load function
    from ydb.tests.stability.agent.app import load_nemesis_config

    config = load_nemesis_config()
    return {"status": "ok", "config": config}

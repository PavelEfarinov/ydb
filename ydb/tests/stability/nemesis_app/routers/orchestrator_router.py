import asyncio
import logging
import socket
from typing import List, Dict, Any

import requests
from fastapi import APIRouter
from ydb.tests.stability.nemesis_app.internal.defaults import PROCESS_TYPES
from ydb.tests.stability.nemesis_app.internal.models import ProcessInfo, SetScheduleRequest, CreateHostProcessRequest
from ydb.tests.stability.nemesis_app.internal.orchestrator_warden_checker import (
    OrchestratorWardenChecker,
    get_all_warden_definitions,
)


logger = logging.getLogger(__name__)


router = APIRouter()

# Module-level state
hosts = []
scheduled_tasks = {}
scheduled_executions_history = []  # List of {type, action, host, timestamp}
mon_port = 8765  # Default monitoring port
orchestrator_warden_checker: OrchestratorWardenChecker = None  # initialized in app.py


def get_app_port() -> int:
    """Get the configured app port from settings"""
    from ydb.tests.stability.nemesis_app.internal.config import Settings
    return Settings().app_port


def is_local_host(host: str) -> bool:
    """Check if the host is the local machine"""
    try:
        # Get configured app_host from settings
        from ydb.tests.stability.nemesis_app.internal.config import Settings
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
            from ydb.tests.stability.nemesis_app.routers.agent_router import create_process
            from ydb.tests.stability.nemesis_app.internal.models import CreateProcessRequest

            req = CreateProcessRequest(type=process_type, action=action)
            result = await create_process(req)
            print(f"Started process {process_type} locally with action {action}: {result}")
        else:
            # Remote call via HTTP
            port = get_app_port()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: requests.post(f"http://{host}:{port}/api/processes", json={'type': process_type, 'action': action}, timeout=5))

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
        from ydb.tests.stability.nemesis_app.routers.agent_router import get_all_processes
        return await get_all_processes()
    else:
        port = get_app_port()
        return requests.get(f"http://{host}:{port}/api/processes").json()


async def fetch_host_processes(host):
    try:
        if is_local_host(host):
            # Direct call to avoid HTTP deadlock
            from ydb.tests.stability.nemesis_app.routers.agent_router import get_all_processes
            return host, await get_all_processes()
        else:
            port = get_app_port()
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: requests.get(f"http://{host}:{port}/api/processes", timeout=5))
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
            from ydb.tests.stability.nemesis_app.routers.agent_router import create_process
            from ydb.tests.stability.nemesis_app.internal.models import CreateProcessRequest

            process_req = CreateProcessRequest(type=req.type, action=action)
            result = await create_process(process_req)
            return result
        else:
            port = get_app_port()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: requests.post(f"http://{req.host}:{port}/api/processes", json={'type': req.type, 'action': action}, timeout=5))
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
                port = get_app_port()
                resp = requests.get(f"http://{host}:{port}/health")
                aggregated_health[host] = resp.json()
        except Exception as e:
            aggregated_health[host] = {"status": "error", "message": str(e)}
    return aggregated_health


@router.post("/api/schedule", response_model=Dict[str, Any])
async def set_schedule(req: SetScheduleRequest):
    if req.type not in PROCESS_TYPES:
        return {"status": "error", "message": "Invalid process type"}

    # Import here to get the current nemesis_config
    from ydb.tests.stability.nemesis_app.app import nemesis_config

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
    from ydb.tests.stability.nemesis_app.app import healthcheck_reporter

    if healthcheck_reporter:
        return healthcheck_reporter.last_results
    return {}


@router.post("/api/config/reload", response_model=Dict[str, Any])
async def reload_config():
    # Import here to get the load function
    from ydb.tests.stability.nemesis_app.app import load_nemesis_config

    config = load_nemesis_config()
    return {"status": "ok", "config": config}


@router.post("/api/hosts/warden/start", response_model=Dict[str, Any])
async def start_warden_checks_on_all_hosts():
    """
    Start warden checks:
    - Liveness checks run centrally on master (HTTP monitoring)
    - Safety checks run on each agent (local log/dmesg access)
    """
    logger.info(f"Starting warden checks on all hosts. Total hosts: {len(hosts)}")
    results = {"agents": {}, "master": {}}

    # 1. Start orchestrator checks (liveness + orchestrator safety)
    logger.info("Starting orchestrator warden checks (liveness + PDisk + aggregated)")
    orchestrator_started = await orchestrator_warden_checker.start_checks()
    results["master"] = {
        "status": "started" if orchestrator_started else "already_running",
        "type": "liveness"
    }
    logger.info(f"Orchestrator checks: {'started' if orchestrator_started else 'already running'}")

    # 2. Start safety checks on all agents
    async def start_safety_on_host(host):
        try:
            logger.debug(f"Starting safety checks on agent: {host}")
            if is_local_host(host):
                # Direct call to avoid HTTP deadlock
                from ydb.tests.stability.nemesis_app.routers.agent_router import start_warden_checks
                result = await start_warden_checks()
                logger.debug(f"Agent {host} (local): {result.get('status', 'unknown')}")
                return host, result
            else:
                port = get_app_port()
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(
                    None,
                    lambda: requests.post(f"http://{host}:{port}/api/warden/start", timeout=10)
                )
                result = resp.json()
                logger.debug(f"Agent {host} (remote): {result.get('status', 'unknown')}")
                return host, result
        except Exception as e:
            logger.error(f"Failed to start safety checks on agent {host}: {e}")
            return host, {"status": "error", "message": str(e)}

    tasks = [start_safety_on_host(host) for host in hosts]
    task_results = await asyncio.gather(*tasks)

    started_count = 0
    error_count = 0
    for host, result in task_results:
        results["agents"][host] = result
        if result.get("status") == "started":
            started_count += 1
        elif result.get("status") == "error":
            error_count += 1

    logger.info(f"Agent safety checks initiated: {started_count} started, {error_count} errors, {len(hosts) - started_count - error_count} already running")

    return {"status": "ok", "results": results}


@router.get("/api/hosts/warden/results", response_model=Dict[str, Any])
async def get_warden_results_from_all_hosts():
    """
    Get combined warden check results:
    - Liveness checks from orchestrator
    - Safety checks from each agent
    - Aggregated safety checks from orchestrator (e.g., UnifiedAgentVerifyFailedAggregated)
    """
    logger.debug("Fetching warden results from all hosts")

    # Get orchestrator results (liveness + safety including aggregated checks)
    orchestrator_result = orchestrator_warden_checker.get_last_result()
    logger.debug(f"Orchestrator status: {orchestrator_result.get('status', 'unknown')}")

    # Get safety results from all agents
    agent_results = {}

    async def get_safety_from_host(host):
        try:
            if is_local_host(host):
                # Direct call to avoid HTTP deadlock
                from ydb.tests.stability.nemesis_app.routers.agent_router import get_warden_result
                result = await get_warden_result()
                return host, result
            else:
                port = get_app_port()
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(
                    None,
                    lambda: requests.get(f"http://{host}:{port}/api/warden/result", timeout=10)
                )
                return host, resp.json()
        except Exception as e:
            logger.error(f"Failed to get warden result from {host}: {e}")
            return host, {"status": "error", "error_message": str(e)}

    tasks = [get_safety_from_host(host) for host in hosts]
    task_results = await asyncio.gather(*tasks)

    for host, result in task_results:
        agent_results[host] = result

    # Log summary of agent statuses
    status_summary = {}
    for host, result in agent_results.items():
        status = result.get("status", "unknown")
        status_summary[status] = status_summary.get(status, 0) + 1
    logger.debug(f"Agent results summary: {status_summary}")

    # Combine results: orchestrator liveness + agent safety per host
    combined_results = {}

    # Add orchestrator as a special entry with liveness checks and safety checks
    combined_results["_orchestrator"] = {
        "status": orchestrator_result.get("status", "idle"),
        "started_at": orchestrator_result.get("started_at"),
        "completed_at": orchestrator_result.get("completed_at"),
        "liveness_checks": orchestrator_result.get("liveness_checks", []),
        "safety_checks": orchestrator_result.get("safety_checks", []),  # PDisk checks + aggregated checks
        "error_message": orchestrator_result.get("error_message")
    }

    # Add agent results (safety checks only)
    for host, result in agent_results.items():
        combined_results[host] = {
            "status": result.get("status", "idle"),
            "started_at": result.get("started_at"),
            "completed_at": result.get("completed_at"),
            "liveness_checks": [],  # Agents don't run liveness checks
            "safety_checks": result.get("safety_checks", []),
            "error_message": result.get("error_message")
        }

    return combined_results


@router.get("/api/warden/checks", response_model=List[Dict[str, Any]])
async def get_all_available_warden_checks():
    """
    Get list of all available warden checks across the system.

    Returns checks from:
    - Agent safety wardens (run on each agent)
    - Orchestrator liveness wardens (run centrally)
    - Orchestrator safety wardens (run centrally via HTTP)
    """
    return get_all_warden_definitions()

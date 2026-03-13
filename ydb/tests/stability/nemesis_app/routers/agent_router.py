import asyncio
import logging
import io
import socket
import threading
from typing import List, Dict, Any

from fastapi import APIRouter
from ydb.tests.stability.nemesis_app.internal.defaults import PROCESS_TYPES
from ydb.tests.stability.nemesis_app.internal.models import CreateProcessRequest, ProcessInfo
from ydb.tests.stability.nemesis_app.internal.agent_warden_checker import AgentWardenChecker


logger = logging.getLogger(__name__)


class ProcessManager:
    def __init__(self):
        self.processes = []

    async def start_process(self, type_name: str, runner, action='inject'):
        proc_id = len(self.processes)
        proc_data = {
            "id": proc_id,
            "type": type_name,
            "command": f"{type_name} ({action})",
            "logs": "",
            "ret_code": None,
            "status": "running"
        }
        self.processes.append(proc_data)

        asyncio.create_task(self._run(proc_id, runner, action))
        return proc_data

    async def _run(self, proc_id, runner, action):
        try:
            # It's a nemesis runner
            log_capture_string = io.StringIO()

            # Custom handler that filters logs based on thread ID
            class ThreadLocalHandler(logging.StreamHandler):
                def __init__(self, stream, thread_id):
                    super().__init__(stream)
                    self.thread_id = thread_id

                def filter(self, record):
                    return record.thread == self.thread_id

            # We need to know the thread ID where inject_fault will run
            # Since run_in_executor runs in a thread pool, we can't know the ID beforehand easily
            # But we can wrap the execution to set up logging inside the thread

            def execute_with_logging():
                thread_id = threading.get_ident()
                handler = ThreadLocalHandler(log_capture_string, thread_id)
                handler.setLevel(logging.INFO)

                # Configure root logger to ensure it processes INFO logs
                root_logger = logging.getLogger()
                original_level = root_logger.level
                root_logger.setLevel(logging.INFO)

                root_logger.addHandler(handler)
                try:
                    if action == 'inject':
                        runner.inject_fault()
                    elif action == 'extract':
                        runner.extract_fault()
                    else:
                        raise Exception('Unknown action type')
                finally:
                    root_logger.removeHandler(handler)
                    root_logger.setLevel(original_level)

            # Background task to flush logs
            async def flush_logs():
                while self.processes[proc_id]['status'] == 'running':
                    self.processes[proc_id]['logs'] = log_capture_string.getvalue()
                    await asyncio.sleep(1)
                # Final flush
                self.processes[proc_id]['logs'] = log_capture_string.getvalue()

            loop = asyncio.get_running_loop()
            flush_task = asyncio.create_task(flush_logs())

            try:
                await loop.run_in_executor(None, execute_with_logging)
                self.processes[proc_id]['status'] = 'finished'
                self.processes[proc_id]['ret_code'] = 0
            except Exception as e:
                import traceback
                self.processes[proc_id]['status'] = 'error'
                self.processes[proc_id]['ret_code'] = 1
                await flush_task
                self.processes[proc_id]['logs'] = log_capture_string.getvalue() + f"\nError executing process: {str(e)}\n{traceback.format_exc()}"
            log_capture_string.close()

        except Exception as e:
            import traceback
            self.processes[proc_id]['logs'] += f"\nError setting up process: {str(e)}\n{traceback.format_exc()}"
            self.processes[proc_id]['status'] = 'error'
            self.processes[proc_id]['ret_code'] = 1

    def get_all(self):
        return self.processes


manager = ProcessManager()
router = APIRouter()
warden_checker: AgentWardenChecker = None  # initialized in app.py


@router.get("/api/processes", response_model=List[ProcessInfo])
async def get_all_processes():
    return manager.get_all()


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


@router.post("/api/processes")
async def create_process(req: CreateProcessRequest):
    if req.type not in PROCESS_TYPES:
        return {"status": "error", "message": "Invalid process type"}

    process_def = PROCESS_TYPES[req.type]
    runner = process_def['runner']

    action = getattr(req, 'action', 'inject')

    await manager.start_process(req.type, runner, action)
    return {"status": "started"}


@router.post("/api/warden/start", response_model=Dict[str, Any])
async def start_warden_checks():
    """Start warden checks asynchronously."""
    hostname = socket.gethostname()
    logger.info(f"[{hostname}] Agent warden checks start requested")

    started = await warden_checker.start_checks()
    if started:
        logger.info(f"[{hostname}] Agent warden checks started successfully")
        return {"status": "started"}
    else:
        logger.info(f"[{hostname}] Agent warden checks already running")
        return {"status": "already_running"}


@router.get("/api/warden/result", response_model=Dict[str, Any])
async def get_warden_result():
    """Get the last warden check result."""
    hostname = socket.gethostname()
    result = warden_checker.get_last_result()
    status = result.get("status", "unknown")
    safety_count = len(result.get("safety_checks", []))
    logger.debug(f"[{hostname}] Agent warden result requested: status={status}, safety_checks={safety_count}")
    return result

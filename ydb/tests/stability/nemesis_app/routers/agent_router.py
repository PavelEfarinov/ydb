import logging
import io
import socket
import threading
import time
import asyncio

from flask import Blueprint, request, jsonify

from ydb.tests.stability.nemesis_app.internal.defaults import PROCESS_TYPES
from ydb.tests.stability.nemesis_app.internal.agent_warden_checker import AgentWardenChecker


logger = logging.getLogger(__name__)


class ProcessManager:
    def __init__(self):
        self.processes = []
        self.processes_lock = threading.Lock()

    def start_process(self, type_name: str, runner, action='inject'):
        with self.processes_lock:
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

        # Start process in a daemon thread
        thread = threading.Thread(target=self._run, args=(proc_id, runner, action))
        thread.daemon = True
        thread.start()
        return proc_data

    def _run(self, proc_id, runner, action):
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

            def execute_with_logging():
                thread_id = threading.get_ident()
                handler = ThreadLocalHandler(log_capture_string, thread_id)
                handler.setLevel(logging.DEBUG)  # Capture all levels

                # Configure root logger to ensure it processes all logs
                root_logger = logging.getLogger()
                original_level = root_logger.level
                root_logger.setLevel(logging.DEBUG)

                # Add handler to root logger
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

            # Background task to flush logs using threading.Timer
            stop_flushing = threading.Event()

            def flush_logs():
                while not stop_flushing.is_set():
                    with self.processes_lock:
                        if proc_id < len(self.processes):
                            self.processes[proc_id]['logs'] = log_capture_string.getvalue()
                    time.sleep(1)
                # Final flush
                with self.processes_lock:
                    if proc_id < len(self.processes):
                        self.processes[proc_id]['logs'] = log_capture_string.getvalue()

            flush_thread = threading.Thread(target=flush_logs)
            flush_thread.daemon = True
            flush_thread.start()

            try:
                execute_with_logging()
                # Force final log capture
                final_logs = log_capture_string.getvalue()
                with self.processes_lock:
                    if proc_id < len(self.processes):
                        self.processes[proc_id]['logs'] = final_logs
                        self.processes[proc_id]['status'] = 'finished'
                        self.processes[proc_id]['ret_code'] = 0
                stop_flushing.set()
                flush_thread.join(timeout=2)
            except Exception as e:
                import traceback
                final_logs = log_capture_string.getvalue()
                stop_flushing.set()
                flush_thread.join(timeout=2)
                with self.processes_lock:
                    if proc_id < len(self.processes):
                        self.processes[proc_id]['logs'] = final_logs + f"\nError executing process: {str(e)}\n{traceback.format_exc()}"
                        self.processes[proc_id]['status'] = 'error'
                        self.processes[proc_id]['ret_code'] = 1
            log_capture_string.close()
        except Exception as e:
            import traceback
            with self.processes_lock:
                if proc_id < len(self.processes):
                    self.processes[proc_id]['logs'] += f"\nError setting up process: {str(e)}\n{traceback.format_exc()}"
                    self.processes[proc_id]['status'] = 'error'
                    self.processes[proc_id]['ret_code'] = 1

    def get_all(self):
        with self.processes_lock:
            return self.processes.copy()


manager = ProcessManager()
blueprint = Blueprint('agent', __name__)
warden_checker: AgentWardenChecker = None  # initialized in app.py


# Helper functions that can be called directly (without Flask request context)
def get_all_processes_helper():
    """Helper function to get all processes (can be called directly)"""
    return manager.get_all()


def create_process_helper(process_type: str, action: str = 'inject'):
    """Helper function to create a process (can be called directly)"""
    if process_type not in PROCESS_TYPES:
        return {"status": "error", "message": "Invalid process type"}

    process_def = PROCESS_TYPES[process_type]
    runner = process_def['runner']

    manager.start_process(process_type, runner, action)
    return {"status": "started"}


def start_warden_checks_helper():
    """Helper function to start warden checks (can be called directly)"""
    hostname = socket.gethostname()
    logger.info(f"[{hostname}] Agent warden checks start requested")

    # Handle async method in sync context
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If there's already a running loop, create a new one
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, warden_checker.start_checks())
                started = future.result()
        else:
            started = asyncio.run(warden_checker.start_checks())
    except RuntimeError:
        # No event loop, create one
        started = asyncio.run(warden_checker.start_checks())

    if started:
        logger.info(f"[{hostname}] Agent warden checks started successfully")
        return {"status": "started"}
    else:
        logger.info(f"[{hostname}] Agent warden checks already running")
        return {"status": "already_running"}


def get_warden_result_helper():
    """Helper function to get warden result (can be called directly)"""
    hostname = socket.gethostname()
    result = warden_checker.get_last_result()
    status = result.get("status", "unknown")
    safety_count = len(result.get("safety_checks", []))
    logger.debug(f"[{hostname}] Agent warden result requested: status={status}, safety_checks={safety_count}")
    return result


# Flask route functions (call the helper functions)
@blueprint.route("/api/processes", methods=["GET"])
def get_all_processes():
    return jsonify(get_all_processes_helper())


@blueprint.route("/api/process_types", methods=["GET"])
def get_process_types():
    """Return process types with their descriptions"""
    result = []
    for name, definition in PROCESS_TYPES.items():
        runner = definition.get('runner')
        description = runner.nemesis_description if runner and hasattr(runner, 'nemesis_description') else ""
        result.append({
            "name": name,
            "description": description
        })
    return jsonify(result)


@blueprint.route("/api/processes", methods=["POST"])
def create_process():
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No data provided"}), 400

    process_type = data.get("type")
    if not process_type:
        return jsonify({"status": "error", "message": "Missing type field"}), 400

    action = data.get("action", "inject")

    result = create_process_helper(process_type, action)
    if result.get("status") == "error":
        return jsonify(result), 400
    return jsonify(result)


@blueprint.route("/api/warden/start", methods=["POST"])
def start_warden_checks():
    """Start warden checks."""
    return jsonify(start_warden_checks_helper())


@blueprint.route("/api/warden/result", methods=["GET"])
def get_warden_result():
    """Get the last warden check result."""
    return jsonify(get_warden_result_helper())

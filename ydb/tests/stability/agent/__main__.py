from contextlib import asynccontextmanager
from functools import lru_cache
import signal
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn
import asyncio
import requests
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
from ydb.tests.stability.agent.install import get_hosts_from_yaml, install_on_hosts, stop_agent_services
from ydb.tests.stability.agent import config


class ProcessInfo(BaseModel):
    id: int
    type: str
    command: str
    stdout: str
    stderr: str
    ret_code: Optional[int]
    status: str


class ProcessType(BaseModel):
    name: str
    command: str


PROCESS_TYPES = {
    "type1": {"cmd": "echo 'Type 1 process started'; sleep 1; echo 'Type 1 output'; sleep 1; echo 'Type 1 finished'"},
    "type2": {"cmd": "echo 'Type 2 process started'; sleep 1; echo 'Type 2 error' >&2; sleep 1; echo 'Type 2 finished'"},
    "NodeKiller": {
        "cmd": "ps aux | grep '\\--ic-port' | grep -v grep | awk '{ print $2 }' | tail -n 1 | xargs -r sudo kill -%d" % (
            int(signal.SIGKILL),
        ),
        "run_timeout_seconds": 300
    },
}


class CreateProcessRequest(BaseModel):
    type: str


class ProcessManager:
    def __init__(self):
        self.processes = []

    async def start_process(self, type_name: str, command: str):
        proc_id = len(self.processes)
        proc_data = {
            "id": proc_id,
            "type": type_name,
            "command": command,
            "stdout": "",
            "stderr": "",
            "ret_code": None,
            "status": "running"
        }
        self.processes.append(proc_data)

        asyncio.create_task(self._run(proc_id, command))
        return proc_data

    async def _run(self, proc_id, command):
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            async def read_stream(stream, key):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    self.processes[proc_id][key] += line.decode()

            await asyncio.gather(
                read_stream(process.stdout, 'stdout'),
                read_stream(process.stderr, 'stderr')
            )

            ret_code = await process.wait()
            self.processes[proc_id]['ret_code'] = ret_code
            self.processes[proc_id]['status'] = 'finished' if ret_code == 0 else 'failed'
        except Exception as e:
            self.processes[proc_id]['stderr'] += f"\nError executing process: {str(e)}"
            self.processes[proc_id]['status'] = 'error'

    def get_all(self):
        return self.processes


manager = ProcessManager()


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
if get_settings().nemesis_type != 'agent':
    app.mount("/static", StaticFiles(directory=get_settings().static_location), name="static")


@app.get("/health")
async def get_health():
    return {"status": "ok"}


@app.get("/api/processes", response_model=List[ProcessInfo])
async def get_all_processes():
    return manager.get_all()


@app.get("/api/{host}/processes", response_model=List[ProcessInfo])
async def get_all_host_processes(host: str):
    return requests.get(f"http://{host}:31434/api/processes").json()


@app.get("/api/process_types", response_model=List[str])
async def get_process_types():
    return list(PROCESS_TYPES.keys())


@app.post("/api/processes")
async def create_process(req: CreateProcessRequest):
    if req.type not in PROCESS_TYPES:
        return {"status": "error", "message": "Invalid process type"}

    command = PROCESS_TYPES[req.type]
    await manager.start_process(req.type, command['cmd'])
    return {"status": "started"}


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

if __name__ == "__main__":
    # workers=1 is important because we store state in memory
    uvicorn.run(
        "ydb.tests.stability.agent.__main__:app", host=get_settings().app_host, port=31434, workers=1
    )

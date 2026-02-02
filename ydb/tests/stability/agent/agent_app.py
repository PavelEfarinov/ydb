import asyncio
from typing import List

from fastapi import FastAPI
from ydb.tests.stability.agent.defaults import PROCESS_TYPES
from ydb.tests.stability.agent.models import CreateProcessRequest, ProcessInfo


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
app = FastAPI()


@app.get("/health")
async def get_health():
    return {"status": "ok"}


@app.get("/api/processes", response_model=List[ProcessInfo])
async def get_all_processes():
    return manager.get_all()


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
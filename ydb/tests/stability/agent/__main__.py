from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn
import asyncio
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


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
    "type1": "echo 'Type 1 process started'; sleep 1; echo 'Type 1 output'; sleep 1; echo 'Type 1 finished'",
    "type2": "echo 'Type 2 process started'; sleep 1; echo 'Type 2 error' >&2; sleep 1; echo 'Type 2 finished'",
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
    await manager.start_process(req.type, command)
    return {"status": "started"}


if __name__ == "__main__":
    # workers=1 is important because we store state in memory
    uvicorn.run(
        "ydb.tests.stability.agent.__main__:app", host='::', port=8084, workers=1
    )

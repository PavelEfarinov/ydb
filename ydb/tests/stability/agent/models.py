from typing import Optional
from pydantic import BaseModel


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


class CreateProcessRequest(BaseModel):
    type: str


class SetScheduleRequest(BaseModel):
    type: str
    enabled: bool


class CreateHostProcessRequest(BaseModel):
    host: str
    type: str
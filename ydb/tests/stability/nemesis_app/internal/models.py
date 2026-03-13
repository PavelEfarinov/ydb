from typing import Optional
from pydantic import BaseModel


class ProcessInfo(BaseModel):
    id: int
    type: str
    command: str
    logs: str
    ret_code: Optional[int]
    status: str


class ProcessType(BaseModel):
    name: str
    command: str


class CreateProcessRequest(BaseModel):
    type: str
    action: Optional[str] = 'inject'


class SetScheduleRequest(BaseModel):
    type: str
    enabled: bool
    interval: Optional[int] = None  # Custom interval in seconds


class CreateHostProcessRequest(BaseModel):
    host: str
    type: str
    action: Optional[str] = 'inject'

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MessageRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=100)
    user_id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=20000)


class MessageAccepted(BaseModel):
    message_id: str
    task_id: str | None = None
    status: str


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['new_task', 'follow_up', 'dependent_task', 'clarification']
    target_task_ids: list[str]
    reason: str

    @model_validator(mode='after')
    def validate_targets(self):
        count = len(self.target_task_ids)
        if count != len(set(self.target_task_ids)):
            raise ValueError('Duplicate target tasks')
        if self.action in {'new_task', 'clarification'} and count:
            raise ValueError('This action cannot reference tasks')
        if self.action == 'follow_up' and count != 1:
            raise ValueError('A follow-up needs exactly one task')
        if self.action == 'dependent_task' and not count:
            raise ValueError('A dependent task needs prerequisites')
        return self


class ORMResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MessageResponse(ORMResponse):
    message_id: str
    chat_id: str
    user_id: str
    content: str
    task_id: str | None
    created_at: datetime
    sequence: int
    routing_status: str


class DependencyResponse(ORMResponse):
    prerequisite_task_id: str
    prerequisite_version: int


class DecisionResponse(ORMResponse):
    decision_id: str
    task_id: str
    version: int
    prerequisite_task_id: str
    original_version: int
    updated_version: int
    status: str
    choice: str | None
    answer_published: bool = False


class VersionResponse(ORMResponse):
    task_id: str
    version: int
    message_id: str | None
    content: str
    status: str
    result: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None
    attempts: int


class TaskResponse(VersionResponse):
    chat_id: str
    user_id: str
    original_message_id: str
    dependencies: list[DependencyResponse]
    pending_decisions: list[DecisionResponse]


class EventResponse(ORMResponse):
    event_id: str
    chat_id: str
    sequence: int
    type: str
    bot_id: str
    task_id: str | None
    version: int | None
    message_id: str | None
    payload: dict
    created_at: datetime


class EventPage(BaseModel):
    events: list[EventResponse]
    next_cursor: int


class DecisionRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=100)
    choice: Literal['keep_original', 'use_updated']

"""Logical identities, versioned executions, exact-version edges and outbox."""
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


ACTIVE = {'queued', 'running', 'waiting_for_dependencies', 'waiting_for_decision'}
TERMINAL = {'completed', 'failed', 'cancelled'}


class TaskRecord(Base):
    __tablename__ = 'tasks'
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(36))
    chat_id: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Compatibility snapshots only; workers always read TaskVersion.
    content: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(30), default='queued')
    result: Mapped[str | None] = mapped_column(Text)


class TaskVersion(Base):
    __tablename__ = 'task_versions'
    task_id: Mapped[str] = mapped_column(ForeignKey('tasks.task_id'), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str | None] = mapped_column(String(36))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), index=True)
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    run_token: Mapped[str | None] = mapped_column(String(36))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatState(Base):
    __tablename__ = 'chat_states'
    chat_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    last_message_sequence: Mapped[int] = mapped_column(Integer, default=0)


class ChatMessage(Base):
    __tablename__ = 'messages'
    message_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(100), index=True)
    user_id: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    task_id: Mapped[str | None] = mapped_column(ForeignKey('tasks.task_id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sequence: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    routing_status: Mapped[str] = mapped_column(String(20), default='stored_no_action', server_default='stored_no_action', index=True)


class TaskDependency(Base):
    __tablename__ = 'task_dependencies'
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    prerequisite_task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    prerequisite_version: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(['task_id', 'version'], ['task_versions.task_id', 'task_versions.version']),
        ForeignKeyConstraint(['prerequisite_task_id', 'prerequisite_version'], ['task_versions.task_id', 'task_versions.version']),
    )


class DependencyDecision(Base):
    __tablename__ = 'dependency_decisions'
    decision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer)
    prerequisite_task_id: Mapped[str] = mapped_column(String(36))
    original_version: Mapped[int] = mapped_column(Integer)
    updated_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    choice: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(['task_id', 'version'], ['task_versions.task_id', 'task_versions.version']),
        ForeignKeyConstraint(['prerequisite_task_id', 'original_version'], ['task_versions.task_id', 'task_versions.version']),
        ForeignKeyConstraint(['prerequisite_task_id', 'updated_version'], ['task_versions.task_id', 'task_versions.version']),
    )


class ChatEvent(Base):
    __tablename__ = 'chat_events'
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(100), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    bot_id: Mapped[str] = mapped_column(String(30), default='CastleBot')
    task_id: Mapped[str | None] = mapped_column(String(36))
    version: Mapped[int | None] = mapped_column(Integer)
    message_id: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint('chat_id', 'sequence'),)

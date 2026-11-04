from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent
ReasoningEffort = Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']


class Settings(BaseSettings):
    # Independent of the shell's working directory; explicit environment overrides
    # retain conventional precedence and can be inspected with check_configuration.
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore', hide_input_in_errors=True)
    database_url: str = Field(default='postgresql+asyncpg://localhost/castle_agent', repr=False)
    llm_provider: Literal['openai'] = 'openai'
    openai_api_key: SecretStr | None = None
    router_model: str = 'gpt-6-luna'
    executor_model: str = 'gpt-6.1-terra'
    router_reasoning_effort: ReasoningEffort | None = 'none'
    executor_reasoning_effort: ReasoningEffort | None = 'low'
    router_max_output_tokens: int = Field(default=700, ge=128, le=16000)
    max_concurrent_agents: int = Field(default=3, ge=1, le=100)
    max_concurrent_routes: int = Field(default=3, ge=1, le=100)
    api_timeout_seconds: float = Field(default=60, gt=0)
    task_timeout_seconds: float = Field(default=240, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_base_seconds: float = Field(default=1, ge=0)
    max_output_tokens: int = Field(default=4096, ge=128, le=16000)
    context_messages: int = Field(default=30, ge=1, le=100)
    context_tasks: int = Field(default=20, ge=1, le=100)
    retention_days: int = Field(default=30, ge=1)
    scheduler_interval_seconds: float = Field(default=0.5, gt=0)
    routing_structured_output: bool = True
    cors_origins: list[str] = []
    demo_execution_delay_seconds: float = Field(default=0, ge=0, le=30)

from functools import lru_cache
import sys

from pydantic_settings import BaseSettings, SettingsConfigDict
import logging


class Settings(BaseSettings):
    app_name: str = "Nemesis App"
    nemesis_type: str = 'master'
    static_location: str = 'static'
    hosts: list[str] = []
    app_host: str = '::'
    app_port: int = 31434
    mon_port: int = 8765
    yaml_config_location: str = '/home/pefavel/ydbwork/arcadia/kikimr/ci/stability/resources/ydb_myt_3_dc_stability_testing/cluster.yaml'

    model_config = SettingsConfigDict(env_file=".env")

    @classmethod
    def from_args(cls, **kwargs) -> 'Settings':
        """
        Create Settings instance with argv arguments having highest priority.
        Priority: argv > env > default values
        """
        # Get base settings (env + defaults)
        base_settings = cls()

        # Override with argv arguments (only if provided)
        for key, value in kwargs.items():
            if value is not None and hasattr(base_settings, key):
                setattr(base_settings, key, value)
            else:
                logging.getLogger(__name__).warning(f"Invalid argument key: {key}")

        return base_settings


class AgentSettings(BaseSettings):
    app_name: str = "Nemesis Agent API"
    nemesis_type: str = 'agent'
    app_host: str = '::'
    app_port: int = 31434
    mon_port: int = 8765

    model_config = SettingsConfigDict(env_file=".env")

    @classmethod
    def from_master_args(cls, settings: Settings) -> 'AgentSettings':
        base_settings = cls()

        base_settings.app_host = settings.app_host
        base_settings.app_port = settings.app_port
        base_settings.mon_port = settings.mon_port

        return base_settings


@lru_cache
def get_master_settings(**kwargs):
    """Get settings with argv arguments having highest priority."""
    settings = Settings.from_args(**kwargs)
    print(settings, file=sys.stderr)
    return settings

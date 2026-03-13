from pydantic_settings import BaseSettings, SettingsConfigDict
import logging


class Settings(BaseSettings):
    app_name: str = "Nemesis App"
    nemesis_type: str = 'master'  # or 'agent'
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

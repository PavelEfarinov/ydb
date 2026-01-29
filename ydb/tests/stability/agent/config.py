from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Awesome API"
    nemesis_type: str = 'master'  # or 'agent'
    static_location: str = 'static'
    app_host: str = '::'
    yaml_config_location: str = '/home/pefavel/ydbwork/arcadia/kikimr/ci/stability/resources/ydb_myt_stability_testing/cluster.yaml'

    model_config = SettingsConfigDict(env_file=".env")

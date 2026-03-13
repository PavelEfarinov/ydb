import argparse
import uvicorn
from functools import lru_cache
from ydb.tests.stability.nemesis_app.internal import config
from ydb.tests.stability.nemesis_app.internal.install import get_hosts_from_yaml, install_on_hosts, stop_agent_services


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Nemesis App - Stability testing application",
        allow_abbrev=False
    )

    # Positional command argument
    parser.add_argument(
        'command',
        nargs='?',
        choices=['run', 'stop', ''],
        help='Command to run: run (install services), stop (stop services)'
    )

    # Optional settings arguments (override env and defaults)
    parser.add_argument('--nemesis-type', choices=['master', 'agent'],
                        help='Type of nemesis: master or agent')
    parser.add_argument('--app-host', help='Host to bind the application to')
    parser.add_argument('--app-port', type=int, help='Port to bind the application to')
    parser.add_argument('--yaml-config-location', help='Path to cluster.yaml config file')
    parser.add_argument('--static-location', help='Path to static files directory')

    return parser.parse_args()


@lru_cache
def get_settings(**kwargs):
    """Get settings with argv arguments having highest priority."""
    settings = config.Settings.from_args(**kwargs)
    print(settings)
    return settings


def main():
    args = parse_args()

    # Build kwargs from argv (only include non-None values)
    argv_kwargs = {}
    if args.nemesis_type is not None:
        argv_kwargs['nemesis_type'] = args.nemesis_type
    if args.app_host is not None:
        argv_kwargs['app_host'] = args.app_host
    if args.app_port is not None:
        argv_kwargs['app_port'] = args.app_port
    if args.yaml_config_location is not None:
        argv_kwargs['yaml_config_location'] = args.yaml_config_location
    if args.static_location is not None:
        argv_kwargs['static_location'] = args.static_location

    settings = get_settings(**argv_kwargs)

    # Check for command-line arguments
    if args.command == "run":
        # Install mode: deploy services and print orchestrator endpoint
        print("Installing nemesis services on cluster...")
        hosts = get_hosts_from_yaml(settings.yaml_config_location)
        settings.hosts = hosts
        print(f"Hosts: {hosts}")

        orchestrator_host = install_on_hosts(hosts, settings.yaml_config_location)

        print("\n" + "=" * 60)
        print("Installation completed successfully!")
        print(f"Orchestrator endpoint: http://{orchestrator_host}:{settings.app_port}")
        print(f"Orchestrator UI: http://{orchestrator_host}:{settings.app_port}/static/index.html")
        print("=" * 60 + "\n")
        return

    elif args.command == "stop":
        # Stop mode: stop all services on cluster
        print("Stopping nemesis services on cluster...")
        hosts = get_hosts_from_yaml(settings.yaml_config_location)
        print(f"Hosts: {hosts}")

        stop_agent_services(hosts)

        print("\n" + "=" * 60)
        print("All services stopped successfully!")
        print("=" * 60 + "\n")
        return

    # Default mode: run the application
    # workers=1 is important because we store state in memory
    uvicorn.run(
        "ydb.tests.stability.nemesis_app.app:app",
        host=settings.app_host,
        port=settings.app_port,
        workers=1
    )


if __name__ == "__main__":
    main()

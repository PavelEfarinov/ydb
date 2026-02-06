import sys
import uvicorn
from functools import lru_cache
from ydb.tests.stability.agent import config
from ydb.tests.stability.agent.install import get_hosts_from_yaml, install_on_hosts, stop_agent_services


@lru_cache
def get_settings():
    settings = config.Settings()
    print(settings)
    return settings


def main():
    settings = get_settings()

    # Check for command-line arguments
    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "install":
            # Install mode: deploy services and print orchestrator endpoint
            print("Installing nemesis services on cluster...")
            hosts = get_hosts_from_yaml(settings.yaml_config_location)
            print(f"Hosts: {hosts}")

            orchestrator_host = install_on_hosts(hosts, settings.yaml_config_location)

            print("\n" + "=" * 60)
            print("Installation completed successfully!")
            print(f"Orchestrator endpoint: http://{orchestrator_host}:31434")
            print(f"Orchestrator UI: http://{orchestrator_host}:31434/static/index.html")
            print("=" * 60 + "\n")
            return

        elif command == "stop":
            # Stop mode: stop all services on cluster
            print("Stopping nemesis services on cluster...")
            hosts = get_hosts_from_yaml(settings.yaml_config_location)
            print(f"Hosts: {hosts}")

            stop_agent_services(hosts)

            print("\n" + "=" * 60)
            print("All services stopped successfully!")
            print("=" * 60 + "\n")
            return

        else:
            print(f"Unknown command: {command}")
            print("Available commands: install, stop")
            sys.exit(1)

    # Default mode: run the application
    # workers=1 is important because we store state in memory
    uvicorn.run(
        "ydb.tests.stability.agent.app:app",
        host=settings.app_host,
        port=31434,
        workers=1
    )


if __name__ == "__main__":
    main()

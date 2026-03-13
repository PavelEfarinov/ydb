from concurrent.futures import ThreadPoolExecutor
import os
import subprocess

import yaml


class ClusterConfigError(Exception):
    """Exception raised for cluster configuration errors."""
    pass


def validate_cluster_yaml(yaml_path: str) -> None:
    """
    Validate cluster.yaml file exists and has required structure.

    Args:
        yaml_path: Path to the cluster.yaml file

    Raises:
        ClusterConfigError: If file doesn't exist or has invalid structure
    """
    if not yaml_path:
        raise ClusterConfigError("yaml_config_location is not specified")

    if not os.path.exists(yaml_path):
        raise ClusterConfigError(f"Cluster config file not found: {yaml_path}")

    try:
        with open(yaml_path, 'r') as f:
            yaml_config = yaml.safe_load(f.read())
    except yaml.YAMLError as e:
        raise ClusterConfigError(f"Invalid YAML in cluster config: {e}")
    except Exception as e:
        raise ClusterConfigError(f"Failed to read cluster config: {e}")

    if yaml_config is None:
        raise ClusterConfigError("Cluster config is empty")

    if not isinstance(yaml_config, dict):
        raise ClusterConfigError("Cluster config must be a YAML dictionary")

    if 'hosts' not in yaml_config:
        raise ClusterConfigError("Cluster config must contain 'hosts' section")

    hosts = yaml_config.get('hosts', [])
    if not hosts:
        raise ClusterConfigError("Cluster config 'hosts' section is empty")

    for i, host in enumerate(hosts):
        if not isinstance(host, dict):
            raise ClusterConfigError(f"Host entry {i} must be a dictionary")
        if 'name' not in host:
            raise ClusterConfigError(f"Host entry {i} must have 'name' field")


def get_app_port() -> int:
    """Get the configured app port from settings"""
    from ydb.tests.stability.nemesis_app.internal.config import Settings
    return Settings().app_port


def upload_binary(host, is_orchestrator=False, yaml_config_location=None, port=31434):
    print(f'Uploading binary to {host}')
    ssh_base = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-A']
    ssh_rsh = " ".join(ssh_base)

    subprocess.check_call(["rsync", "-avqLW", "--del", "--no-o", "--no-g",
                           "--rsh={}".format(ssh_rsh),
                           "--rsync-path=sudo rsync", "--progress", './nemesis_app', f'{host}:/Berkanavt/nemesis/bin/agent'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Upload static files and config for orchestrator
    if is_orchestrator:
        print(f'Uploading static files to {host}')
        subprocess.check_call(["rsync", "-avqLW", "--del", "--no-o", "--no-g",
                               "--rsh={}".format(ssh_rsh),
                               "--rsync-path=sudo rsync", "--progress", './static/', f'{host}:/Berkanavt/nemesis/static/'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if yaml_config_location:
            print(f'Uploading cluster config to {host}')
            subprocess.check_call(["rsync", "-avqLW", "--no-o", "--no-g",
                                   "--rsh={}".format(ssh_rsh),
                                   "--rsync-path=sudo rsync", "--progress", yaml_config_location, f'{host}:/Berkanavt/nemesis/cluster.yaml'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f'Uploading service to {host}')
    subprocess.check_call(["rsync", "-avqLW", "--no-o", "--no-g",
                           "--rsh={}".format(ssh_rsh),
                           "--rsync-path=sudo rsync", "--progress", f'./nemesis-agent.service.{host}',
                           f'{host}:/etc/systemd/system/nemesis-agent.service'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f'Restarting service on {host}')
    subprocess.check_call(ssh_base + [host, "sudo systemctl daemon-reload && sudo systemctl enable nemesis-agent && sudo systemctl restart nemesis-agent"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_agent_service(host):
    ssh_base = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-A']

    print(f'Stopping service on {host}')
    subprocess.check_call(ssh_base + [host, "sudo systemctl stop nemesis-agent"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def get_hosts_from_yaml(yaml_path):
    """
    Get list of hosts from cluster.yaml file.

    Args:
        yaml_path: Path to the cluster.yaml file

    Returns:
        List of host names

    Raises:
        ClusterConfigError: If file is invalid
    """
    validate_cluster_yaml(yaml_path)

    with open(yaml_path, 'r') as r:
        yaml_config = yaml.safe_load(r.read())
        hosts = [host.get('name') for host in yaml_config.get('hosts', [])]
        return hosts


def install_on_hosts(hosts, yaml_config_location=None):
    """
    Install services on hosts:
    - First host: orchestrator mode (master)
    - Remaining hosts: agent mode
    """
    if not hosts:
        return

    port = get_app_port()
    orchestrator_host = hosts[0]
    agent_hosts = hosts[1:] if len(hosts) > 1 else []

    # Create service file for orchestrator (first host)
    with open(f"nemesis-agent.service.{orchestrator_host}", "w") as f:
        # Use the copied config location on the orchestrator host
        config_location = "/Berkanavt/nemesis/cluster.yaml" if yaml_config_location else ""
        yaml_config_env = f"Environment=YAML_CONFIG_LOCATION={config_location}\n        " if config_location else ""
        f.write(f"""[Unit]
        Description=Nemesis Orchestrator Service
        After=network-online.target
        Wants=nemesis-autoconf.service
        StartLimitInterval=10
        StartLimitBurst=15

        [Service]
        Restart=always
        RestartSec=10
        Environment=NEMESIS_USER=robot-nemesis
        Environment=NEMESIS_TYPE=master
        Environment=STATIC_LOCATION=/Berkanavt/nemesis/static
        Environment=APP_HOST={orchestrator_host}
        Environment=APP_PORT={port}
        {yaml_config_env}Type=simple
        ExecStart=/Berkanavt/nemesis/bin/agent
        StandardOutput=syslog
        StandardError=syslog
        SyslogIdentifier=nemesis-orchestrator
        SyslogFacility=daemon
        SyslogLevel=err
        LimitNOFILE=65536
        LimitCORE=0
        LimitMEMLOCK=32212254720

        [Install]
        WantedBy=multi-user.target
        """)

    # Create service files for agents (remaining hosts)
    for host in agent_hosts:
        with open(f"nemesis-agent.service.{host}", "w") as f:
            f.write(f"""[Unit]
        Description=Nemesis Agent Service
        After=network-online.target
        Wants=nemesis-autoconf.service
        StartLimitInterval=10
        StartLimitBurst=15

        [Service]
        Restart=always
        RestartSec=10
        Environment=NEMESIS_USER=robot-nemesis
        Environment=NEMESIS_TYPE=agent
        Environment=STATIC_LOCATION=/Berkanavt/nemesis/static
        Environment=APP_HOST={host}
        Environment=APP_PORT={port}
        Type=simple
        ExecStart=/Berkanavt/nemesis/bin/agent
        StandardOutput=syslog
        StandardError=syslog
        SyslogIdentifier=nemesis-agent
        SyslogFacility=daemon
        SyslogLevel=err
        LimitNOFILE=65536
        LimitCORE=0
        LimitMEMLOCK=32212254720

        [Install]
        WantedBy=multi-user.target
        """)

    # Upload binaries to all hosts
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        # Upload to orchestrator with static files and config
        executor.submit(upload_binary, orchestrator_host, True, yaml_config_location, port)

        # Upload to agents (without static files and config)
        for host in agent_hosts:
            executor.submit(upload_binary, host, False, None, port)

    return orchestrator_host


def stop_agent_services(hosts):
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        for host in hosts:
            executor.submit(stop_agent_service, host)

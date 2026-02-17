from concurrent.futures import ThreadPoolExecutor
import subprocess

import yaml


def upload_binary(host, is_orchestrator=False, yaml_config_location=None):
    print(f'Uploading binary to {host}')
    ssh_base = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-A']
    ssh_rsh = " ".join(ssh_base)

    subprocess.check_call(["rsync", "-avqLW", "--del", "--no-o", "--no-g",
                           "--rsh={}".format(ssh_rsh),
                           "--rsync-path=sudo rsync", "--progress", './nemesis_app', f'{host}:/Berkanavt/nemesis/bin/agent'])

    # Upload static files and config for orchestrator
    if is_orchestrator:
        print(f'Uploading static files to {host}')
        subprocess.check_call(["rsync", "-avqLW", "--del", "--no-o", "--no-g",
                               "--rsh={}".format(ssh_rsh),
                               "--rsync-path=sudo rsync", "--progress", './static/', f'{host}:/Berkanavt/nemesis/static/'])

        if yaml_config_location:
            print(f'Uploading cluster config to {host}')
            subprocess.check_call(["rsync", "-avqLW", "--no-o", "--no-g",
                                   "--rsh={}".format(ssh_rsh),
                                   "--rsync-path=sudo rsync", "--progress", yaml_config_location, f'{host}:/Berkanavt/nemesis/cluster.yaml'])

    print(f'Uploading service to {host}')
    subprocess.check_call(["rsync", "-avqLW", "--no-o", "--no-g",
                           "--rsh={}".format(ssh_rsh),
                           "--rsync-path=sudo rsync", "--progress", f'./nemesis-agent.service.{host}', f'{host}:/etc/systemd/system/nemesis-agent.service'])

    print(f'Restarting service on {host}')
    subprocess.check_call(ssh_base + [host, "sudo systemctl daemon-reload && sudo systemctl enable nemesis-agent && sudo systemctl restart nemesis-agent"])


def stop_agent_service(host):
    ssh_base = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-A']

    print(f'Stopping service on {host}')
    subprocess.check_call(ssh_base + [host, "sudo systemctl stop nemesis-agent"])


def get_hosts_from_yaml(yaml_path):
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
        executor.submit(upload_binary, orchestrator_host, True, yaml_config_location)

        # Upload to agents (without static files and config)
        for host in agent_hosts:
            executor.submit(upload_binary, host, False, None)

    return orchestrator_host


def stop_agent_services(hosts):
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        for host in hosts:
            executor.submit(stop_agent_service, host)

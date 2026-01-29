from concurrent.futures import ThreadPoolExecutor
import subprocess

import yaml


def upload_binary(host):
    print(f'Uploading binary to {host}')
    ssh_base = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-A']
    ssh_rsh = " ".join(ssh_base)

    subprocess.check_call(["rsync", "-avqLW", "--del", "--no-o", "--no-g",
                           "--rsh={}".format(ssh_rsh),
                           "--rsync-path=sudo rsync", "--progress", './agent', f'{host}:/Berkanavt/nemesis/bin/'])
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


def install_on_hosts(hosts):
    for host in hosts:
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
        User=kikimr
        Environment=NEMESIS_USER=robot-nemesis
        Environment=NEMESIS_TYPE=agent
        Environment=STATIC_LOCATION=/Berkanavt/nemesis/static
        Environment=APP_HOST={host}
        Type=simple
        ExecStart=/Berkanavt/nemesis/bin/agent
        StandardOutput=syslog
        StandardError=syslog
        SyslogIdentifier=nemesis
        SyslogFacility=daemon
        SyslogLevel=err
        LimitNOFILE=65536
        LimitCORE=0
        LimitMEMLOCK=32212254720

        [Install]
        WantedBy=multi-user.target
        """)
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        for host in hosts:
            executor.submit(upload_binary, host)


def stop_agent_services(hosts):
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        for host in hosts:
            executor.submit(stop_agent_service, host)

import signal

PROCESS_TYPES = {
    "type1": {
        "cmd": "echo 'Type 1 process started'; sleep 1; echo 'Type 1 output'; sleep 1; echo 'Type 1 finished'",
        "schedule": 10
    },
    "type2": {
        "cmd": "echo 'Type 2 process started'; sleep 1; echo 'Type 2 error' >&2; sleep 1; echo 'Type 2 finished'",
        "schedule": 20
    },
    "NodeKiller": {
        "cmd": "ps aux | grep '\\--ic-port' | grep -v grep | awk '{ print $2 }' | tail -n 1 | xargs -r sudo kill -%d" % (
            int(signal.SIGKILL),
        ),
        "schedule": 300
    },
}
import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
SCRIPT = (
    ROOT
    / ".agents/skills/ditto-subnet-release-ops/scripts"
    / "recover_platform_postgres_disk.sh"
)
CONFIRMATION = "GROW PLATFORM POSTGRES BOOT DISK TO 100GB"


def test_recovery_workflow_pins_target_and_separates_authorities() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/platform-postgres-disk-recovery.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {"confirmation"}
    cloud = workflow["jobs"]["grow-cloud-disk"]
    guest = workflow["jobs"]["expand-guest-and-check"]
    assert cloud["environment"] == "infra-apply"
    assert guest["environment"] == "prod"
    assert guest["needs"] == "grow-cloud-disk"
    assert "github.ref == 'refs/heads/main'" in cloud["if"]
    assert CONFIRMATION in cloud["if"]
    commands = "\n".join(step.get("run", "") for step in cloud["steps"])
    assert "int(d['sizeGb']) in (30,100)" in commands
    assert "--size=100GB" in commands
    assert "instances/ditto-pg-platform" in commands
    assert "disks delete" not in commands and "instances reset" not in commands
    source = SCRIPT.read_text()
    assert source.index("findmnt -n -o SOURCE") < source.index("growpart -N")
    assert source.index("resize2fs /dev/sda1") < source.index(
        "pg_ctlcluster --mode fast 17 main restart"
    )
    assert "107374182400" in source
    assert "pg_resetwal" not in source


def test_recovery_refuses_missing_or_wrong_confirmation_before_connecting(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "connected"
    fake = tmp_path / "gcloud"
    fake.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
    fake.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path) + ":" + os.environ["PATH"]}
    for args in [[], ["yes"], [CONFIRMATION, "extra"]]:
        result = subprocess.run(
            ["bash", str(SCRIPT), *args], env=env, capture_output=True, check=False
        )
        assert result.returncode == 2
        assert not marker.exists()


def test_guest_growth_guards_and_conditional_restart(tmp_path: Path) -> None:
    import json
    import sys

    tools = tmp_path / "bin"
    tools.mkdir()
    mock = tools / "mock"
    mock.write_text(
        f"#!{sys.executable}\n"
        + r"""
import json,os,subprocess,sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
log=Path(os.environ['CALL_LOG'])
with log.open('a') as f: f.write(json.dumps([name,*args])+'\n')
if name=='gcloud':
    command=next(a.split('=',1)[1] for a in args if a.startswith('--command='))
    sys.exit(subprocess.run(['bash','-c',command]).returncode)
if name=='findmnt':
    print(os.environ.get('ROOT_SOURCE','/dev/sda1') if 'SOURCE' in args else 'ext4')
elif name=='sudo':
    args=args[1:]
    if args[0]=='blockdev': print(os.environ.get('DISK_BYTES','107374182400'))
    elif args[0]=='pg_lsclusters':
        print('17 main 5432 online postgres '
              '/var/lib/postgresql/17/main /var/log/postgresql.log')
    elif args[0]=='growpart': print('CHANGED: partition=1')
    elif args[0]=='resize2fs': pass
    elif args[0]=='pg_ctlcluster': Path(os.environ['RESTARTED']).touch()
    else: sys.exit(99)
elif name=='df':
    print('Avail\n'+os.environ.get('FREE_BYTES','60000000000'))
elif name=='pg_isready':
    ready=os.environ.get('READY')=='1' or Path(os.environ['RESTARTED']).exists()
    sys.exit(0 if ready else 1)
elif name in ['sleep','lsblk']: pass
else: sys.exit(99)
"""
    )
    mock.chmod(0o755)
    for name in [
        "gcloud",
        "findmnt",
        "sudo",
        "df",
        "pg_isready",
        "sleep",
        "lsblk",
        "growpart",
    ]:
        (tools / name).symlink_to(mock)
    cases = [
        ({"READY": "1"}, True, False, True),
        ({"READY": "0"}, True, True, True),
        ({"ROOT_SOURCE": "/dev/sdb1"}, False, False, False),
        ({"DISK_BYTES": "32212254720"}, False, False, False),
        ({"FREE_BYTES": "1000000"}, False, False, True),
    ]
    for i, (changes, success, restarted, expanded) in enumerate(cases):
        log = tmp_path / f"calls-{i}"
        restart = tmp_path / f"restarted-{i}"
        env = {
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "RESTARTED": str(restart),
            **changes,
        }
        result = subprocess.run(
            ["bash", str(SCRIPT), CONFIRMATION],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        calls = [json.loads(row) for row in log.read_text().splitlines()]
        assert (result.returncode == 0) == success, (changes, result.stderr)
        assert restart.exists() == restarted
        assert any("resize2fs" in call for call in calls) == expanded

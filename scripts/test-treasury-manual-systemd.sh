#!/usr/bin/env bash
# Hosted disposable runner only: harmless units, no signer, no production names.
set -euo pipefail
[[ -d /run/systemd/system ]] || { echo 'A systemd hosted runner is required' >&2; exit 1; }
task_root=$(mktemp -d)
task_prefix="sn118-manual-ci-${GITHUB_RUN_ID:-$$}-${GITHUB_RUN_ATTEMPT:-0}"
[[ "$task_prefix" =~ ^sn118-manual-ci-[0-9]+-[0-9]+$ ]]
cleanup() {
  sudo systemctl stop "$task_prefix-manual.service" "$task_prefix-attempt.timer" "$task_prefix-transfer.service" "$task_prefix-transfer.timer" || true
  sudo rm -f "/run/systemd/system/$task_prefix-manual.service" "/run/systemd/system/$task_prefix-transfer.service" "/run/systemd/system/$task_prefix-transfer.timer" "/run/systemd/system/$task_prefix-attempt.timer"
  sudo rm -f "/etc/systemd/system/$task_prefix-transfer.service" "/etc/systemd/system/$task_prefix-transfer.timer"
  sudo systemctl daemon-reload
  rm -rf "$task_root"
}
trap cleanup EXIT
export task_root task_prefix
python3 - <<'PY'
import os
from pathlib import Path
root, prefix = Path(os.environ['task_root']), os.environ['task_prefix']
manual = Path('infra/systemd/sn118-treasury-manual.service').read_text()
unit = manual.split('[Service]')[0]
unit = unit.replace('sn118-collector@transfer', prefix + '-transfer')
unit = unit.replace('/etc/sn118-collector/manual/activation.env', str(root / 'manual-enabled'))
(root / 'manual-enabled').touch()
guard = Path('scripts/check-treasury-manual-mode.sh').read_text().replace('sn118-collector@transfer', prefix + '-transfer')
(root / 'guard.sh').write_text(guard)
(root / 'guard.sh').chmod(0o755)
# Keep the actual reviewed relationships/condition, replace signing with sleep.
(root / (prefix + '-manual.service')).write_text(unit + f'[Service]\nType=simple\nExecStartPre={root}/guard.sh\nExecStart=/bin/sleep 120\n')
(root / (prefix + '-transfer.service')).write_text(f'[Unit]\nDescription=Harmless transfer probe\n[Service]\nType=simple\nExecStart=/bin/sleep 120\nExecStartPost=/usr/bin/touch {root}/transfer-executed\n')
timer = Path('infra/systemd/sn118-collector@.timer').read_text().replace('sn118-collector@%i.service', prefix + '-transfer.service')
(root / (prefix + '-transfer.timer')).write_text(timer)
# Another timer attempts to activate the masked service; systemd may refuse
# its dependency at start or refuse the service when the timer fires.
(root / (prefix + '-attempt.timer')).write_text(f'[Timer]\nOnActiveSec=100ms\nAccuracySec=1ms\nUnit={prefix}-transfer.service\n')
PY
for suffix in manual.service transfer.service transfer.timer attempt.timer; do
  sudo cp "$task_root/$task_prefix-$suffix" /run/systemd/system/
done
sudo systemctl daemon-reload
if bash "$task_root/guard.sh"; then echo 'Unmasked units passed the guard' >&2; exit 1; fi
# Rejected manual activation must leave both existing recurring units running.
sudo systemctl start "$task_prefix-transfer.service" "$task_prefix-transfer.timer"
if sudo systemctl start "$task_prefix-manual.service"; then echo 'Unmasked manual activation succeeded' >&2; exit 1; fi
sudo systemctl is-active --quiet "$task_prefix-transfer.service"
sudo systemctl is-active --quiet "$task_prefix-transfer.timer"
sudo systemctl stop "$task_prefix-transfer.service" "$task_prefix-transfer.timer"
sudo systemctl reset-failed "$task_prefix-manual.service"
rm -f "$task_root/transfer-executed"
# Remove the CI probe definitions before masking their names (production
# instances inherit a template rather than overriding unit files).
sudo rm -f "/run/systemd/system/$task_prefix-transfer.service" "/run/systemd/system/$task_prefix-transfer.timer"
sudo systemctl daemon-reload
# Same lifecycle as documented production activation, scoped to CI-only names.
sudo systemctl mask --now "$task_prefix-transfer.service" "$task_prefix-transfer.timer"
sudo systemctl start "$task_prefix-manual.service"
sudo systemctl is-active --quiet "$task_prefix-manual.service"
for suffix in transfer.timer transfer.service; do
  if sudo systemctl start "$task_prefix-$suffix"; then echo 'Masked recurring activation succeeded' >&2; exit 1; fi
  sudo systemctl is-active --quiet "$task_prefix-manual.service"
done
if sudo systemctl start "$task_prefix-attempt.timer"; then
  sleep 1
fi
sudo systemctl is-active --quiet "$task_prefix-manual.service"
[[ ! -e "$task_root/transfer-executed" ]]
printf '%s\n' 'Manual consumer survived both masked starts and attempted timer activation; no transfer executed.'

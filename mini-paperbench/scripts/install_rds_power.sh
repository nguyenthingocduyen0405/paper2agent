#!/usr/bin/env bash
# EC2에 paperagent-rds 서비스를 설치한다: EC2가 켜지면 RDS를 켜고, 꺼질 때(Learner Lab 세션 종료 포함) RDS를 끈다.
# 사용(EC2, mini-paperbench 폴더에서): sudo DB_ID=paperagent-db REGION=us-east-1 scripts/install_rds_power.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DB_ID=${DB_ID:-paperagent-db}
REGION=${REGION:-us-east-1}
VENV=/opt/paperagent-aws

if [ ! -x "$VENV/bin/python" ]; then
  apt-get install -y python3-venv >/dev/null
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q "boto3==1.35.36"
install -m 0755 scripts/rds_power.py "$VENV/rds_power.py"
printf 'DB_ID=%s\nREGION=%s\n' "$DB_ID" "$REGION" > /etc/paperagent-rds.env

cat > /etc/systemd/system/paperagent-rds.service <<'UNIT'
[Unit]
Description=PaperAgent: start RDS with this EC2, stop it when the EC2 shuts down
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
EnvironmentFile=/etc/paperagent-rds.env
ExecStart=/opt/paperagent-aws/bin/python /opt/paperagent-aws/rds_power.py start
ExecStop=/opt/paperagent-aws/bin/python /opt/paperagent-aws/rds_power.py stop
TimeoutStartSec=90
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now paperagent-rds.service
systemctl --no-pager status paperagent-rds.service | head -12

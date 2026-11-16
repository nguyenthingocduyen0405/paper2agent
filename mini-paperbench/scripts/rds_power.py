"""RDS를 EC2와 함께 켜고 끈다(systemd 서비스 paperagent-rds가 부른다).

Learner Lab은 세션이 끝나면 EC2는 끄지만 RDS는 끄지 않는다. 그대로 두면 아무도 안 쓰는 동안에도 크레딧이 나간다.
  start: EC2가 켜질 때. RDS가 꺼져 있으면 켠다.
  stop:  EC2가 꺼질 때. RDS가 켜져 있으면 끈다.
권한은 EC2에 붙인 LabRole(메타데이터 서비스)에서 온다. 사용: python rds_power.py start|stop|status
"""
import os
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

DB_ID = os.environ.get("DB_ID", "paperagent-db")
REGION = os.environ.get("REGION", "us-east-1")


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    rds = boto3.client("rds", region_name=REGION, config=Config(retries={"max_attempts": 5, "mode": "standard"},
                                                                connect_timeout=5, read_timeout=20))
    try:
        status = rds.describe_db_instances(DBInstanceIdentifier=DB_ID)["DBInstances"][0]["DBInstanceStatus"]
    except ClientError as e:
        print(f"{DB_ID}: cannot read status: {e}", flush=True)
        return 0  # EC2 시작·종료를 막지 않는다
    print(f"{DB_ID}: {status}", flush=True)
    try:
        if action == "start" and status == "stopped":
            rds.start_db_instance(DBInstanceIdentifier=DB_ID)
            print(f"{DB_ID}: starting", flush=True)
        elif action == "stop" and status == "available":
            rds.stop_db_instance(DBInstanceIdentifier=DB_ID)
            print(f"{DB_ID}: stopping", flush=True)
    except ClientError as e:
        print(f"{DB_ID}: {action} failed: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

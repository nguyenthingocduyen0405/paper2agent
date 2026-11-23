#!/usr/bin/env bash
# AWS 쪽 준비: S3 버킷, RDS PostgreSQL, EC2 권한. 내 PC에서 AWS CLI로 실행한다(여러 번 실행해도 안전하다).
# 준비: Learner Lab의 AWS Details → AWS CLI 값을 ~/.aws/credentials에 넣는다(세션마다 바뀐다).
# 사용(Git Bash): AWS="/c/Program Files/Amazon/AWSCLIV2/aws.exe" scripts/aws_setup.sh <EC2 IP>
#
# 1 EC2에 LabInstanceProfile(LabRole) 붙이기, 메타데이터 hop limit 2 (Pod 안의 웹이 S3를 쓰게)
# 2 S3 버킷 paperagent-<계정 번호>: 공개 차단, 기본 암호화
# 3 RDS용 보안 그룹: EC2 보안 그룹에서 오는 5432만 연다
# 4 RDS PostgreSQL db.t4g.micro, gp2 20GB, 공개 안 함, 백업 1일
# 5 EC2의 .env에 S3_BUCKET, DATABASE_URL, ADMIN_USERNAME/ADMIN_PASSWORD, SERVICE_TOKEN을 넣는다
#   비밀번호는 화면에 찍지 않는다. 첫 관리자 비밀번호만 내 PC의 ~/.paperagent/admin.txt에 남긴다.
# 6 EC2에 paperagent-rds 서비스: EC2가 켜지면 RDS를 켜고, 꺼질 때 RDS도 끈다(Learner Lab은 RDS를 끄지 않는다)
set -euo pipefail
cd "$(dirname "$0")/.."

IP=${1:?usage: scripts/aws_setup.sh <EC2 IP>}
AWS=${AWS:-aws}
REGION=us-east-1
INSTANCE=${INSTANCE:-i-0a36b0a7447859c42}
KEY=${KEY:-$HOME/paperagent.pem}
REPO_ON_EC2=${REPO_ON_EC2:-paper2agent/mini-paperbench}
DB_ID=paperagent-db
DB_NAME=paperagent
DB_USER=paperagent
ADMIN_USER=${ADMIN_USER:-admin}
CR=$(printf '\r')
aws_() { "$AWS" --region "$REGION" "$@" | tr -d "$CR"; }   # Windows AWS CLI는 CRLF로 찍는다
ssh_() { ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "ubuntu@$IP" "$@"; }
step() { echo; echo "=== $* ==="; }

ACCOUNT=$(aws_ sts get-caller-identity --query Account --output text)
BUCKET=paperagent-$ACCOUNT

step "1/6 EC2 instance profile + metadata hop limit"
PROFILE=$(aws_ ec2 describe-instances --instance-ids "$INSTANCE" \
  --query "Reservations[0].Instances[0].IamInstanceProfile.Arn" --output text)
if [ "$PROFILE" = "None" ]; then
  aws_ ec2 associate-iam-instance-profile --instance-id "$INSTANCE" \
    --iam-instance-profile Name=LabInstanceProfile --query "IamInstanceProfileAssociation.State" --output text
else
  echo "skip: profile already attached ($PROFILE)"
fi
aws_ ec2 modify-instance-metadata-options --instance-id "$INSTANCE" --http-endpoint enabled \
  --http-tokens required --http-put-response-hop-limit 2 --query "InstanceMetadataOptions.HttpPutResponseHopLimit" --output text

step "2/6 S3 bucket $BUCKET"
if aws_ s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "skip: bucket exists"
else
  aws_ s3api create-bucket --bucket "$BUCKET" --query Location --output text
fi
aws_ s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws_ s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

step "3/6 security group for RDS"
read -r VPC EC2_SG < <(aws_ ec2 describe-instances --instance-ids "$INSTANCE" \
  --query "Reservations[0].Instances[0].[VpcId,SecurityGroups[?GroupName!='paperagent-web'] | [0].GroupId]" --output text)
DB_SG=$(aws_ ec2 describe-security-groups --filters Name=group-name,Values=paperagent-db Name=vpc-id,Values="$VPC" \
  --query "SecurityGroups[0].GroupId" --output text)
if [ "$DB_SG" = "None" ]; then
  DB_SG=$(aws_ ec2 create-security-group --group-name paperagent-db --vpc-id "$VPC" \
    --description "PaperAgent RDS: PostgreSQL from the PaperAgent EC2 only" --query GroupId --output text)
  aws_ ec2 authorize-security-group-ingress --group-id "$DB_SG" \
    --ip-permissions "IpProtocol=tcp,FromPort=5432,ToPort=5432,UserIdGroupPairs=[{GroupId=$EC2_SG}]" >/dev/null
fi
echo "db security group: $DB_SG (5432 from $EC2_SG)"

step "4/6 RDS PostgreSQL $DB_ID"
NEW_DB=0
STATUS=$(aws_ rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --query "DBInstances[0].DBInstanceStatus" --output text 2>/dev/null || echo missing)
if [ "$STATUS" = "missing" ]; then
  DB_PASSWORD=$(openssl rand -hex 24)
  aws_ rds create-db-instance --db-instance-identifier "$DB_ID" --engine postgres \
    --db-instance-class db.t4g.micro --allocated-storage 20 --storage-type gp2 \
    --db-name "$DB_NAME" --master-username "$DB_USER" --master-user-password "$DB_PASSWORD" \
    --vpc-security-group-ids "$DB_SG" --no-publicly-accessible --no-multi-az \
    --backup-retention-period 1 --monitoring-interval 0 --no-enable-performance-insights \
    --storage-encrypted --tags Key=project,Value=paperagent \
    --query "DBInstance.DBInstanceStatus" --output text
  NEW_DB=1
elif [ "$STATUS" = "stopped" ]; then
  aws_ rds start-db-instance --db-instance-identifier "$DB_ID" --query "DBInstance.DBInstanceStatus" --output text
fi
echo "waiting for RDS to be available (new: 5~15 min) ..."
aws_ rds wait db-instance-available --db-instance-identifier "$DB_ID"
ENDPOINT=$(aws_ rds describe-db-instances --db-instance-identifier "$DB_ID" \
  --query "DBInstances[0].Endpoint.Address" --output text)
echo "endpoint: $ENDPOINT"
if [ "$NEW_DB" = 0 ] && ! ssh_ "grep -q '^DATABASE_URL=.' $REPO_ON_EC2/.env"; then
  # DB는 있는데 EC2 .env에 주소가 없다(앞선 실행이 중간에 끊긴 경우): 비밀번호를 새로 정한다
  DB_PASSWORD=$(openssl rand -hex 24)
  aws_ rds modify-db-instance --db-instance-identifier "$DB_ID" --master-user-password "$DB_PASSWORD"     --apply-immediately --query "DBInstance.DBInstanceStatus" --output text
  sleep 20
  aws_ rds wait db-instance-available --db-instance-identifier "$DB_ID"
  NEW_DB=1
fi

step "5/6 EC2 .env (values are not printed)"
mkdir -p "$HOME/.paperagent"
VALUES="S3_BUCKET=$BUCKET"$'\n'
if [ "$NEW_DB" = 1 ]; then
  VALUES+="DATABASE_URL=postgresql+psycopg://$DB_USER:$DB_PASSWORD@$ENDPOINT:5432/$DB_NAME?sslmode=require"$'\n'
fi
if ! ssh_ "grep -q '^ADMIN_PASSWORD=.' $REPO_ON_EC2/.env"; then
  ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)
  VALUES+="ADMIN_USERNAME=$ADMIN_USER"$'\n'"ADMIN_PASSWORD=$ADMIN_PASSWORD"$'\n'
  (umask 077; printf 'PaperAgent web admin\nid: %s\npassword: %s\n(log in, then change it on the 내 계정 page)\n' \
    "$ADMIN_USER" "$ADMIN_PASSWORD" > "$HOME/.paperagent/admin.txt")
  echo "first admin password saved to ~/.paperagent/admin.txt on this PC"
fi
if ! ssh_ "grep -q '^SERVICE_TOKEN=.' $REPO_ON_EC2/.env"; then
  VALUES+="SERVICE_TOKEN=$(openssl rand -hex 32)"$'\n'
fi
printf '%s' "$VALUES" | ssh_ "cd $REPO_ON_EC2 && python3 scripts/env_update.py"
unset DB_PASSWORD ADMIN_PASSWORD VALUES

step "6/6 RDS follows the EC2 (start on boot, stop on shutdown)"
ssh_ "cd $REPO_ON_EC2 && sudo DB_ID=$DB_ID REGION=$REGION scripts/install_rds_power.sh"
echo
echo "done. next on the EC2: cd $REPO_ON_EC2 && scripts/bootstrap.sh"

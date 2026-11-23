#!/usr/bin/env bash
# EC2에 고정 공개 주소(Elastic IP)와 HTTPS 주소를 붙인다. 내 PC에서 AWS CLI로 실행한다(여러 번 실행해도 안전하다).
# 준비: Learner Lab의 AWS Details → AWS CLI 값을 ~/.aws/credentials에 넣고, EC2가 켜져 있어야 한다.
# 사용(Git Bash): AWS="/c/Program Files/Amazon/AWSCLIV2/aws.exe" scripts/aws_public_ip.sh
#
# 1 Elastic IP: project=paperagent 태그가 붙은 것을 쓰고, 없으면 새로 받는다. EC2에 붙인다.
#   랩 세션이 끝나 EC2가 꺼졌다 켜져도 주소가 그대로다(그 대신 EC2가 꺼져 있어도 시간당 요금이 나간다).
# 2 보안 그룹 paperagent-web: 80, 443번을 모두에게 연다(인증서 발급과 웹). EC2에 기존 그룹과 함께 붙인다.
# 3 EC2의 .env에 PUBLIC_URL=https://<IP의 점을 -로>.sslip.io 를 넣는다.
#   공개 저장소이므로 주소는 .env에만 두고 커밋하지 않는다.
# 다음: EC2에서 scripts/bootstrap.sh → Caddy(k8s/57-caddy.yaml)가 인증서를 받는다.
#
# 되돌리기(주소를 놓을 때): aws ec2 disassociate-address --association-id <id>
#                          aws ec2 release-address --allocation-id <id>
#                          그리고 EC2 .env에서 PUBLIC_URL을 지우고 bootstrap.sh
set -euo pipefail
cd "$(dirname "$0")/.."

AWS=${AWS:-aws}
REGION=us-east-1
INSTANCE=${INSTANCE:-i-0a36b0a7447859c42}
KEY=${KEY:-$HOME/paperagent.pem}
REPO_ON_EC2=${REPO_ON_EC2:-paper2agent/mini-paperbench}
WEB_SG_NAME=paperagent-web
CR=$(printf '\r')
aws_() { "$AWS" --region "$REGION" "$@" | tr -d "$CR"; }   # Windows AWS CLI는 CRLF로 찍는다
step() { echo; echo "=== $* ==="; }

STATE=$(aws_ ec2 describe-instances --instance-ids "$INSTANCE" --query "Reservations[0].Instances[0].State.Name" --output text)
[ "$STATE" = running ] || { echo "EC2 is $STATE. Start the lab (and the instance) first."; exit 1; }

step "1/3 Elastic IP"
read -r ALLOC EIP ASSOC_INSTANCE < <(aws_ ec2 describe-addresses --filters Name=tag:project,Values=paperagent \
  --query "Addresses[0].[AllocationId,PublicIp,InstanceId]" --output text)
if [ "$ALLOC" = "None" ] || [ -z "$ALLOC" ]; then
  read -r ALLOC EIP < <(aws_ ec2 allocate-address --domain vpc \
    --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=project,Value=paperagent},{Key=Name,Value=paperagent-web}]" \
    --query "[AllocationId,PublicIp]" --output text)
  ASSOC_INSTANCE=None
  echo "allocated $EIP ($ALLOC)"
fi
if [ "$ASSOC_INSTANCE" = "$INSTANCE" ]; then
  echo "skip: $EIP already on $INSTANCE"
else
  aws_ ec2 associate-address --instance-id "$INSTANCE" --allocation-id "$ALLOC" --query AssociationId --output text
  echo "attached $EIP to $INSTANCE"
fi

step "2/3 security group $WEB_SG_NAME (80, 443 from anywhere)"
VPC=$(aws_ ec2 describe-instances --instance-ids "$INSTANCE" --query "Reservations[0].Instances[0].VpcId" --output text)
WEB_SG=$(aws_ ec2 describe-security-groups --filters Name=group-name,Values="$WEB_SG_NAME" Name=vpc-id,Values="$VPC" \
  --query "SecurityGroups[0].GroupId" --output text)
if [ "$WEB_SG" = "None" ]; then
  WEB_SG=$(aws_ ec2 create-security-group --group-name "$WEB_SG_NAME" --vpc-id "$VPC" \
    --description "PaperAgent web: HTTP and HTTPS from anywhere (Caddy)" --query GroupId --output text)
  aws_ ec2 authorize-security-group-ingress --group-id "$WEB_SG" --ip-permissions \
    "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0,Description=http-and-acme}]" \
    "IpProtocol=tcp,FromPort=443,ToPort=443,IpRanges=[{CidrIp=0.0.0.0/0,Description=https}]" >/dev/null
fi
SGS=$(aws_ ec2 describe-instances --instance-ids "$INSTANCE" \
  --query "Reservations[0].Instances[0].SecurityGroups[].GroupId" --output text)
case " $SGS " in
  *" $WEB_SG "*) echo "skip: $WEB_SG already on the instance" ;;
  *) # shellcheck disable=SC2086
     aws_ ec2 modify-instance-attribute --instance-id "$INSTANCE" --groups $SGS "$WEB_SG"
     echo "instance groups: $SGS $WEB_SG" ;;
esac

step "3/3 EC2 .env PUBLIC_URL"
HOST="${EIP//./-}.sslip.io"
URL="https://$HOST"
ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "ubuntu@$EIP" \
  "cd $REPO_ON_EC2 && printf 'PUBLIC_URL=%s\n' '$URL' | python3 scripts/env_update.py"
echo
echo "public address: $URL"
echo "ssh from now on: ssh -i $KEY ubuntu@$EIP"
echo "next on the EC2: cd $REPO_ON_EC2 && scripts/bootstrap.sh"

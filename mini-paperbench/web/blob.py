"""S3: 논문 원본과 끝난 작업의 결과를 EC2 디스크 밖에 보관한다.

S3_BUCKET이 비어 있으면 아무것도 하지 않는다(로컬 시험). 자격 증명은 EC2에 붙인 LabRole(instance profile)을
boto3가 메타데이터 서비스에서 알아서 가져온다. 키를 파일이나 환경 변수로 넣지 않는다.

  papers/<fingerprint>/paper.pdf | paper.md   올린 논문 원본, 준비한 본문
  jobs/<id>/job.json                          작업 기록(DB 행의 사본)
  jobs/<id>/paperagent-<id>.zip               코드 + 보고서 + 채점 기준 (내려받기와 같은 묶음)
  jobs/<id>/results.json                      항목별 검증 결과
  jobs/<id>/paper/...                         이 작업이 쓴 논문 본문, 채점 기준, meta.json
  jobs/<id>/logs/...                          단계별 로그, LLM 호출 기록
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("blob")
_client = None


def bucket() -> str:
    return os.environ.get("S3_BUCKET", "").strip()


def enabled() -> bool:
    return bool(bucket())


def client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                               config=Config(retries={"max_attempts": 3, "mode": "standard"},
                                             connect_timeout=5, read_timeout=60))
    return _client


def put_file(path: Path, key: str, content_type: str | None = None) -> None:
    extra = {"ContentType": content_type} if content_type else {}
    client().upload_file(str(path), bucket(), key, ExtraArgs=extra or None)


def put_bytes(data: bytes, key: str, content_type: str = "application/octet-stream") -> None:
    client().put_object(Bucket=bucket(), Key=key, Body=data, ContentType=content_type)


def get_file(key: str, path: Path) -> bool:
    """S3에서 내려받는다. 없으면 False."""
    from botocore.exceptions import ClientError
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        client().download_file(bucket(), key, str(tmp))
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    os.replace(tmp, path)
    return True


def exists(key: str) -> bool:
    from botocore.exceptions import ClientError
    try:
        client().head_object(Bucket=bucket(), Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def delete_prefix(prefix: str) -> int:
    """prefix로 시작하는 객체를 모두 지운다. 지운 수를 돌려준다."""
    n = 0
    for page in client().get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            client().delete_objects(Bucket=bucket(), Delete={"Objects": keys, "Quiet": True})
            n += len(keys)
    return n


def presign(key: str, filename: str, expires: int = 900) -> str:
    """브라우저가 S3에서 바로 내려받는 주소(15분 동안만 쓸 수 있다)."""
    return client().generate_presigned_url(
        "get_object", ExpiresIn=expires,
        Params={"Bucket": bucket(), "Key": key, "ResponseContentDisposition": f'attachment; filename="{filename}"'})

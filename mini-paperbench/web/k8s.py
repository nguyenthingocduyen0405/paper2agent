"""쿠버네티스 API: Job 만들기, 상태와 로그 읽기, judge 워커에게 run 맡기기.

Job은 k8s/jobs/의 템플릿을 그대로 쓴다(scripts/k8s_run.sh와 같은 방식).
권한은 k8s/40-web.yaml의 Role(web)에 있다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

NAMESPACE = os.environ.get("NAMESPACE", "paperbench")
IMAGE_TAG = os.environ.get("IMAGE_TAG", "latest")
TEMPLATES = Path(__file__).resolve().parent.parent / "k8s" / "jobs"
# Pod가 이 이유로 기다리면 저절로 풀리지 않는다. 실패로 본다.
STUCK_REASONS = {"ErrImageNeverPull", "ErrImagePull", "ImagePullBackOff", "InvalidImageName",
                 "CreateContainerConfigError", "CreateContainerError"}

_apis = None


def _api():
    global _apis
    if _apis is None:
        config.load_incluster_config()
        _apis = (client.BatchV1Api(), client.CoreV1Api(), client.AppsV1Api())
    return _apis


def job_name(prefix: str, run: str) -> str:
    return f"{prefix}-{run}".lower().replace("_", "-")[:63]


def create_job(template: str, name: str, args: list[str]) -> None:
    text = (TEMPLATES / f"{template}.yaml").read_text()
    text = (text.replace("__NAME__", name).replace("__TAG__", IMAGE_TAG)
            .replace("__ARGS__", json.dumps(args, ensure_ascii=False)))
    batch, _, _ = _api()
    try:
        batch.create_namespaced_job(NAMESPACE, yaml.safe_load(text))
    except ApiException as e:
        if e.status != 409:  # 이미 있으면 그대로 쓴다(같은 단계를 다시 불러도 안전하다)
            raise


def job_state(name: str) -> tuple[str, str]:
    """("missing" | "running" | "succeeded" | "failed", 설명)"""
    batch, core, _ = _api()
    try:
        job = batch.read_namespaced_job(name, NAMESPACE)
    except ApiException as e:
        if e.status == 404:
            return "missing", ""
        raise
    for c in job.status.conditions or []:
        if c.status == "True" and c.type == "Complete":
            return "succeeded", ""
        if c.status == "True" and c.type == "Failed":
            return "failed", c.message or c.reason or ""
    for pod in core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={name}").items:
        for cs in pod.status.container_statuses or []:
            waiting = cs.state.waiting if cs.state else None
            if waiting and waiting.reason in STUCK_REASONS:
                return "failed", f"{waiting.reason}: {waiting.message or ''}".strip()
    return "running", ""


def job_logs(name: str, tail: int = 40) -> str:
    _, core, _ = _api()
    pods = core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={name}").items
    if not pods:
        return ""
    pod = max(pods, key=lambda p: p.metadata.creation_timestamp)
    try:
        return core.read_namespaced_pod_log(pod.metadata.name, NAMESPACE, tail_lines=tail) or ""
    except ApiException:
        return ""


def worker_run() -> tuple[str | None, str | None]:
    """judge 워커가 지금 맡은 (RUN_ID, MOCK_JUDGE)."""
    _, _, apps = _api()
    dep = apps.read_namespaced_deployment("judge-worker", NAMESPACE)
    container = next(c for c in dep.spec.template.spec.containers if c.name == "worker")
    env = {e.name: e.value for e in (container.env or [])}
    return env.get("RUN_ID"), env.get("MOCK_JUDGE")


def assign_workers(run: str, mock: bool) -> None:
    """judge 워커에게 이 run을 맡긴다. 환경 변수가 바뀌므로 워커 Pod가 새로 뜬다(k8s_run.sh의 set env와 같다)."""
    _, _, apps = _api()
    for attempt in range(3):
        dep = apps.read_namespaced_deployment("judge-worker", NAMESPACE)
        container = next(c for c in dep.spec.template.spec.containers if c.name == "worker")
        env = [e for e in (container.env or []) if e.name not in ("RUN_ID", "MOCK_JUDGE")]
        env += [client.V1EnvVar(name="RUN_ID", value=run),
                client.V1EnvVar(name="MOCK_JUDGE", value="1" if mock else "0")]
        container.env = env
        try:
            apps.replace_namespaced_deployment("judge-worker", NAMESPACE, dep)
            return
        except ApiException as e:
            if e.status != 409 or attempt == 2:  # 409: 그사이 누가 바꿨다. 다시 읽고 시도한다
                raise

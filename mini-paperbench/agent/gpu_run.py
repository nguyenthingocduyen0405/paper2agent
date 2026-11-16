"""GPU 실행: 에이전트가 만든 코드를 학교 공용 GPU(JPUShare)에서 reproduce.sh로 실행하고, 오류가 나면 LLM이 고쳐 다시 실행한다.

사용자가 작업 화면에서 실행을 고르면 웹(web/gpu.py)이 쿠버네티스 Job(k8s/jobs/gpu-run.yaml)으로 띄운다.
  1 준비  제출물을 gpu/code/에 복사한다. requirements.txt에서 JPUShare에 이미 있는 패키지(torch, numpy 등)를 빼고
          나머지를 Python 3.12 linux용 wheel로 받는다. JPUShare 작업 안에서는 인터넷을 쓸 수 없기 때문이다.
  2 실행  코드 + wheel + 실행 스크립트(.paperagent/run.sh)를 ZIP으로 묶어 제출하고 끝날 때까지 기다린다.
  3 수정  오류로 끝나면 로그와 관련 파일을 LLM에게 주고 고친 파일을 받아 다시 실행한다(최대 --max-fixes번).
          시간 제한에 걸렸거나 인터넷이 없어 데이터를 못 받은 경우는 고치지 않는다(실험을 바꾸게 되므로).
  4 정리  마지막 실행의 reproduce.log와 결과 파일을 코드와 함께 gpu/judged/에 모은다. 웹이 이 폴더로 실행·결과 항목을 검증한다.
진행 상황은 gpu/status.json, 결과는 gpu/result.json에 남는다. 웹이 gpu/cancel 파일을 만들면 JPUShare 작업을 취소한다.
API 키(JPUSHARE_API_KEY)는 이 Job에만 들어간다(Secret jpushare).

사용: python -m agent.gpu_run --run-id <작업 id> --submission-dir data/agent_runs/<id>/submission [--minutes 30]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

from pydantic import BaseModel, Field

from agent.llm import LLM

JPU_URL = os.environ.get("JPUSHARE_URL", "https://share.jedutools.io").rstrip("/")
POLL_SEC = 15
# JPUShare 실행 환경에 이미 있는 패키지(2026-11 확인: Python 3.12, torch 2.14.1+cu126, torchvision 0.29.1, numpy 2.5.3)와
# torch·requests가 끌어오는 의존성. 이것들은 wheel을 받지 않는다(같은 이름을 다시 깔면 CUDA용 torch가 바뀔 수 있다).
PREINSTALLED = {
    "torch", "torchvision", "torchaudio", "numpy", "pillow", "pyyaml", "requests", "filelock", "typing-extensions",
    "sympy", "mpmath", "networkx", "jinja2", "markupsafe", "fsspec", "setuptools", "triton", "idna", "urllib3",
    "certifi", "charset-normalizer", "pip", "wheel",
}
CONSTRAINTS = "torch==2.14.1\ntorchvision==0.29.1\nnumpy==2.5.3\n"
MAX_WHEELS_BYTES = 420 * 1024 * 1024        # 코드 ZIP 한도는 500 MiB
MAX_CODE_FILE = 50 * 1024 * 1024
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".ipynb_checkpoints", "wandb"}
TERMINAL = {"SUCCEEDED", "FAILED_RUNTIME", "FAILED_ENV", "FAILED_OOM_CONFIRMED", "FAILED_OOM_SUSPECT", "TIMED_OUT",
            "CANCELLED", "KILLED_BY_ADMIN", "FAILED_SUBMISSION", "UPLOAD_FAILED"}
FIXABLE = {"FAILED_RUNTIME", "FAILED_OOM_CONFIRMED", "FAILED_OOM_SUSPECT"}
LOG_HEAD_LINES, LOG_TAIL_LINES, LOG_MAX_CHARS = 200, 800, 80_000
TEXT_OUTPUT_MAX = 100_000


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def is_preinstalled(name: str) -> bool:
    n = norm(name)
    return n in PREINSTALLED or n.startswith("nvidia-") or n.startswith("cuda-")


# ---------------------------------------------------------------- JPUShare API

class JPU:
    def __init__(self, key: str):
        self.key = key

    def _call(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
              raw: bool = False, tries: int = 5):
        h = {"Authorization": f"Bearer {self.key}", **(headers or {})}
        for i in range(tries):
            req = urllib.request.Request(JPU_URL + path, data=body, method=method, headers=h)
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = r.read()
                    return data if raw else json.loads(data or b"null")
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:500]
                if e.code < 500 or i == tries - 1:
                    raise RuntimeError(f"JPUShare {method} {path}: HTTP {e.code} {text}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if i == tries - 1:
                    raise RuntimeError(f"JPUShare {method} {path}: {e}") from None
            time.sleep(min(60, 5 * 2 ** i))

    def submit(self, zip_bytes: bytes, command: str, name: str, description: str, tier: str, timeout_sec: int,
               env: dict) -> str:
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in (("command", command), ("name", name), ("description", description), ("tier", tier),
                     ("timeout_seconds", str(timeout_sec)), ("work_type", "training"), ("result_location", "private"),
                     ("env", json.dumps(env) if env else None)):
            if v is None:
                continue
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"artifact\"; filename=\"code.zip\"\r\n"
                     f"Content-Type: application/zip\r\n\r\n".encode() + zip_bytes + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        d = self._call("POST", "/v1/jobs", b"".join(parts), {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                       tries=2)
        return d["job_id"] if isinstance(d, dict) and "job_id" in d else d["id"]

    def job(self, job_id: str) -> dict:
        return self._call("GET", f"/v1/jobs/{job_id}")

    def cancel(self, job_id: str) -> None:
        self._call("POST", f"/v1/jobs/{job_id}/cancel", b"", tries=2)

    def results_zip(self, job_id: str) -> bytes:
        return self._call("GET", f"/v1/jobs/{job_id}/results-zip", raw=True)


# ---------------------------------------------------------------- 준비: requirements → wheel

def split_requirements(text: str) -> tuple[list[str], list[str], list[str]]:
    """(받을 줄, 이미 있어 뺀 이름, 받을 수 없는 줄)"""
    keep, dropped, unsupported = [], [], []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-", "git+", "http:", "https:", "file:")) or " @ " in line:
            unsupported.append(line)
            continue
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if not m:
            unsupported.append(line)
            continue
        (dropped if is_preinstalled(m.group(1)) else keep).append(line)
    return keep, dropped, unsupported


def _pip_report(lines: list[str], work: Path, constraints: bool) -> list[dict]:
    req = work / "req.txt"
    req.write_text("\n".join(lines) + "\n")
    report = work / "report.json"
    cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet", "--report", str(report),
           "--only-binary=:all:", "--python-version", "3.12", "--implementation", "cp",
           "--abi", "cp312", "--abi", "abi3", "--abi", "none",
           "--platform", "manylinux_2_28_x86_64", "--platform", "manylinux_2_17_x86_64",
           "--platform", "manylinux2014_x86_64", "--target", str(work / "target"), "-r", str(req)]
    if constraints:
        (work / "c.txt").write_text(CONSTRAINTS)
        cmd += ["-c", str(work / "c.txt")]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-1500:])
    return json.loads(report.read_text(encoding="utf-8")).get("install", [])


def resolve(lines: list[str], log) -> tuple[list[dict], list[str]]:
    """받을 wheel 목록과 받지 못한 줄. 전체가 안 풀리면 줄마다 따로 푼다."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for constraints in (True, False):
            try:
                return _pip_report(lines, work, constraints), []
            except RuntimeError as e:
                log(f"pip resolve ({'with' if constraints else 'without'} constraints) failed: {str(e)[-300:]}")
        items, failed = {}, []
        for line in lines:
            try:
                for it in _pip_report([line], work, False):
                    items[norm(it["metadata"]["name"])] = it
            except RuntimeError:
                failed.append(line)
        return list(items.values()), failed


def fetch_wheels(lines: list[str], cache_root: Path, log) -> tuple[Path | None, list[str], list[str]]:
    """wheel 폴더, 받은 패키지 이름, 받지 못한 줄. 같은 requirements면 캐시를 다시 쓴다."""
    if not lines:
        return None, [], []
    key = hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()[:16]
    dest = cache_root / key
    manifest = dest / "manifest.json"
    if manifest.exists():
        m = json.loads(manifest.read_text(encoding="utf-8"))
        log(f"wheels: cache hit {key} ({len(m['names'])} packages)")
        return dest, m["names"], m["failed"]
    items, failed = resolve(lines, log)
    tmp = cache_root / f".{key}.{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    names, total = [], 0
    for it in items:
        name = it["metadata"]["name"]
        if is_preinstalled(name):
            continue
        info = it.get("download_info") or {}
        url = info.get("url")
        if not url:
            failed.append(name)
            continue
        fname = url.rsplit("/", 1)[-1].split("#", 1)[0].split("?", 1)[0]
        with urllib.request.urlopen(url, timeout=300) as r:
            data = r.read()
        want = ((info.get("archive_info") or {}).get("hashes") or {}).get("sha256")
        if want and hashlib.sha256(data).hexdigest() != want:
            raise RuntimeError(f"hash mismatch for {fname}")
        total += len(data)
        if total > MAX_WHEELS_BYTES:
            raise RuntimeError(f"wheels too large (> {MAX_WHEELS_BYTES >> 20} MB)")
        (tmp / fname).write_bytes(data)
        names.append(name)
    (tmp / "manifest.json").write_text(json.dumps({"names": names, "failed": failed}))
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    log(f"wheels: {len(names)} packages, {total / 1e6:.1f} MB" + (f", not available: {failed}" if failed else ""))
    return dest, names, failed


RUN_SH = r"""#!/usr/bin/env bash
# PaperAgent가 JPUShare에서 reproduce.sh를 실행하는 스크립트(자동 생성). 인터넷 없이 미리 받은 wheel로 패키지를 깐다.
W="$(cd "$(dirname "$0")/.." && pwd)"
cd "$W"
OUT="${JOB_OUTPUT_DIR:-/output}"
mkdir -p "$OUT/files"
export PATH="$W/.paperagent/bin:$HOME/.local/bin:$PATH"
export PIP_NO_INDEX=1 PIP_FIND_LINKS="$W/.paperagent/wheels" PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_BREAK_SYSTEM_PACKAGES=1 PIP_USER=1
export MPLBACKEND=Agg PYTHONUNBUFFERED=1 PAPERAGENT_GPU_RUN=1
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 WANDB_MODE=disabled
{
  echo "[paperagent] start $(date -u +%FT%TZ) on $(hostname)"
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>&1 | head -8
  python3 --version
} 2>&1 | tee "$OUT/env.log"
if compgen -G ".paperagent/wheels/*.whl" > /dev/null; then
  if python3 -m pip install --no-deps .paperagent/wheels/*.whl > "$OUT/pip.log" 2>&1; then
    echo "[paperagent] installed $(ls .paperagent/wheels/*.whl | wc -l) packages from wheels" | tee -a "$OUT/env.log"
  else
    echo "[paperagent] wheel install failed (see pip.log)" | tee -a "$OUT/env.log"
  fi
fi
touch .paperagent/.started
set -o pipefail
timeout --signal=INT --kill-after=60 __RUN_SEC__ bash reproduce.sh 2>&1 | tee "$OUT/reproduce.log"
code=${PIPESTATUS[0]}
if [ "$code" = 124 ]; then
  echo "[paperagent] stopped: time limit of __RUN_MIN__ minutes reached" | tee -a "$OUT/reproduce.log"
fi
echo "[paperagent] reproduce.sh exit code $code" | tee -a "$OUT/reproduce.log"
# 실행하면서 생기거나 바뀐 파일(결과 표, 그림, 로그)을 /output/files로 모은다
find . -type f -newer .paperagent/.started ! -path './.paperagent/*' ! -path '*/__pycache__/*' -size -20M -print0 2>/dev/null \
  | head -z -n 500 | while IFS= read -r -d '' f; do mkdir -p "$OUT/files/$(dirname "$f")"; cp -p "$f" "$OUT/files/$f"; done
exit $code
"""


def pack(code: Path, wheels: Path | None, wheel_names: list[str], run_sec: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(code.rglob("*")):
            rel = p.relative_to(code)
            if not p.is_file() or any(part in SKIP_DIRS for part in rel.parts) or p.stat().st_size > MAX_CODE_FILE:
                continue
            if rel.as_posix() == "requirements.txt":
                continue
            z.write(p, rel.as_posix())
        # reproduce.sh가 `pip install -r requirements.txt`를 부르면 미리 받은 wheel만으로 끝나게 한다
        z.writestr("requirements.txt", "".join(f"{n}\n" for n in wheel_names))
        if (code / "requirements.txt").exists():
            z.write(code / "requirements.txt", ".paperagent/requirements.original.txt")
        run_sh = RUN_SH.replace("__RUN_SEC__", str(run_sec)).replace("__RUN_MIN__", str(run_sec // 60))
        for name, body in ((".paperagent/run.sh", run_sh),
                           (".paperagent/bin/pip", '#!/usr/bin/env bash\nexec python3 -m pip "$@"\n'),
                           (".paperagent/bin/pip3", '#!/usr/bin/env bash\nexec python3 -m pip "$@"\n'),
                           (".paperagent/bin/python", '#!/usr/bin/env bash\nexec python3 "$@"\n')):
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o755 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, body)
        if wheels:
            for w in sorted(wheels.glob("*.whl")):
                z.write(w, f".paperagent/wheels/{w.name}", compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


# ---------------------------------------------------------------- 수정(LLM)

class FileChange(BaseModel):
    path: str = Field(description="path relative to the codebase root, e.g. src/train.py")
    content: str = Field(description="the complete new content of the file")


class FixOutput(BaseModel):
    diagnosis: str = Field(description="what went wrong and what you changed, 1-2 short sentences in Korean")
    fixable: bool = Field(description="false if the failure cannot be fixed by editing code (e.g. data or model "
                                      "must be downloaded from the internet)")
    changes: list[FileChange] = Field(default_factory=list, max_length=6)


FIX_SYSTEM = """You fix a research-paper reproduction codebase that failed when its reproduce.sh was run on a GPU server.
Environment of the run: Linux x86_64, Python 3.12, PyTorch 2.14 with CUDA on NVIDIA TITAN Xp GPUs (12 GB each),
numpy 2.5. There is NO internet access: nothing can be downloaded (datasets, pretrained weights, pip packages
beyond the ones already installed). The run has a time limit of {minutes} minutes.

Rules:
- Make the smallest change that fixes the error. Keep the experiment faithful to the paper: do not remove experiments,
  do not fake or hard-code results, do not replace real data with made-up data.
- If the failure needs internet (downloading a dataset or a model) or is not caused by the code, set fixable=false
  and return no changes.
- For an out-of-memory error you may reduce the batch size or use gradient accumulation.
- Return the complete new content of each file you change (at most 6 files). Paths are relative to the codebase root.
  Never touch files under .paperagent/.
- Write the diagnosis in Korean, 1-2 short sentences, for a researcher who will read it on the result page."""


def _tail(text: str, lines: int, chars: int) -> str:
    out = "\n".join(text.splitlines()[-lines:])
    return out[-chars:]


def fix_with_llm(llm: LLM, code: Path, attempt_dir: Path, minutes: int, state: str) -> dict:
    log_text = read_text(attempt_dir / "reproduce.log") or read_text(attempt_dir / "logs" / "stdout.log")
    err_text = read_text(attempt_dir / "logs" / "stderr.log")
    pip_text = read_text(attempt_dir / "pip.log")
    tree = "\n".join(f"{p.relative_to(code).as_posix()} ({p.stat().st_size} B)" for p in sorted(code.rglob("*"))
                     if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(code).parts))[:6000]
    mentioned = []
    for m in re.finditer(r'File "(?:/workspace/)?([^"]+\.py)", line \d+', log_text + "\n" + err_text):
        rel = m.group(1).lstrip("./")
        if rel not in mentioned and (code / rel).is_file():
            mentioned.append(rel)
    for rel in ("reproduce.sh",):
        if (code / rel).is_file() and rel not in mentioned:
            mentioned.append(rel)
    files, total = [], 0
    for rel in mentioned[-6:]:
        body = read_text(code / rel)[:40_000]
        total += len(body)
        if total > 120_000:
            break
        files.append(f"<FILE:{rel}>\n{body}\n</FILE:{rel}>")
    orig_req = read_text(code / "requirements.txt")
    messages = [
        {"role": "system", "content": FIX_SYSTEM.format(minutes=minutes)},
        {"role": "user", "content": f"JPUShare final state: {state}\n\nFiles in the codebase:\n{tree}\n\n"
                                    f"requirements.txt:\n{orig_req[:3000]}"},
        {"role": "user", "content": f"End of the run log (stdout and stderr of reproduce.sh):\n<log>\n"
                                    f"{_tail(log_text, 200, 15_000)}\n</log>"
                                    + (f"\n\nstderr:\n<stderr>\n{_tail(err_text, 80, 6000)}\n</stderr>" if err_text.strip() else "")
                                    + (f"\n\npip install log:\n<pip>\n{_tail(pip_text, 40, 3000)}\n</pip>"
                                       if "ERROR" in pip_text else "")},
        {"role": "user", "content": "Relevant files (from the traceback):\n" + "\n\n".join(files)},
        {"role": "user", "content": "Diagnose the failure and fix it following the rules."},
    ]
    text = llm.chat("fix", messages, response_format={
        "type": "json_schema", "json_schema": {"name": "fix", "schema": FixOutput.model_json_schema()}})
    out = FixOutput.model_validate_json(text)
    changed = []
    if out.fixable:
        root = code.resolve()
        for ch in out.changes:
            target = (code / ch.path.lstrip("/")).resolve()
            rel = target.relative_to(root).as_posix() if target.is_relative_to(root) else None
            if rel is None or rel.startswith(".paperagent") or any(part in SKIP_DIRS for part in Path(rel).parts):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(ch.content, encoding="utf-8")
            changed.append(rel)
    return {"diagnosis": out.diagnosis.strip(), "fixable": out.fixable and bool(changed), "files": changed}


# ---------------------------------------------------------------- 공통

def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def write_json(p: Path, data: dict) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def extract_results(data: bytes, dest: Path) -> None:
    root = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if info.is_dir() or not target.is_relative_to(root):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))


def shorten_log(text: str) -> str:
    """judge에게 줄 reproduce.log: 앞 200줄 + 뒤 800줄, 최대 8만 자(학습 로그가 수 MB여도 토큰이 터지지 않게)."""
    lines = text.splitlines()
    if len(lines) > LOG_HEAD_LINES + LOG_TAIL_LINES:
        lines = lines[:LOG_HEAD_LINES] + [f"... ({len(lines) - LOG_HEAD_LINES - LOG_TAIL_LINES} lines omitted) ..."] \
            + lines[-LOG_TAIL_LINES:]
    out = "\n".join(lines)
    if len(out) > LOG_MAX_CHARS:
        out = out[:LOG_MAX_CHARS // 4] + "\n... (truncated) ...\n" + out[-LOG_MAX_CHARS * 3 // 4:]
    return out


def build_judged(code: Path, attempt_dir: Path | None, judged: Path) -> list[str]:
    """judge가 볼 폴더: 고친 코드 + reproduce.log + 실행 중 생긴 파일(같은 경로에). PaperBench의 실행 후 제출물과 같은 모양."""
    shutil.rmtree(judged, ignore_errors=True)
    shutil.copytree(code, judged, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    outputs = []
    if attempt_dir is not None:
        files = attempt_dir / "files"
        if files.is_dir():
            for p in sorted(files.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(files)
                target = judged / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if p.suffix.lower() in (".json", ".jsonl", ".csv", ".tsv", ".txt", ".md", ".log", ".yaml", ".yml") \
                        and p.stat().st_size > TEXT_OUTPUT_MAX:
                    target.write_text(read_text(p)[:TEXT_OUTPUT_MAX] + "\n... (truncated by PaperAgent) ...\n")
                else:
                    shutil.copyfile(p, target)
                outputs.append(rel.as_posix())
        log_text = read_text(attempt_dir / "reproduce.log") or read_text(attempt_dir / "logs" / "stdout.log")
        (judged / "reproduce.log").write_text(shorten_log(log_text), encoding="utf-8")
    return outputs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--submission-dir", required=True)
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--max-fixes", type=int, default=2)
    ap.add_argument("--tier", default="jpu-48-edu")
    args = ap.parse_args()

    base = Path("data/agent_runs") / args.run_id / "gpu"
    base.mkdir(parents=True, exist_ok=True)
    code, attempts_dir, judged = base / "code", base / "attempts", base / "judged"
    status = {"state": "preparing", "attempt": 0, "attempts": [], "started_at": time.time(), "tier": args.tier,
              "minutes": args.minutes}
    result: dict = {"final": "error", "message": "", "attempts": status["attempts"], "changed_files": [],
                    "outputs": [], "tier": args.tier, "minutes": args.minutes}

    def log(msg: str) -> None:
        print(f"[gpu-run] {msg}", flush=True)

    def save_status(**kw) -> None:
        status.update(kw, updated_at=time.time())
        write_json(base / "status.json", status)

    original: dict[str, bytes] = {}

    def finish(final: str, message: str, attempt_dir: Path | None) -> int:
        result.update(final=final, message=message, finished_at=time.time(),
                      outputs=build_judged(code, attempt_dir, judged) if code.is_dir() else [])
        if code.is_dir() and original:
            result["changed_files"] = sorted(p.relative_to(code).as_posix() for p in code.rglob("*") if p.is_file()
                                             and original.get(p.relative_to(code).as_posix()) != p.read_bytes())
        result["judged_dir"] = judged.as_posix() if attempt_dir is not None else None
        write_json(base / "result.json", result)
        save_status(state="done")
        log(f"done: {final} ({message})")
        return 0

    key = os.environ.get("JPUSHARE_API_KEY", "").strip()
    if not key:
        return finish("error", "JPUShare API 키가 설정되지 않았습니다(Secret jpushare).", None)
    jpu = JPU(key)
    (base / "result.json").unlink(missing_ok=True)
    (base / "cancel").unlink(missing_ok=True)
    shutil.rmtree(code, ignore_errors=True)
    shutil.rmtree(attempts_dir, ignore_errors=True)
    shutil.copytree(args.submission_dir, code, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    if not (code / "reproduce.sh").is_file():
        return finish("error", "reproduce.sh가 없어 실행할 수 없습니다.", None)
    original.update({p.relative_to(code).as_posix(): p.read_bytes() for p in code.rglob("*") if p.is_file()})
    llm = LLM(base)
    run_sec = args.minutes * 60
    attempt_dir = None

    for n in range(1, args.max_fixes + 2):
        save_status(state="preparing", attempt=n)
        req_text = read_text(code / "requirements.txt")
        keep, dropped, unsupported = split_requirements(req_text)
        try:
            wheels, names, failed = fetch_wheels(keep, Path("data/gpu_wheels"), log)
        except Exception as e:
            return finish("error", f"필요한 패키지를 준비하지 못했습니다: {str(e)[-300:]}", attempt_dir)
        art = pack(code, wheels, names, run_sec)
        log(f"attempt {n}: zip {len(art) / 1e6:.1f} MB, wheels {len(names)}, preinstalled {len(dropped)}, "
            f"not available {failed + unsupported}")
        try:
            jid = jpu.submit(art, "bash .paperagent/run.sh", f"paperagent-{args.run_id}-{n}",
                             f"PaperAgent: run reproduce.sh of job {args.run_id} (attempt {n}) for paper reproduction",
                             args.tier, run_sec + 900, {})
        except Exception as e:
            return finish("error", f"JPUShare에 제출하지 못했습니다: {str(e)[-300:]}", attempt_dir)
        rec = {"n": n, "jpushare_id": jid, "state": "SUBMITTED", "submitted_at": time.time(), "started_at": None,
               "finished_at": None, "packages": names, "unavailable": failed + unsupported, "fix": None}
        status["attempts"].append(rec)
        save_status(state="queued")
        log(f"attempt {n}: submitted {jid}")

        cancel_sent = False
        while True:
            time.sleep(POLL_SEC)
            if (base / "cancel").exists() and not cancel_sent:
                log("cancel requested by the user")
                try:
                    jpu.cancel(jid)
                except Exception as e:
                    log(f"cancel failed: {e}")
                cancel_sent = True
            try:
                d = jpu.job(jid)
            except Exception as e:
                log(f"poll failed: {e}")
                continue
            state = d.get("state", "?")
            if state != rec["state"]:
                log(f"attempt {n}: {state}")
            rec["state"] = state
            if state == "RUNNING" and not rec["started_at"]:
                rec["started_at"] = time.time()
            if d.get("last_error_message"):
                rec["error"] = str(d["last_error_message"])[:500]
            save_status(state="running" if state in ("RUNNING", "ASSIGNED") else
                        "cancelling" if cancel_sent else "queued")
            if state in TERMINAL:
                rec["finished_at"] = time.time()
                break

        attempt_dir = attempts_dir / str(n)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(8):  # 결과 업로드가 조금 늦게 끝날 수 있다
            try:
                extract_results(jpu.results_zip(jid), attempt_dir)
            except Exception as e:
                log(f"results not ready: {str(e)[:200]}")
            if (attempt_dir / "logs" / "stdout.log").exists() or (attempt_dir / "reproduce.log").exists():
                break
            time.sleep(15)
        exit_text = read_text(attempt_dir / "exit_code.txt").strip()
        rec["exit_code"] = int(exit_text) if exit_text.lstrip("-").isdigit() else None
        log_text = read_text(attempt_dir / "reproduce.log")
        rec["time_limit"] = "[paperagent] stopped: time limit" in log_text or state == "TIMED_OUT"
        save_status(state="checking")

        if state == "SUCCEEDED":
            return finish("succeeded", f"{n}번째 실행에서 끝까지 실행했습니다.", attempt_dir)
        if state == "CANCELLED":
            return finish("cancelled", "사용자가 실행을 취소했습니다.", attempt_dir)
        if rec["time_limit"]:
            return finish("time_limit", f"{args.minutes}분 시간 제한에 걸려 실행을 멈췄습니다. 그때까지의 로그와 결과로 검증합니다.",
                          attempt_dir)
        if state not in FIXABLE:
            return finish("failed", f"JPUShare 작업이 {state}로 끝났습니다. {rec.get('error', '')}".strip(), attempt_dir)
        if n > args.max_fixes:
            return finish("failed", f"{n}번 실행했지만 오류가 남았습니다.", attempt_dir)
        save_status(state="fixing")
        try:
            fix = fix_with_llm(llm, code, attempt_dir, args.minutes, state)
        except Exception as e:
            return finish("failed", f"오류를 고치지 못했습니다(LLM 호출 실패: {str(e)[:200]}).", attempt_dir)
        rec["fix"] = fix
        log(f"fix: {fix['diagnosis']} files={fix['files']}")
        save_status(state="fixing")
        if not fix["fixable"]:
            return finish("failed", f"코드를 고쳐서 해결할 수 없는 오류입니다: {fix['diagnosis']}", attempt_dir)
    return finish("failed", "실행을 끝내지 못했습니다.", attempt_dir)


if __name__ == "__main__":
    sys.exit(main())

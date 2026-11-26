from __future__ import annotations

import io
import zipfile
from pathlib import Path

from agent import gpu_run, pipeline


def test_pipeline_extract_block():
    assert pipeline.extract_block("```python\nprint(1)\n```\n") == "print(1)\n"
    assert pipeline.extract_block("Here:\n```bash\necho hi\n```\nDone") == "echo hi\n"
    assert pipeline.extract_block("plain text") == "plain text\n"


def test_gpu_run_split_requirements_and_preinstalled():
    assert gpu_run.is_preinstalled("torch")
    assert gpu_run.is_preinstalled("NVIDIA_cublas_cu12")
    assert not gpu_run.is_preinstalled("scipy")

    req = """
    # comment
    torch>=2.0
    numpy==2.1.0
    scipy>=1.11
    scikit-learn
    -e git+https://example.com/repo.git
    """
    keep, dropped, unsupported = gpu_run.split_requirements(req)
    assert keep == ["scipy>=1.11", "scikit-learn"]
    assert dropped == ["torch>=2.0", "numpy==2.1.0"]
    assert len(unsupported) == 1


def test_gpu_run_pack_and_shorten_log(tmp_path: Path):
    code = tmp_path / "code"
    (code / "src").mkdir(parents=True)
    (code / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (code / "requirements.txt").write_text("torch\nscipy\n", encoding="utf-8")
    (code / "reproduce.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "scipy-1.0.whl").write_bytes(b"fake-wheel")

    blob = gpu_run.pack(code, wheels, ["scipy"], run_sec=600)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = set(z.namelist())
        assert "src/main.py" in names
        assert ".paperagent/run.sh" in names
        assert ".paperagent/wheels/scipy-1.0.whl" in names
        assert z.read("requirements.txt").decode() == "scipy\n"
        assert "timeout --signal=INT --kill-after=60 600" in z.read(".paperagent/run.sh").decode()

    long_log = "\n".join(f"line {i}" for i in range(2000))
    short = gpu_run.shorten_log(long_log)
    assert "line 0" in short and "line 1999" in short and "lines omitted" in short

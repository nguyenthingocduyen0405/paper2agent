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


def test_gpu_run_detect_and_pack_offline_datasets(tmp_path: Path):
    code = tmp_path / "code"
    (code / "src").mkdir(parents=True)
    (code / "src" / "data.py").write_text(
        "from torchvision import datasets\nds = datasets.MNIST('./d', download=True)\nc = datasets.CIFAR10('./d')\n",
        encoding="utf-8",
    )
    (code / "requirements.txt").write_text("torch\n", encoding="utf-8")
    (code / "reproduce.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    assert gpu_run.detect_datasets(code) == ["mnist", "cifar10"]

    ds_dir = tmp_path / "ds_mnist"
    (ds_dir / "MNIST" / "raw").mkdir(parents=True)
    (ds_dir / "MNIST" / "raw" / "train-images-idx3-ubyte").write_bytes(b"mnist-bytes")

    blob = gpu_run.pack(code, None, [], run_sec=300, datasets=[("mnist", ds_dir)])
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = set(z.namelist())
        assert ".paperagent/py/sitecustomize.py" in names
        assert ".paperagent/datasets/MNIST/raw/train-images-idx3-ubyte" in names
        assert "PAPERAGENT_DATASETS_DIR" in z.read(".paperagent/run.sh").decode()


def test_sitecustomize_wraps_fashion_mnist_subclass(tmp_path: Path, monkeypatch):
    import sys
    import types

    ds_root = tmp_path / "bundled"
    (ds_root / "MNIST" / "raw").mkdir(parents=True)
    (ds_root / "MNIST" / "raw" / "f").write_bytes(b"m")
    (ds_root / "FashionMNIST" / "raw").mkdir(parents=True)
    (ds_root / "FashionMNIST" / "raw" / "f").write_bytes(b"fm")

    class MNIST:
        def __init__(self, root, download=False):
            self.root = root

    class FashionMNIST(MNIST):
        pass

    tv = types.ModuleType("torchvision")
    tv_ds = types.ModuleType("torchvision.datasets")
    tv_ds.MNIST = MNIST
    tv_ds.FashionMNIST = FashionMNIST
    tv.datasets = tv_ds
    monkeypatch.setitem(sys.modules, "torchvision", tv)
    monkeypatch.setitem(sys.modules, "torchvision.datasets", tv_ds)
    monkeypatch.setenv("PAPERAGENT_DATASETS_DIR", str(ds_root))

    ns: dict = {}
    exec(compile(gpu_run.SITE_CUSTOMIZE_PY, "sitecustomize.py", "exec"), ns)

    out_root = tmp_path / "user_data"
    FashionMNIST(str(out_root), download=True)
    assert (out_root / "FashionMNIST" / "raw" / "f").read_bytes() == b"fm"
    assert not (out_root / "MNIST").exists()


def test_gpu_run_cifar100_bundled_and_unbundled_datasets_reported(tmp_path: Path):
    code = tmp_path / "code"
    code.mkdir()
    (code / "train.py").write_text(
        "from torchvision import datasets\n"
        "a = datasets.CIFAR100('./data', download=True)\n"
        "b = datasets.SVHN('./data', download=True)\n"
        "c = datasets.ImageFolder('./imgs')\n"
        "from datasets import load_dataset\n"
        "d = load_dataset('glue', 'sst2')\n", encoding="utf-8")
    assert gpu_run.detect_datasets(code) == ["cifar100"]  # CIFAR100( is not mistaken for CIFAR10(
    assert gpu_run.detect_unbundled_datasets(code) == ["SVHN", "Hugging Face: glue"]
    assert "CIFAR100" in gpu_run.SITE_CUSTOMIZE_PY
    prompt = gpu_run.FIX_SYSTEM.format(minutes=30, datasets=gpu_run.BUNDLED_DATASET_NAMES)
    assert "CIFAR100" in prompt and "not a missing download" in prompt

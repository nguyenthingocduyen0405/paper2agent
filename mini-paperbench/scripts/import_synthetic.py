"""합성 논문 zip을 judge용 폴더와 정답 라벨 폴더로 나눠 푼다.

zip 안의 맨 위 폴더 중 paper/paper.md가 있는 것을 논문 하나로 본다(zip 하나에 여러 편이 있어도 된다).
  data/synthetic/<이름>/          paper.md, rubric.json, submissions/   ← judge가 읽는 곳
  data/synthetic_labels/<이름>/   sub_*.json, labels_table.csv, README.md ← 평가 스크립트만 읽는 곳
build/, common/(정답 코드 원본, 생성 도구)는 풀지 않는다. judge에게 보이면 안 되기 때문이다.

사용: python scripts/import_synthetic.py datasets/synthetic_*.zip
"""
import shutil
import sys
import zipfile
from pathlib import Path

for zpath in sys.argv[1:]:
    zf = zipfile.ZipFile(zpath)
    names = zf.namelist()
    roots = sorted({n.split("/")[0] for n in names if n.count("/") >= 2 and n.split("/")[1:3] == ["paper", "paper.md"]})
    for root in roots:
        name = root.removeprefix("synthetic_")
        judge_dir = Path("data/synthetic") / name
        label_dir = Path("data/synthetic_labels") / name
        for d in (judge_dir, label_dir):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True)
        for info in zf.infolist():
            parts = info.filename.split("/")
            if info.is_dir() or parts[0] != root or ".." in parts or info.filename.startswith("/"):
                continue
            rel = parts[1:]
            if rel[0] == "paper":
                dest = judge_dir / "/".join(rel[1:])
            elif rel[0] in ("rubric.json", "submissions"):
                dest = judge_dir / "/".join(rel)
            elif rel[0] == "labels":
                dest = label_dir / "/".join(rel[1:])
            elif rel == ["README.md"]:
                dest = label_dir / "README.md"
            else:  # build/ 등
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info))
        subs = sorted(p.name for p in (judge_dir / "submissions").iterdir())
        print(f"{name}: {judge_dir} (submissions: {', '.join(subs)}), labels → {label_dir}")

"""표준 입력의 KEY=VALUE 줄로 .env를 고친다(같은 키는 바꾸고 없는 키는 붙인다). 값은 화면에 찍지 않는다.

사용(mini-paperbench 폴더에서): printf 'S3_BUCKET=...\n' | python3 scripts/env_update.py
"""
import os
import sys
from pathlib import Path

path = Path(".env")
new = {}
for line in sys.stdin.read().splitlines():
    if "=" in line and not line.startswith("#"):
        key, value = line.split("=", 1)
        new[key.strip()] = value.strip()
lines = path.read_text().splitlines() if path.exists() else []
out = [line for line in lines if line.split("=", 1)[0].strip() not in new]
out += [f"{k}={v}" for k, v in new.items()]
tmp = path.with_name(".env.tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("\n".join(out) + "\n")
os.replace(tmp, path)
print("updated .env keys:", ", ".join(new) or "(none)")

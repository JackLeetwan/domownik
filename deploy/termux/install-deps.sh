#!/data/data/com.termux/files/usr/bin/bash
# Install Domownik's Python dependencies inside Termux (run on the phone, from ~/domownik).
# - no prebuilt wheels for Android: pillow and cryptography come from Termux packages
#   (venv with system site-packages), the rest is built here (rust/clang/cmake from pkg);
# - uvicorn without [standard]: uvloop and watchfiles do not build on Android and are not needed.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."

pkg_py=(python python-pillow python-cryptography python-greenlet)
pkg_build=(git clang make cmake ninja rust maturin libffi openssl sqlite binutils)
missing=()
for p in "${pkg_py[@]}" "${pkg_build[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [[ ${#missing[@]} -gt 0 ]]; then
  DEBIAN_FRONTEND=noninteractive apt-get -y -q install "${missing[@]}"
fi

[[ -x .venv/bin/python ]] || python -m venv --system-site-packages .venv
py=.venv/bin/python

reqs=$(mktemp) cons=$(mktemp)
trap 'rm -f "$reqs" "$cons"' EXIT
"$py" - "$reqs" <<'PY'
import sys, tomllib
deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
out = []
for d in deps:
    if d.startswith("uvicorn[standard]"):
        out += ["uvicorn" + d[len("uvicorn[standard]"):], "httptools", "websockets", "python-dotenv"]
    else:
        out.append(d)
open(sys.argv[1], "w").write("\n".join(out) + "\n")
PY
# pin what Termux already provides, so pip does not try to build it again
"$py" -c 'import PIL, cryptography, greenlet; print(f"pillow=={PIL.__version__}\ncryptography=={cryptography.__version__}\ngreenlet=={greenlet.__version__}")' > "$cons"

# maturin needs the API level the interpreter reports (Termux: 24), not the device's
export ANDROID_API_LEVEL=$("$py" -c 'import sys; print(sys.getandroidapilevel())')
export CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-6}
"$py" -m pip install -q -c "$cons" -r "$reqs"
"$py" -m pip install -q --no-deps -e .
"$py" -c 'import domownik, fastapi, argon2, pywebpush, PIL; print("zależności OK")'

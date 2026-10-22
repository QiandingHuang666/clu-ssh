#!/bin/sh
# 用 PyInstaller 打单文件二进制（clu 内部分派 clu-mcp）。
# 用法：packaging/build_binary.sh [输出目录，默认 dist]
set -e
cd "$(dirname "$0")/.."
ROOT=$(pwd)
OUT=${1:-dist}
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) SEP=";" ;;
  *) SEP=":" ;;
esac
uv run --with pyinstaller pyinstaller \
  --onefile --noconfirm --clean --name clu \
  --distpath "$OUT" \
  --workpath build/pyinstaller \
  --specpath build/pyinstaller \
  --add-data "$ROOT/src/clu/ptyd${SEP}clu/ptyd" \
  --collect-submodules mcp \
  --collect-submodules asyncssh \
  src/clu/bundle_entry.py
echo "built $OUT/clu"

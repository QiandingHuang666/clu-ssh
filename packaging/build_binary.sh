#!/bin/sh
# 用 Nuitka 打单文件二进制（clu 按 argv[0] 分派 clu-mcp）。
# 用法：packaging/build_binary.sh [输出目录，默认 dist]
# 注意：Nuitka 不能交叉编译，需在各目标平台分别构建（CI 矩阵负责）。
set -e
cd "$(dirname "$0")/.."
ROOT=$(pwd)
OUT=${1:-dist}
VER=$(sed -n 's/__version__ = "\(.*\)"/\1/p' src/clu/__init__.py | head -1)
[ -n "$VER" ] || VER=dev
mkdir -p "$OUT"

EXTRA=""
case "$(uname -s)" in
  Darwin)
    # 避免 Homebrew LLVM / 第三方 flags 污染（本地环境常见），固定 Apple clang
    export CC=/usr/bin/clang CXX=/usr/bin/clang++
    unset CPPFLAGS LDFLAGS CPATH LIBRARY_PATH || true
    ;;
  MINGW*|MSYS*|CYGWIN*)
    EXTRA="--windows-console-mode=force --product-name=clu \
      --product-version=${VER}.0 --file-version=${VER}.0 --company-name=Qianding Huang"
    ;;
esac

uv run --with nuitka python -m nuitka \
  --onefile \
  --assume-yes-for-downloads \
  --output-filename=clu \
  --output-dir="$OUT" \
  --remove-output \
  --include-data-dir="$ROOT/src/clu/ptyd=clu/ptyd" \
  --include-package=mcp \
  --include-package=asyncssh \
  $EXTRA \
  src/clu/bundle_entry.py
echo "built $OUT/clu"

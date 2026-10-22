#!/bin/sh
# 交叉编译 clu-ptyd（linux amd64/arm64 静态二进制，gzip）到 wheel 包数据目录
set -e
cd "$(dirname "$0")"
OUT=${1:-../src/clu/ptyd}
VERSION=$(sed -n 's/^PTYD_VERSION = "\(.*\)"/\1/p' ../src/clu/ptyd.py | head -1)
[ -n "$VERSION" ] || VERSION=dev
mkdir -p "$OUT"
for arch in amd64 arm64; do
  CGO_ENABLED=0 GOOS=linux GOARCH=$arch \
    go build -trimpath -ldflags "-s -w -X main.version=$VERSION" \
    -o "/tmp/clu-ptyd-linux-$arch" .
  gzip -cn "/tmp/clu-ptyd-linux-$arch" > "$OUT/clu-ptyd-linux-$arch.gz"
  echo "built $OUT/clu-ptyd-linux-$arch.gz (version=$VERSION)"
done

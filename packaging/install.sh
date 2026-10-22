#!/bin/sh
# clu 二进制安装脚本（Linux / macOS）
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/QiandingHuang666/clu-ssh/main/packaging/install.sh | sh
#   或本地：./packaging/install.sh
# 环境变量：CLU_VERSION（默认 latest）、CLU_PREFIX（默认 ~/.local/bin）
set -eu

REPO="QiandingHuang666/clu-ssh"
VERSION="${CLU_VERSION:-latest}"
PREFIX="${CLU_PREFIX:-$HOME/.local/bin}"

os=$(uname -s)
case "$os" in
  Linux) os_key=linux ;;
  Darwin) os_key=darwin ;;
  *) echo "不支持的平台：$os" >&2; exit 1 ;;
esac
arch=$(uname -m)
case "$arch" in
  x86_64|amd64) arch_key=x86_64 ;;
  arm64|aarch64) arch_key=arm64 ;;
  *) echo "不支持的架构：$arch" >&2; exit 1 ;;
esac

if [ "$VERSION" = "latest" ]; then
  VERSION=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
    | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)
fi
[ -n "$VERSION" ] || { echo "无法确定最新版本" >&2; exit 1; }
ver=${VERSION#v}
asset="clu-${ver}-${os_key}-${arch_key}.tar.gz"
url="https://github.com/$REPO/releases/download/${VERSION}/${asset}"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo "下载 $url"
curl -fsSL "$url" -o "$tmp/$asset"

if curl -fsSL "$url.sha256" -o "$tmp/$asset.sha256" 2>/dev/null; then
  expected=$(awk '{print $1}' "$tmp/$asset.sha256")
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$tmp/$asset" | awk '{print $1}')
  else
    actual=$(shasum -a 256 "$tmp/$asset" | awk '{print $1}')
  fi
  [ "$expected" = "$actual" ] || { echo "校验失败（sha256）" >&2; exit 1; }
  echo "sha256 校验通过"
fi

tar -xzf "$tmp/$asset" -C "$tmp"
mkdir -p "$PREFIX"
install -m 0755 "$tmp/clu" "$PREFIX/clu"
ln -sf "$PREFIX/clu" "$PREFIX/clu-mcp"   # 同一二进制按 argv[0] 分派 MCP server
echo "已安装：$PREFIX/clu（clu-mcp 为软链）"
echo "请确保 $PREFIX 在 PATH 中。"

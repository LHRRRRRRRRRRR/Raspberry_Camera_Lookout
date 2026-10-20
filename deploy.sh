#!/usr/bin/env bash
# deploy.sh —— 把代码同步到 Pi 并重启服务。
#
# 前置条件：已经能免密 ssh 到 Pi（ssh-copy-id）。
# 用法：
#   ./deploy.sh                          # 默认 pi@raspberrypi.local
#   ./deploy.sh pi@192.168.1.20          # 指定主机
#   ./deploy.sh pi@host /home/pi/lookout  # 指定目标目录
set -euo pipefail

HOST="${1:-${LOOKOUT_HOST:-pi@raspberrypi.local}}"
DEST="${2:-${LOOKOUT_DEST:-/home/pi/lookout}}"
SERVICE="vision"

echo "==> 目标: ${HOST}:${DEST}"

# 只同步运行需要的东西，实验/文档/图片不同步
rsync -avz --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'events/' \
  --exclude 'shots/' \
  --exclude 'runtime.json' \
  ./pi/ "${HOST}:${DEST}/"

# config.toml 是本地私有配置：第一次部署时如果远端没有就拷一份
if ! ssh "${HOST}" "test -f ${DEST}/config.toml"; then
  echo "==> 远端没有 config.toml，拷一份示例过去"
  scp ./config.example.toml "${HOST}:${DEST}/config.toml"
  echo "    ⚠ 记得上去改：${HOST}:${DEST}/config.toml"
fi

# 服务单元（只有当内容变了才覆盖）
echo "==> 安装 systemd 服务"
ssh "${HOST}" "sudo cp ${DEST}/vision.service /etc/systemd/system/ && sudo systemctl daemon-reload"

echo "==> 重启服务"
ssh "${HOST}" "sudo systemctl enable --now ${SERVICE} && sudo systemctl restart ${SERVICE}"
sleep 8
ssh "${HOST}" "systemctl is-active ${SERVICE} && curl -s --max-time 5 http://127.0.0.1:8080/status || true"
echo
echo "==> 完成"

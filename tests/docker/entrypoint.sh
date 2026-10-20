#!/bin/sh
# 集成测试用 sshd 容器：注入 PUBKEY（公钥一行）后启动 sshd
set -e
ssh-keygen -A
mkdir -p /home/tester/.ssh
echo "$PUBKEY" > /home/tester/.ssh/authorized_keys
chown -R tester:tester /home/tester/.ssh
chmod 700 /home/tester/.ssh
chmod 600 /home/tester/.ssh/authorized_keys
exec /usr/sbin/sshd -D -e -o PubkeyAuthentication=yes -o AllowTcpForwarding=yes

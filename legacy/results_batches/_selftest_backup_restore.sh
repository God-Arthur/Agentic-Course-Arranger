#!/usr/bin/env bash
# 一次性核对 §9 备份/恢复命令真的可用：把 logs 卷备份成 tgz，再在容器内恢复到临时目录逐字节核对，跑完即删。
set -eu
cd "$(dirname "$0")/.."

docker compose exec -T planner tar czf - -C /app logs > /tmp/backup-logs.tgz
echo "备份大小：$(wc -c < /tmp/backup-logs.tgz) 字节"

docker compose exec -T planner sh -c 'cat > /tmp/restore-probe.tgz' < /tmp/backup-logs.tgz
docker compose exec -T planner sh -c '
  mkdir -p /tmp/restore-into &&
  tar xzf /tmp/restore-probe.tgz -C /tmp/restore-into &&
  echo "恢复出的 runs.jsonl 行数：$(wc -l < /tmp/restore-into/logs/runs.jsonl)" &&
  cmp /tmp/restore-into/logs/runs.jsonl /app/logs/runs.jsonl &&
  echo "cmp：恢复副本与卷内文件逐字节一致" &&
  rm -rf /tmp/restore-into /tmp/restore-probe.tgz'

rm -f /tmp/backup-logs.tgz
echo BACKUP-RESTORE-OK

#!/usr/bin/env bash
# 箱の負荷を 2 秒ごとに記録する（load 1/5/15、メモリ使用・空き・スワップ、postgres コンテナのメモリ）。
# 使い方: bench/sysmon.sh OUTFILE &   → 止めるときは起動時の PID を kill する
OUT="${1:?出力先}"
echo "time load1 load5 load15 mem_used_mb mem_avail_mb swap_used_mb pg_mem" > "$OUT"
while true; do
  read -r l1 l5 l15 _ < /proc/loadavg
  read -r _ _ used _ _ _ avail < <(free -m | sed -n 2p)
  read -r _ _ sw _ < <(free -m | sed -n 3p)
  pg=$(docker stats --no-stream --format '{{.MemUsage}}' postgresql 2>/dev/null | cut -d/ -f1 | tr -d ' ')
  echo "$(date +%T) $l1 $l5 $l15 $used $avail $sw $pg" >> "$OUT"
  sleep 2
done

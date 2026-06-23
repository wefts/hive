#!/usr/bin/env bash
# Soak the running stack: every INTERVAL_S do an Embed and sample per-service
# memory / open-FDs / restart-count. CSV to $OUT. Detects leaks & restarts (drift),
# not just "it ran".
set -uo pipefail

DURATION_S=${DURATION_S:-14400}   # 4h
INTERVAL_S=${INTERVAL_S:-60}
OUT=${OUT:-/tmp/soak.csv}
SERVICES="hive-postgres-1 hive-ollama-1 hive-ml-1 hive-kernel-1"

hdr="ts,iter,embed_ok,embed_ms"
for s in $SERVICES; do hdr="$hdr,${s##hive-}_memMiB,${s##hive-}_fd,${s##hive-}_restarts"; done
echo "$hdr" > "$OUT"

start=$(date +%s); iter=0
while :; do
  now=$(date +%s); [ $((now - start)) -ge "$DURATION_S" ] && break
  iter=$((iter + 1))

  t0=$(date +%s%3N)
  res=$(docker exec hive-kernel-1 /app/bin/swarm rpc \
        'case Swarm.ML.Embeddings.embed(["soak"]) do {:ok,r}->IO.puts("OK"); _->IO.puts("ERR") end' \
        2>/dev/null | grep -oE "OK|ERR" | head -1)
  t1=$(date +%s%3N)
  ok=0; [ "$res" = "OK" ] && ok=1
  row="$(date -Iseconds),$iter,$ok,$((t1 - t0))"

  # One stats snapshot for all services.
  stats=$(docker stats --no-stream --format '{{.Name}};{{.MemUsage}}' $SERVICES 2>/dev/null)
  for s in $SERVICES; do
    pid=$(docker inspect -f '{{.State.Pid}}' "$s" 2>/dev/null)
    mem=$(echo "$stats" | awk -F';' -v n="$s" '$1==n{print $2}' | awk '{print $1}')
    fd=$(sudo ls "/proc/$pid/fd" 2>/dev/null | wc -l); [ "$fd" = 0 ] && fd=$(ls "/proc/$pid/fd" 2>/dev/null | wc -l)
    rc=$(docker inspect -f '{{.RestartCount}}' "$s" 2>/dev/null)
    row="$row,$mem,$fd,$rc"
  done
  echo "$row" >> "$OUT"
  sleep "$INTERVAL_S"
done
echo "# soak complete: $iter iters over $((($(date +%s) - start))/60)) min" >> "$OUT"

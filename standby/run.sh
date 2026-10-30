#!/usr/bin/env bash
# 使い捨てのコンテナ（Hindsight の公式イメージ。psycopg2・numpy 入り）で standby の python を流す。
#   ./run.sh filler.py --db hindsight_standby_test --model ... 
#   ./run.sh switch.py ...
# 本番の hindsight コンテナには入らない。DB の鍵は .env から2つだけ読んで環境で渡す（表示しない）。
# --network host＝ホストの 127.0.0.1（postgres 5432、llama-embed 8081、sarashina-embed 8082）をそのまま使う。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENVF="$HERE/../.env"
getenv() { sed -n "s/^$1=//p" "$ENVF" | head -n1; }
VERSION="${HINDSIGHT_VERSION:-$(getenv HINDSIGHT_VERSION)}"
HINDSIGHT_DB_USER="$(getenv HINDSIGHT_DB_USER)"
HINDSIGHT_DB_PASSWORD="$(getenv HINDSIGHT_DB_PASSWORD)"
export HINDSIGHT_DB_USER HINDSIGHT_DB_PASSWORD
SCRIPT="$1"; shift
EXTRA=()
# 任意: 出力やベンチ用に追加のマウント（STANDBY_MOUNTS="/host:/in/container ..."）
for m in ${STANDBY_MOUNTS:-}; do EXTRA+=(-v "$m"); done
exec docker run --rm -i --network host --user "$(id -u):$(id -g)" \
  -e HINDSIGHT_DB_USER -e HINDSIGHT_DB_PASSWORD -e STANDBY_DB_HOST -e STANDBY_DB_PORT -e PYTHONUNBUFFERED=1 \
  -v "$HERE:/standby:ro" "${EXTRA[@]}" -w /standby \
  --entrypoint python "ghcr.io/vectorize-io/hindsight:${VERSION:-1.1.0}" "/standby/$SCRIPT" "$@"

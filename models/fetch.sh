#!/usr/bin/env bash
# 埋め込みモデルを Hugging Face から models/<名前>/ に落とし、必要なら接頭辞（prompts）を設定に書き足す。
#
# Hindsight の local プロバイダ（SentenceTransformers）は、モデルが自前で持つ prompts（config_sentence_transformers.json）
# だけを使う。ruri-v3 や multilingual-e5 のように「問いと文書に接頭辞を付けろ」とカードが言うのに prompts が空のモデルは、
# 本家を触らずにここで prompts を書き足して、compose から HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL=/models/<名前> で指す。
# 公式イメージの python と huggingface_hub を使うので、ホストに Python も pip も要らない。
#
# 使い方: models/fetch.sh <HF の repo> [問いの接頭辞] [文書の接頭辞]
#   models/fetch.sh cl-nagoya/ruri-v3-130m "検索クエリ: " "検索文書: "
#   models/fetch.sh intfloat/multilingual-e5-large "query: " "passage: "
#   models/fetch.sh BAAI/bge-m3                              # 接頭辞が要らないモデルは省く（prompts は触らない）
# 置き場: models/<repo の最後の要素>/（.gitignore 済み。消すときはディレクトリごと）
set -euo pipefail
cd "$(dirname "$0")/.."

repo="${1:?HF の repo（例: cl-nagoya/ruri-v3-130m）}"
qprefix="${2-}"
dprefix="${3-}"
name="${repo##*/}"
version="$(sed -n 's/^HINDSIGHT_VERSION=//p' .env 2>/dev/null | head -1)"
image="ghcr.io/vectorize-io/hindsight:${version:-1.0.0}"

mkdir -p models
# 重みの重複（pytorch_model.bin と safetensors）、onnx/openvino 版、評価結果、画像は落とさない
docker run --rm -i -u "$(id -u):$(id -g)" -e HF_HOME=/models/.hf-cache -e HF_HUB_DOWNLOAD_TIMEOUT=600 \
  -v "$PWD/models:/models" --entrypoint python "$image" - "$repo" "$name" "$qprefix" "$dprefix" <<'PY'
import json, os, sys, time
from huggingface_hub import snapshot_download, HfApi
repo, name, qprefix, dprefix = sys.argv[1:5]
dest = f"/models/{name}"
t0 = time.time()
# safetensors があれば .bin は落とさない
files = [s.rfilename for s in HfApi().model_info(repo).siblings]
has_st = any(f.endswith(".safetensors") and "/" not in f for f in files)
ignore = ["onnx/*", "openvino/*", "results*/*", ".eval_results/*", "imgs/*", "*.jpg", "*.webp", "*.png", ".gitattributes"]
if has_st:
    ignore += ["*.bin", "*.pt"]
snapshot_download(repo, local_dir=dest, ignore_patterns=ignore)
dt = time.time() - t0
cfg_path = os.path.join(dest, "config_sentence_transformers.json")
cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}
if qprefix or dprefix:
    prompts = dict(cfg.get("prompts") or {})
    prompts["query"] = qprefix
    prompts["document"] = dprefix
    cfg["prompts"] = prompts
    cfg.setdefault("similarity_fn_name", "cosine")
    json.dump(cfg, open(cfg_path, "w"), ensure_ascii=False, indent=2)
    print(f"prompts written: {json.dumps(prompts, ensure_ascii=False)}")
else:
    print(f"prompts left as is: {json.dumps(cfg.get('prompts'), ensure_ascii=False)}")
size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(dest) for f in fs if ".cache" not in r)
print(f"fetched {repo} -> models/{name} ({size/1e6:.0f} MB, {dt:.0f} s)")
PY
chmod -R a+rX "models/$name"   # コンテナ内の uid 1000（hindsight）が読めるように

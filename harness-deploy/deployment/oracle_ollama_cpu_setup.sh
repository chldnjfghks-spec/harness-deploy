#!/usr/bin/env bash
# 오라클 클라우드(Oracle Cloud Always Free Ubuntu ARM/x86) 24시간 백업 서버(2순위) 원클릭 설치 스크립트
set -euo pipefail

echo "[1/4] zstd 및 Ollama 설치 중..."
sudo apt-get update -qq && sudo apt-get install -y -qq zstd
curl -fsSL https://ollama.com/install.sh | sh

echo "[2/4] 2순위 경량 모델(qwen3:4b, qwen3:1.7b) 다운로드 중..."
ollama pull qwen3:4b
ollama pull qwen3:1.7b

echo "[3/4] Cloudflared 설치 중..."
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
    CF_BIN="cloudflared-linux-arm64"
else
    CF_BIN="cloudflared-linux-amd64"
fi
sudo curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/${CF_BIN}" -o /usr/local/bin/cloudflared
sudo chmod +x /usr/local/bin/cloudflared

echo "[4/4] 백그라운드 터널 시작 및 OLLAMA_FALLBACK_URL 확인..."
nohup cloudflared tunnel --url http://127.0.0.1:11434 --http-host-header localhost:11434 --no-autoupdate > /tmp/oracle_cloudflared.log 2>&1 &
sleep 6
grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' /tmp/oracle_cloudflared.log | head -n 1
echo "위 주소를 Streamlit Cloud Secrets의 OLLAMA_FALLBACK_URL에 등록하세요."

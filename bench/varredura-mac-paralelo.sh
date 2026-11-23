#!/usr/bin/env bash
# Segunda passada na maquina real (ADR-0027): a alavanca OLLAMA_NUM_PARALLEL.
#
#   ./varredura-mac-paralelo.sh 4     # reinicia o Ollama.app com NUM_PARALLEL=4
#   ./varredura-mac-paralelo.sh 1     # volta ao padrao (um pedido por vez)
#
# O Ollama.app no Mac le a variavel do launchd, entao e preciso reiniciar o app.
# So roda o pico (15 e 30) e o fim a fim: e onde a serializacao aparece.
set -u
NP="${1:?informe OLLAMA_NUM_PARALLEL}"
AQUI="$(cd "$(dirname "$0")" && pwd)"
RAIZ="$(cd "$AQUI/../.." && pwd)"
PY="$RAIZ/backend/.venv/bin/python"
PORTA=8011; API="http://127.0.0.1:$PORTA"; CODIGO="CARGA-8A-WXXJ"; DB="$AQUI/experimento.db"
GPU=-1; THR=8
cd "$AQUI"

if [ "$(pgrep -f 'carga.py' | grep -vc "$$")" -ne 0 ]; then echo "ABORTANDO: gerador vivo"; exit 1; fi

echo "== reiniciando Ollama com OLLAMA_NUM_PARALLEL=$NP =="
osascript -e 'quit app "Ollama"' 2>/dev/null; sleep 3
pkill -f "Ollama.app/Contents/Resources/ollama serve" 2>/dev/null; sleep 1
launchctl setenv OLLAMA_NUM_PARALLEL "$NP"
open -a Ollama; sleep 6
for _ in $(seq 1 20); do curl -fsS -m 3 http://localhost:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
# aquece: carrega o modelo com a nova configuracao
curl -s http://localhost:11434/api/chat -d '{"model":"qwen2.5:3b-instruct","messages":[{"role":"user","content":"oi"}],"stream":false,"options":{"num_gpu":-1,"num_thread":8}}' >/dev/null
echo "runner: $(pgrep -fl llama-server | grep -o -- '-np [0-9]*')"

parar_backend() { lsof -ti tcp:$PORTA 2>/dev/null | xargs kill -9 2>/dev/null || true; sleep 1; }
subir_backend() {
  parar_backend
  ( cd "$RAIZ/backend" && DATABASE_URL="sqlite:///$DB" LLM_PROVIDER=ollama \
      OLLAMA_NUM_THREAD=$THR OLLAMA_NUM_GPU=$GPU OLLAMA_MODEL=qwen2.5:3b-instruct \
      nohup ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 \
      --port $PORTA --log-level warning > "$AQUI/uvicorn-mac-np$NP.log" 2>&1 & )
  for _ in $(seq 1 20); do curl -s -m 3 "$API/api/oficinas/$CODIGO" >/dev/null 2>&1 && { echo "backend no ar"; return 0; }; sleep 1; done
  echo "backend NAO subiu"; return 1
}
rodar() { local rot="$1" reps="$2"; shift 2
  for r in $(seq 1 "$reps"); do
    $PY carga.py --label "${rot}-r${r}" --variant "$((r - 1))" --timeout 900 "$@" >/dev/null 2>&1
    echo "  ${rot}-r${r}  $(date +%H:%M:%S)"
  done; }

echo "== pico (nivel 1) com np=$NP =="
for n in 15 30; do
  rodar "mac-np$NP-pico-$(printf %02d "$n")" 3 --target ollama --students "$n" --pattern burst --num-gpu $GPU --num-thread $THR
done
echo "== fim a fim pela plataforma (nivel 2) com np=$NP =="
subir_backend || exit 1
rodar "mac-np$NP-sonda-pela-app" 2 --target platform --students 30 --pattern burst --probe --api "$API" --code "$CODIGO"
parar_backend
echo "== FIM $(date +%H:%M:%S) =="

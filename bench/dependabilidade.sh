#!/usr/bin/env bash
# Repete os cenarios de dependabilidade 3x, ANTES e DEPOIS da ADR-0024.
#
# O "antes" exige o codigo anterior a correcao, entao o script troca
# temporariamente o feedback.py pela versao do git HEAD e RESTAURA ao sair,
# inclusive em erro ou interrupcao (trap). Nada fica pela metade.
set -u

RAIZ="$(cd "$(dirname "$0")/../.." && pwd)"
BACK="$RAIZ/backend"
EXP="$RAIZ/experiments/capacidade-appliance"
PY="$BACK/.venv/bin/python"
ALVO="backend/app/services/feedback.py"
GUARDA="$(mktemp)"
DB="$EXP/experimento.db"
PORTA=8011
API="http://127.0.0.1:$PORTA"
CODIGO="CARGA-8A-WXXJ"

cp "$RAIZ/$ALVO" "$GUARDA"
echo "guarda da versao corrigida: $GUARDA"

restaurar() {
  cp "$GUARDA" "$RAIZ/$ALVO"
  echo "feedback.py RESTAURADO para a versao corrigida"
  git -C "$RAIZ" diff --stat -- "$ALVO" | tail -1
  parar_backend
}
trap restaurar EXIT INT TERM

parar_backend() { lsof -ti tcp:$PORTA 2>/dev/null | xargs kill -9 2>/dev/null || true; sleep 1; }

subir_backend() {
  parar_backend
  ( cd "$BACK" && DATABASE_URL="sqlite:///$DB" LLM_PROVIDER=ollama \
      OLLAMA_NUM_THREAD=0 OLLAMA_MODEL=qwen2.5:3b-instruct \
      nohup ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 \
      --port $PORTA --log-level warning > "$EXP/uvicorn-$1.log" 2>&1 & )
  for _ in $(seq 1 20); do
    curl -s -m 3 "$API/api/oficinas/$CODIGO" >/dev/null 2>&1 && { echo "backend no ar ($1)"; return 0; }
    sleep 1
  done
  echo "backend NAO subiu ($1)"; return 1
}

rodar() {  # rodar <rotulo> <args...>
  local rot="$1"; shift
  for r in 1 2 3; do
    ( cd "$EXP" && $PY carga.py --api "$API" --code "$CODIGO" \
        --label "${rot}-r${r}" --timeout 900 "$@" >/dev/null 2>&1 )
    echo "  ${rot}-r${r}"
  done
}

echo "=== DEPOIS da correcao (codigo atual) ==="
subir_backend depois || exit 1
rodar dep-baseline    --target ollama --students 1 --pattern spread --window 40 --probe
rodar dep-n1-depois   --target ollama --students 30 --pattern burst --probe
rodar dep-n2-depois   --target platform --students 30 --pattern burst --probe

echo "=== ANTES da correcao (feedback.py do git HEAD) ==="
git -C "$RAIZ" checkout -- "$ALVO" || exit 1
grep -q "tres sessoes curtas\|três sessões curtas" "$RAIZ/$ALVO" && {
  echo "ERRO: o HEAD ja contem a correcao; abortando para nao medir a mesma coisa duas vezes"; exit 1; }
subir_backend antes || exit 1
rodar dep-n2-antes    --target platform --students 30 --pattern burst --probe

echo "=== FIM ==="

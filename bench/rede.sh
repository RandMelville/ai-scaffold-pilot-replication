#!/usr/bin/env bash
# Emula o caminho de rede entre Chromebook e appliance (Wi-Fi da caixa).
#
#   sudo ./rede.sh ligar    [rtt_ms] [banda] [perda]
#   sudo ./rede.sh desligar
#   ./rede.sh verificar     [host] [porta]      # nao precisa de sudo
#
# Padrao: 12 ms de RTT, 100 Mbit/s, 0,1% de perda. Wi-Fi de sala com poucos
# saltos fica nessa ordem; o payload aqui e texto, entao a banda quase nunca
# limita e o que importa e latencia e jitter.
#
# ATENCAO, DUAS RESSALVAS QUE PRECISAM ESTAR NO ARTIGO:
#
# 1. LOOPBACK. No macOS o pf normalmente NAO filtra trafego de loopback. Se o
#    gerador de carga e o backend estiverem ambos em 127.0.0.1, estas regras
#    provavelmente NAO terao efeito. Rode o backend no IP de LAN da maquina
#    (--host 0.0.0.0) e aponte o gerador para esse IP. Use "verificar" para
#    confirmar que o atraso apareceu, em vez de supor.
# 2. Isto emula latencia, banda e perda. NAO emula disputa de tempo de antena
#    entre 30 clientes associados ao mesmo radio, que e efeito de camada MAC e
#    exige hardware real.
set -u

ACAO="${1:-verificar}"
RTT_MS="${2:-12}"
BANDA="${3:-100Mbit/s}"
PERDA="${4:-0.001}"
PORTA_ALVO="${PORTA_ALVO:-8011}"
PIPE=1
ANCORA="remidiacao"

case "$ACAO" in
  ligar)
    [ "$(id -u)" -eq 0 ] || { echo "precisa de sudo"; exit 1; }
    # dummynet aplica o atraso em cada sentido; metade do RTT por sentido.
    METADE=$(( RTT_MS / 2 ))
    dnctl pipe $PIPE config delay "${METADE}ms" bw "$BANDA" plr "$PERDA"

    cat > /tmp/${ANCORA}-pf.conf <<EOF
dummynet-anchor "$ANCORA"
anchor "$ANCORA"
EOF
    cat > /tmp/${ANCORA}-rules.conf <<EOF
dummynet in  proto tcp from any to any port $PORTA_ALVO pipe $PIPE
dummynet out proto tcp from any to any port $PORTA_ALVO pipe $PIPE
EOF
    pfctl -f /tmp/${ANCORA}-pf.conf
    pfctl -a "$ANCORA" -f /tmp/${ANCORA}-rules.conf
    pfctl -E
    echo "ligado: RTT ${RTT_MS}ms, banda $BANDA, perda $PERDA, porta $PORTA_ALVO"
    echo "AGORA RODE: ./rede.sh verificar <ip-de-lan> $PORTA_ALVO"
    ;;

  desligar)
    [ "$(id -u)" -eq 0 ] || { echo "precisa de sudo"; exit 1; }
    pfctl -a "$ANCORA" -F all 2>/dev/null || true
    dnctl -q flush 2>/dev/null || true
    pfctl -d 2>/dev/null || true
    echo "desligado (pipes e ancora removidos)"
    ;;

  verificar)
    HOST="${2:-127.0.0.1}"
    PORTA="${3:-$PORTA_ALVO}"
    echo "medindo RTT de aplicacao contra http://$HOST:$PORTA (10 amostras)"
    total=0; n=0
    for _ in $(seq 1 10); do
      t=$(curl -s -o /dev/null -w "%{time_total}" -m 5 \
            "http://$HOST:$PORTA/api/oficinas/NAO-EXISTE" 2>/dev/null) || continue
      ms=$(echo "$t * 1000" | bc -l 2>/dev/null | cut -d. -f1)
      [ -n "$ms" ] && { total=$((total + ms)); n=$((n + 1)); }
    done
    if [ "$n" -eq 0 ]; then
      echo "nenhuma amostra: o backend esta no ar nesse host/porta?"
      exit 1
    fi
    echo "RTT medio de aplicacao: $((total / n)) ms  (n=$n)"
    echo
    echo "Compare com a medicao SEM emulacao. Se nao mudou, a regra nao pegou"
    echo "(o caso classico e trafego de loopback, ver ressalva 1 no cabecalho)."
    ;;

  *)
    echo "uso: $0 {ligar|desligar|verificar}"; exit 1 ;;
esac

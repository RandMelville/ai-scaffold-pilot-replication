/**
 * Calibra o simulador contra o navegador de verdade.
 *
 * NAO e teste de carga. Sobe poucos Chromium headless, faz login de aluno na
 * SPA real e grava TODAS as requisicoes que o navegador dispara sozinho, com
 * horario. Serve para responder uma pergunta so: a mistura de requisicoes que
 * o carga.py gera e a mesma que a turma gera?
 *
 * O que ele valida empiricamente: os pollers de fundo da etapa de texto
 * (mediacao a cada 10 s, lista de projetos a cada 5 s ate carregar).
 * O que ele NAO cobre: a cadencia do polling de feedback, que so roda com um
 * pedido pendente. Essa foi lida direto da fonte (POLL_INTERVALO_MS = 2000 em
 * frontend/src/stores/project.ts), o que e evidencia melhor que inferencia
 * indireta.
 *
 * Pre-requisitos (nenhum e iniciado por este script, de proposito):
 *   1. backend no ar        (porta 8011, banco do experimento)
 *   2. frontend no ar       VITE_API_URL=http://localhost:8011 npm run dev
 *   3. oficina semeada      seed_experimento.py, gera oficina.json
 *
 * Uso:
 *   node calibrar-navegador.mjs [n_navegadores] [segundos] [url_frontend]
 *   node calibrar-navegador.mjs 3 60 http://localhost:5173
 */

// Playwright ja esta instalado no spike do motor de narrativa; reaproveitado
// aqui para nao duplicar ~200 MB de navegador no repositorio.
import { chromium } from "../motor-narrativa-spike/node_modules/playwright/index.mjs";
import { readFileSync, writeFileSync } from "node:fs";

const N = Number(process.argv[2] ?? 3);
const SEGUNDOS = Number(process.argv[3] ?? 60);
const URL_FRONT = process.argv[4] ?? "http://localhost:5173";

const oficina = JSON.parse(readFileSync("oficina.json", "utf8"));
if (!oficina.codinomes || oficina.codinomes.length < N) {
  console.error(`oficina.json tem ${oficina.codinomes?.length ?? 0} codinomes; pedidos ${N}`);
  process.exit(1);
}

console.log(`abrindo ${N} navegador(es) contra ${URL_FRONT}`);
console.log(`oficina ${oficina.codigo}, observando por ${SEGUNDOS}s\n`);

const registros = [];
const inicio = Date.now();
const navegador = await chromium.launch({ headless: true });

const sessoes = await Promise.all(
  Array.from({ length: N }, async (_, i) => {
    const contexto = await navegador.newContext();
    const pagina = await contexto.newPage();

    pagina.on("request", (req) => {
      const url = req.url();
      // startsWith e nao includes: em modo dev o Vite serve os proprios modulos
      // do cliente em /src/api/*.ts, que includes("/api/") capturava por engano.
      if (!new URL(url).pathname.startsWith("/api/")) return;
      registros.push({
        aluno: i,
        t: (Date.now() - inicio) / 1000,
        metodo: req.method(),
        // normaliza ids para agrupar por rota
        rota: new URL(url).pathname
          .replace(/\/[0-9a-f-]{36}/gi, "/{id}")
          .replace(/\/[A-Z]+-[A-Z0-9]+-[A-Z0-9]+/g, "/{codigo}"),
      });
    });

    await pagina.goto(URL_FRONT, { waitUntil: "domcontentloaded" });
    await pagina.fill("#codigo", oficina.codigo);
    await pagina.fill("#pseudonimo", oficina.codinomes[i]);
    await pagina.click("form:has(#codigo) button[type=submit]");
    await pagina.waitForURL(/\/student/, { timeout: 30000 }).catch(() => {
      console.warn(`  aluno ${i}: nao chegou em /student; segue observando`);
    });
    return { contexto, pagina };
  }),
);

console.log("logins feitos; observando o trafego de fundo...");
await new Promise((r) => setTimeout(r, SEGUNDOS * 1000));

for (const { contexto } of sessoes) await contexto.close();
await navegador.close();

// --- analise: cadencia por rota ------------------------------------------
const porRota = {};
for (const r of registros) {
  const chave = `${r.metodo} ${r.rota}`;
  (porRota[chave] ??= []).push(r);
}

console.log(`\n${registros.length} requisicoes de API em ${SEGUNDOS}s, ${N} aluno(s)\n`);
console.log("rota".padEnd(46), "total".padStart(6), "por aluno".padStart(10), "intervalo".padStart(11));
for (const [chave, lista] of Object.entries(porRota).sort((a, b) => b[1].length - a[1].length)) {
  const porAluno = {};
  for (const r of lista) (porAluno[r.aluno] ??= []).push(r.t);
  const intervalos = [];
  for (const ts of Object.values(porAluno)) {
    ts.sort((a, b) => a - b);
    for (let i = 1; i < ts.length; i++) intervalos.push(ts[i] - ts[i - 1]);
  }
  const medio = intervalos.length
    ? (intervalos.reduce((a, b) => a + b, 0) / intervalos.length).toFixed(1) + "s"
    : "uma vez";
  console.log(
    chave.slice(0, 46).padEnd(46),
    String(lista.length).padStart(6),
    (lista.length / N).toFixed(1).padStart(10),
    medio.padStart(11),
  );
}

writeFileSync(
  "resultados/calibracao-navegador.json",
  JSON.stringify({ n_navegadores: N, segundos: SEGUNDOS, registros }, null, 2),
);
console.log("\nbruto em resultados/calibracao-navegador.json");
console.log("Compare a coluna 'intervalo' com --poll-interval e --mediation-interval do carga.py.");

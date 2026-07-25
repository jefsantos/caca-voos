# Caça-Voos Agente (SerpAPI / Google Flights)

Agente autônomo que monitora passagens **GRU → Itália** (Roma/FCO, Milão/MXP, Veneza/VCE) para setembro de 2026 — ida entre **08/09 e 12/09**, estadia de **5 ou 6 dias**. Usa a **SerpAPI** para consultar o **Google Flights** (a fonte mais confiável de preços), guarda histórico e notifica por **e-mail** e/ou **WhatsApp** quando:

- o preço fica **igual ou abaixo do teto** (`PRICE_CEILING`), ou
- surge uma **nova mínima histórica** para uma rota.

## Como o rodízio funciona

O plano gratuito da SerpAPI dá ~100 buscas/mês. Por isso o agente roda **1x por dia** e verifica **3 combinações por vez, em rodízio** — as 30 combinações (5 dias de ida × 2 durações × 3 destinos) são todas cobertas a cada 10 dias, gastando ~90 buscas/mês. O dashboard sempre mostra a última oferta conhecida de cada rota, com selo nas verificadas hoje.

> Complemento recomendado: crie também um alerta nativo no Google Flights (gratuito e em tempo real). O agente é o painel organizado; o alerta nativo é o radar contínuo.

## Fluxo completo

```
GitHub Actions (cron diário 9h BRT)
        │
        ▼
caca_voos.py ──► SerpAPI ──► Google Flights (3 rotas/dia em rodízio)
        │
        ├─► price_history.json  (histórico + estado do rodízio, commitado)
        ├─► docs/data.json      (dados do painel, commitado)
        └─► e-mail / WhatsApp com o LINK do dashboard quando há alerta
                                        │
                                        ▼
                     GitHub Pages serve docs/index.html (React)
                     → você clica no link do e-mail e vê tudo em tela
```

## Rodando local (teste rápido)

```bash
pip install requests
cp .env.example .env        # preencha suas credenciais
export $(grep -v '^#' .env | xargs)
python caca_voos.py --dry-run
```

## Setup completo no GitHub

1. Crie um repositório **público** (Pages gratuito exige repo público) e suba estes arquivos.
2. *Settings → Secrets and variables → Actions → Secrets*: cadastre `SERPAPI_KEY` e os canais de notificação (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO` e/ou os `EVOLUTION_*` + `WHATSAPP_NUMBER`).
3. *Aba Variables*: cadastre `DASHBOARD_URL` = `https://SEU_USUARIO.github.io/NOME_DO_REPO/`. Opcional: `PRICE_CEILING`, `DESTINATIONS`, `SEARCHES_PER_RUN` etc.
4. *Settings → Pages*: Source **Deploy from a branch** → branch `main`, pasta **/docs** → Save.
5. Teste: aba *Actions → Caça-Voos diário → Run workflow*.

## Credencial SerpAPI

1. Conta gratuita em https://serpapi.com (só e-mail, sem cartão).
2. A chave fica no painel, em **Your Private API Key** — esse valor é o `SERPAPI_KEY`.
3. Acompanhe o consumo da cota no próprio painel da SerpAPI.

## Notificação por e-mail com Gmail

Ative verificação em 2 etapas e gere uma **senha de app** em https://myaccount.google.com/apppasswords — use-a no `SMTP_PASSWORD`.

## Ajustes comuns

| O quê | Onde |
|---|---|
| Teto de alerta | `PRICE_CEILING` |
| Destinos | `DESTINATIONS` (códigos IATA separados por vírgula) |
| Janela de ida | `DEPARTURE_START` / `DEPARTURE_END` |
| Duração da viagem | `STAY_DAYS` (ex.: `5,6,7`) |
| Buscas por dia | `SEARCHES_PER_RUN` (atenção à cota mensal) |
| Resumo diário mesmo sem alerta | descomente a linha indicada no final de `run()` em `caca_voos.py` |

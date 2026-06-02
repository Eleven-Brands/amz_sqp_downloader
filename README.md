# SQP Downloader

Automação para download do **Search Query Performance (SQP)** do Amazon Brand Analytics.
Baixa dados de todos os marketplaces via Playwright, consolida em CSV e salva no Google Drive.

---

## Como funciona

O script faz login no Seller Central com um perfil Chrome persistente, navega para a página
do SQP e usa a **API interna do Brand Analytics** para buscar os dados diretamente (mesma
API que a extensão Seller Utilities usa). Sem esperar pelo Download Manager.

### Fluxo por ASIN/semana

1. Navega para o SQP URL com params de marketplace e semana
2. POST para `/api/brand-analytics/v1/dashboard/query-performance/reports` com CSRF token
3. Resposta JSON → CSV com 35 colunas (inclui preços medianos e shipping speeds)
4. Se a API retornar 0 linhas → arquivo só com header (sem dados nessa semana)
5. Se a API falhar → fallback para scraping DOM (sem colunas de preço)

---

## Setup inicial (uma vez por PC)

```bash
# Instalar dependências
pip install -r requirements.txt
playwright install chromium

# (Opcional) Configurar variáveis de ambiente — copie e edite
cp .env.example .env

# Login NA (US/CA/MX)
python main.py setup --marketplace US

# Login EU (DE/FR/IT/ES/GB/NL/SE)
python main.py setup --marketplace DE
```

O login fica salvo em `SQP_LOCAL_DIR\chrome_profile\` (padrão: `C:\SQP\chrome_profile\`).
O perfil EU serve todos os marketplaces EU.

### Variáveis de ambiente (`.env`)

| Variável | Padrão | Descrição |
|---|---|---|
| `SQP_LOCAL_DIR` | `C:\SQP` | Pasta local para Chrome profile, logs e downloads temporários |
| `SQP_SERVICE_ACCOUNT` | `../return_badge_predictor/service_account.json` | Caminho do service account BigQuery |
| `GMAIL_FROM` | — | E-mail remetente para notificações (opcional) |
| `GMAIL_APP_PASSWORD` | — | App Password do Gmail (opcional) |
| `NOTIFY_EMAIL` | — | E-mail destinatário das notificações (opcional) |

O código-fonte resolve `DRIVE_BASE` automaticamente via `Path(__file__).parent` — não precisa configurar o caminho do Drive.

---

## Comandos principais

```bash
# Smoke test — 1 ASIN, última semana
python main.py test --marketplace DE

# Download semanal (todos os marketplaces)
python main.py weekly

# Quando NA e EU estão em semanas diferentes (comum — EU costuma atrasar 1 semana)
python main.py weekly --na-week 2026-05-24 --eu-week 2026-05-17

# Só uma região
python main.py weekly --marketplace US CA MX --na-week 2026-05-24
python main.py weekly --marketplace DE ES FR GB IT NL SE --eu-week 2026-05-17

# Backfill — intervalo de datas
python main.py backfill --from-date 2025-12-28
python main.py backfill --from-date 2025-12-28 --marketplace DE FR IT

# Só consolidar os CSVs existentes
python main.py process
```

---

## Estrutura de arquivos

```
sqp_downloader/
├── config.py           — paths, marketplaces, helpers de semana Amazon (Dom-Sab)
├── bq_client.py        — ASINs ativos por marketplace/data (BigQuery)
├── downloader.py       — Playwright + API fetch + fallback DOM scrape
├── tratamento.py       — consolida raw/*.csv → processed/resultado_final.csv
├── main.py             — CLI orquestrador
├── notifier.py         — email via Gmail SMTP (requer GMAIL_APP_PASSWORD)
├── notify_when_done.py — popup Windows quando backfill termina
├── run_weekly.bat      — entry point do Task Scheduler (usa %~dp0, portável)
├── .env                — variáveis de ambiente locais (não versionado)
├── .env.example        — template de configuração
│
├── raw/{marketplace}/  — CSVs brutos: {ASIN}_{YYYY-MM-DD}.csv (um por ASIN/semana)
├── processed/          — resultado_final.csv (consolidado, pronto para BI)
├── state/progress.json — rastreia o que já foi baixado (permite retomar backfill)
└── debug/              — screenshots Playwright para diagnóstico

{SQP_LOCAL_DIR}/        — local, não sincronizado com Drive (padrão: C:\SQP)
├── chrome_profile/     — sessão Chrome persistente
└── logs/sqp.log        — log de execução
```

---

## Formato do CSV de saída

Cada arquivo em `raw/` tem 35 colunas (formato Seller Utilities):

```
ASIN, Search Query, Search Query Score, Search Query Volume,
Impressions: Total Count, Impressions: ASIN Count, Impressions: ASIN Share %,
Clicks: Total Count, Clicks: Click Rate %, Clicks: ASIN Count, Clicks: ASIN Share %,
Clicks: Price (Median), Clicks: ASIN Price (Median),
Clicks: Same Day Shipping Speed, Clicks: 1D Shipping Speed, Clicks: 2D Shipping Speed,
Cart Adds: [mesmas 9 colunas acima],
Purchases: [mesmas 9 colunas acima],
Marketplace, Reporting Date
```

O `resultado_final.csv` tem colunas renomeadas para o schema BI + `year`, `week`,
`start_date`, `end_date`, `country_code`, `price_currency`.

---

## API do Brand Analytics (descoberta via Seller Utilities)

```
POST {sc_url}/api/brand-analytics/v1/dashboard/query-performance/reports

Headers:
  Anti-Csrftoken-A2z: {token do <meta name="anti-csrftoken-a2z">}
  Content-Type: application/json

Body:
{
  "filterSelections": [
    {"id": "asin",            "value": "{ASIN}",      "valueType": "ASIN"},
    {"id": "reporting-range", "value": "weekly",       "valueType": null},
    {"id": "weekly-week",     "value": "{YYYY-MM-DD}", "valueType": "weekly"}
  ],
  "reportId": "query-performance-asin-report-table",
  "reportOperations": [{"ascending": true, "pageNumber": 1, "pageSize": 100,
                         "reportId": "query-performance-asin-report-table",
                         "reportType": "TABLE", "sortByColumnId": "qp-asin-query-rank"}],
  "selectedCountries": ["{country_id}"],
  "viewId": "query-performance-asin-view"
}

Resposta: {"reportsV2": [{"rows": [{...}, ...], "totalItems": N, ...}]}
```

`weekly-week` = sábado que encerra a semana (ws + 6 dias).
O fetch roda via `page.evaluate()` para herdar cookies de sessão automaticamente.

---

## Sessão e autenticação

- Login manual uma vez via `python main.py setup --marketplace {US|DE}`
- A sessão EU (`.co.uk`) cobre DE, FR, IT, ES, GB, NL, SE com um único perfil
- A sessão expira eventualmente (horas/dias) — sintoma: `Session expired` no log
- Sem 2FA ativo → App Password do Gmail não disponível → notificações só via popup local

---

## Marketplaces

| Código | Região | SQP disponível |
|--------|--------|----------------|
| US, CA, MX | NA | Sim |
| DE, FR, IT, ES, GB, NL, SE | EU | Sim |
| IE, BE, PL | EU | **Não** — retorna "Page not found" |

Fonte dos ASINs: `amazon-sp-api-openbridge.2_Silver_Aux.vw_all_listings_report`

---

## Semanas Amazon

Amazon usa semanas Dom–Sab. A URL do SQP usa o **sábado** como referência.

```python
week_start(d)        # domingo que abre a semana contendo d
last_available_week() # última semana completa com dados (~1-2 dias de lag)
# Ex: hoje 2026-05-29 → retorna 2026-05-17
```

---

## Task Scheduler

- Nome: `SQP_Weekly_Download`
- Horário: toda segunda-feira às 12:00
- Comando: `run_weekly.bat` (na raiz do repo — portável, sem Python hardcoded)
- XML de configuração: `{SQP_LOCAL_DIR}\sqp_weekly_task.xml`
- Requer usuário logado (Playwright abre browser visível)

Para recriar em um PC novo:
```powershell
schtasks /Create /XML "C:\SQP\sqp_weekly_task.xml" /TN "SQP_Weekly_Download"
```

---

## Estado atual (2026-05-29)

| Marketplace | Linhas | Período |
|-------------|--------|---------|
| US | ~314k | jan/2025 → mai/2026 (72 semanas) |
| CA | ~38k | dez/2025 → mai/2026 (20 semanas) |
| DE | ~30k | dez/2025 → mai/2026 (20 semanas) |
| GB | ~26k | dez/2025 → mai/2026 (20 semanas) |
| MX | ~18k | dez/2025 → mai/2026 (20 semanas) |
| IT | ~11k | dez/2025 → mai/2026 (20 semanas) |
| ES | ~8k | dez/2025 → mai/2026 (21 semanas) |
| FR | ~4k | dez/2025 → mai/2026 (20 semanas) |
| NL | ~1.5k | dez/2025 → mai/2026 (20 semanas) |
| SE | ~4 | parcial (volume SQP muito baixo) |

US tem histórico mais longo pois foi importado do Seller Utilities (`import_su_history.py`).
Total: ~450k linhas em `processed/resultado_final.csv`.

---

## Notificações

Ao final de cada execução semanal, o script envia uma mensagem no canal **#teste-automate** do ClickUp.

| Evento | Mensagem |
|---|---|
| Download concluído | ✅ SQP Download concluído — semana `YYYY-MM-DD` + marketplaces |
| Sessão expirada | ⚠️ Sessão expirada em `MKT` + instrução de qual `setup` rodar |

**Configuração:** adicione `CLICKUP_API_KEY` no `.env` (ver `.env.example`).
Sem a key configurada, a notificação é ignorada silenciosamente.

---

## Problemas conhecidos

| Problema | Causa | Solução |
|----------|-------|---------|
| `Session expired` | Cookie expirou | `python main.py setup --marketplace DE` |
| `API error: Failed to fetch` | Conexão instável | O script retenta na próxima execução |
| `API error: http_400` | Formato do request errado | Verificar body da API (ver seção acima) |
| `Table not found on page 1` | ASIN sem dados (fallback DOM) | Normal — ASIN não tem SQP nessa semana |
| `DateParseError` em tratamento.py | `ob_date` usa `_week_date` do filename | Já corrigido — não usar coluna "Reporting Date" |
| `ERR_NETWORK_IO_SUSPENDED` | PC entrou em suspensão | Desativar suspensão durante backfills longos |

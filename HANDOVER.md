# SQP Downloader — Handover

> **Prompt para o próximo chat:**
>
> Durante essa conversa, me avise quando perceber que está com dificuldade de lembrar decisões tomadas anteriormente, ou quando o escopo da conversa mudar significativamente. Nesses momentos, diga explicitamente: "⚠️ Considere fazer handover para um novo chat."
>
> **Cost-aware:** opere sempre dentro dos rate limits da subscription. Não execute comandos longos em paralelo desnecessariamente, prefira leituras cirúrgicas de arquivo (offset/limit) a ler arquivos inteiros, e evite loops de teste repetidos sem mudança de código. Se perceber que a conversa está consumindo muito contexto sem progresso, sugira handover imediatamente. Nunca use overages mesmo que a configuração da conta permita.

---

## Contexto do projeto

O **Search Query Performance (SQP)** é um relatório do Amazon Seller Central que mostra performance de queries de busca por ASIN. O download manual é inviável pelo volume (13 marketplaces, ~939 ASINs ativos).

**Objetivo:** automação semanal que baixa o SQP de todos os marketplaces, consolida em CSV e salva no Google Drive.

Para detalhes completos de arquitetura, formato de API, estrutura de arquivos e comandos, ver **README.md** na mesma pasta.

---

## Como o download funciona (sem API key)

O script usa **Playwright** para abrir o Chrome com um perfil persistente (sessão de login salva). Dentro da página do Seller Central, executa `page.evaluate()` para fazer um `fetch()` que herda os cookies automaticamente — sem precisar de API key oficial.

O endpoint foi descoberto via reverse-engineering da extensão Seller Utilities (`951f03e5b050c9f7.js`). A sessão expira em horas/dias — sintoma: `Session expired` no log.

---

## Decisões de arquitetura

| Decisão | Escolha | Motivo |
|---------|---------|--------|
| Automação | Playwright (browser visível) | Sem acesso à SP-API; login manual necessário |
| Método de download | API interna Brand Analytics | DOM scrape lento (~90s/ASIN); DM assíncrono demora >60min |
| Sessão | Perfil Chrome persistente em `C:\SQP\chrome_profile\` | Login manual uma única vez por região |
| ASINs | BigQuery em tempo real (`vw_all_listings_report`) | Lista sempre atualizada |
| Saída | CSV consolidado em `processed/resultado_final.csv` | Pedido do usuário |
| Fallback | DOM scrape | Usado apenas quando API retorna erro (não quando retorna 0 linhas) |

---

## Estado atual (2026-05-27)

### Backfill completo
`processed/resultado_final.csv` tem **450.632 linhas**:

| Marketplace | Linhas | Período |
|-------------|--------|---------|
| US | 314.380 | jan/2025 → mai/2026 (72 semanas) |
| CA | 38.398 | dez/2025 → mai/2026 (20 semanas) |
| DE | 29.818 | dez/2025 → mai/2026 (20 semanas) |
| GB | 25.944 | dez/2025 → mai/2026 (20 semanas) |
| MX | 17.822 | dez/2025 → mai/2026 (20 semanas) |
| IT | 10.693 | dez/2025 → mai/2026 (20 semanas) |
| ES | 8.019 | dez/2025 → mai/2026 (21 semanas) |
| FR | 4.042 | dez/2025 → mai/2026 (20 semanas) |
| NL | 1.512 | dez/2025 → mai/2026 (20 semanas) |
| SE | 4 | parcial (sessão expirou, volume SQP muito baixo) |

US tem histórico mais longo pois foi importado do Seller Utilities (`import_su_history.py`).

### Task Scheduler
- `SQP_Weekly_Download` — toda segunda-feira às 08:00
- XML: `C:\SQP\sqp_weekly_task.xml`

---

## Bugs corrigidos nesta sessão

| Bug | Causa raiz | Correção |
|-----|-----------|----------|
| Colunas duplicadas `.1` no resultado_final | `tratamento.py` adicionava `df["asin"] = asin` mesmo com coluna `ASIN` já no CSV; concat de arquivos API+DOM gerava colunas com mesmo nome | Drop de `ASIN`/`Marketplace`/`Reporting Date` antes do concat + coalescência de colunas duplicadas (`bfill`) |
| Linhas NaN no resultado_final | DOM scrape produzia linhas vazias; tratamento.py não deduplicava | `import_su_history.py` fez dedup por `country_code+asin+search_query+ob_date` |

---

## Problemas conhecidos / histórico de bugs

| Bug | Causa raiz | Correção aplicada |
|-----|-----------|-------------------|
| HTTP 400 da API | Body errado: faltavam `reportId`, `reportOperations`, `viewId` | Corrigido em `_fetch_sqp_api` (2026-05-19) |
| Fallback DOM em ASIN sem dados | `_fetch_sqp_api` retornava `False` para 0 linhas | 0 linhas -> escreve header-only + retorna `True` (2026-05-19) |
| `DateParseError` em tratamento.py | Coluna `Reporting Date` formato SU (`"2026-05-10 to 2026-05-16"`) | `tratamento.py` usa `_week_date` do filename (2026-05-20) |
| Sessão EU expira em horas/dias | Cookie do `.co.uk` tem vida curta | Renovar com `python main.py setup --marketplace DE` |
| `ERR_NETWORK_IO_SUSPENDED` | PC entrou em modo de suspensão durante backfill | Desativar suspensão no Windows durante backfills longos |
| Drive G:\ inacessível | Google Drive desconectou momentaneamente | Re-rodar; `progress.json` retoma de onde parou |

---

## Próximos passos

1. **Carregar no BigQuery/Power BI** — `processed/resultado_final.csv` está pronto
2. **SE backfill opcional** — SE tem volume SQP muito baixo (4 linhas em 7 semanas); provavelmente não vale esforço
3. **Manutenção semanal** — Task Scheduler cuida automaticamente; renovar sessão se aparecer `Session expired`

---

## Informações de acesso

- **Seller Central:** lucca@11brands.com (sem 2FA)
- **BigQuery:** service account em `code_repository/return_badge_predictor/service_account.json`
- **Máquina:** Windows, sempre ligada
- **Drive:** `G:\` = Shared Drive OrganiHaus
- **Chrome profile:** `C:\SQP\chrome_profile\`
- **Logs:** `C:\SQP\logs\sqp.log`

# Robô de Monitoramento de Estoque — Qualy

Sistema que coleta dados da API D&O (Verleih) 3x/dia, salva snapshots num Postgres do Supabase, e expõe via dashboard Streamlit com chat IA (Google Gemini) que responde perguntas analíticas consultando o banco sob demanda.

- **Coletor** (`coletor.py`): roda via GitHub Actions, seg-sáb, 08h/12h/18h SP.
- **Dashboard** (`dashboard.py`): hospedado em https://qualy-estoque.streamlit.app.
- **Chat IA** (`ai_chat.py`): painel direito do dashboard. Consulta SQL read-only sob demanda + memória persistente entre sessões.

## Setup local

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# preencha as variáveis no .env (veja descrição em cada linha)
streamlit run dashboard.py
```

## Variáveis de ambiente

Veja `.env.example` pra lista completa. As que precisam ser obtidas externamente:

| Var | Onde obter |
|---|---|
| `DOSISTEMAS_TOKEN` | Rodrigo @ D&O (JWT Bearer) |
| `SUPABASE_DB_PASSWORD` | Supabase dashboard → Project Settings → Database |
| `GOOGLE_API_KEY` | https://aistudio.google.com/app/apikey (grátis) |

`DATABASE_URL` e `DATABASE_URL_READONLY` são montadas a partir das demais — formato no `.env.example`.

## Chat IA — como ativar

O chat aparece no painel direito do dashboard em todas as 4 páginas.

1. **Aplicar migration** (uma vez):
   ```
   supabase link --project-ref <SEU_REF>
   supabase db query --linked -f migrations/001_chat_ia.sql
   ```

2. **Criar role read-only** (uma vez, senha gerada localmente):
   ```
   IA_PW="$(openssl rand -hex 32)"
   supabase db query --linked "
     create role ia_readonly with login password '$IA_PW';
     grant usage on schema public to ia_readonly;
     grant select on
       v_saldo_atual, v_mudancas, v_resumo_runs,
       items, stock_snapshots, stock_movements,
       routes, route_items, stock_changes, api_runs,
       chat_sessions, chat_messages, chat_session_summaries
     to ia_readonly;
   "
   echo "DATABASE_URL_READONLY=postgresql://ia_readonly.<SEU_REF>:$IA_PW@aws-1-sa-east-1.pooler.supabase.com:5432/postgres" >> .env
   ```

3. **Preencher `GOOGLE_API_KEY`** no `.env` local.

4. **No Streamlit Cloud**: Settings → Secrets → adicionar em formato TOML:
   ```toml
   GOOGLE_API_KEY = "..."
   DATABASE_URL_READONLY = "..."
   ```
   As outras vars (`DATABASE_URL`, `DOSISTEMAS_TOKEN`, etc.) também precisam estar lá pro dashboard rodar.

## Custo do chat

- Modelo: Gemini 2.5 Flash (`gemini-2.5-flash`)
- Preço (verificar em https://ai.google.dev/pricing): ~$0.075/MTok in, ~$0.30/MTok out
- **Cap mensal hardcoded: $5.00 USD**. Bloqueio automático quando atingir. Trocar valor em `ai_chat.py` → `CAP_MENSAL_USD`.
- Rodapé do painel mostra sempre: `Mês: $X.XX / $5.00`.

## Segurança do chat (defesa em profundidade)

A IA tem 1 ferramenta: `run_query(sql)`. Três camadas a impedem de fazer estrago:

1. **Role Postgres `ia_readonly`**: só `SELECT` em 13 tabelas/views nomeadas. Sem `raw_api_responses` (tem PII bruta da API D&O).
2. **Filtro Python**: `run_query` rejeita queries que não começam com `SELECT` ou `WITH`.
3. **`LIMIT 200` automático**: se a query não tem LIMIT, o sistema adiciona, evitando responses gigantes.

Quando uma nova tabela for criada no Supabase, ela **não** fica visível pra IA por padrão — precisa adicionar manualmente ao GRANT e à constante `TABELAS_EXPOSTAS` em `ai_chat.py`.

## Memória persistente

Cada sessão de chat (`chat_sessions` no banco) acumula mensagens (`chat_messages`). 30 min depois da última mensagem, no próximo boot do dashboard, a sessão é **destilada**: uma chamada extra ao Gemini gera um resumo de até ~2000 tokens em `chat_session_summaries`. O resumo mais recente é injetado no system prompt da próxima sessão — a IA "lembra" preferências, termos internos, perguntas frequentes.

Pra apagar memória: `delete from chat_session_summaries`.

## Coletor — agendamento

Configurado em `.github/workflows/coletor.yml`. Cron em UTC:
- `0 11,15,21 * * 1-6` = 8h/12h/18h SP, segunda a sábado (pula domingo).

Secrets do Actions (já configurados):
- `DOSISTEMAS_TOKEN`
- `DATABASE_URL` (usa o pooler Supabase pra funcionar em IPv4)

Pra rodar manualmente: Actions → coletor-3x-dia → Run workflow.

"""
Chat IA do dashboard — cliente Gemini + tool SQL read-only + memória persistente.

Responsabilidades:
  - run_query(sql)             : única ferramenta exposta à IA. Roda SELECT no
                                  role ia_readonly do Supabase. Defesa em
                                  profundidade: role + prefixo + LIMIT auto.
  - send_message(...)          : envia uma mensagem ao Gemini e devolve stream
                                  de chunks de texto.
  - criar_sessao / salvar_mensagem / carregar_ultimo_resumo : persistência.
  - destilar_resumo_pendentes(): roda no boot, transforma conversas antigas
                                  em resumo destilado pra próximas sessões.
  - verificar_cap / registrar_uso : cap mensal hardcoded em $5,00 USD.

Vars de ambiente necessárias:
  GOOGLE_API_KEY         — https://aistudio.google.com/app/apikey
  DATABASE_URL_READONLY  — postgres com role ia_readonly (lista branca de tabelas)
  DATABASE_URL           — postgres admin (escreve nas tabelas chat_*)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Generator, Optional
from uuid import UUID

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

# ============================================================
# Constantes
# ============================================================
MODEL = "gemini-2.5-flash"

# Preços Gemini 2.5 Flash (https://ai.google.dev/pricing — verificar a cada trimestre).
# Atualizado em 2026-05-22. Trocar quando Google mudar.
PRICE_INPUT_PER_MTOK = 0.075   # USD por milhão de tokens de entrada
PRICE_OUTPUT_PER_MTOK = 0.30   # USD por milhão de tokens de saída

# Cap mensal hardcoded. Pra trocar, editar aqui — sem override em runtime no MVP.
CAP_MENSAL_USD = 5.00

# Lista branca de tabelas/views que a IA pode listar no schema resumo.
# DEVE ser igual ao GRANT da migration 001. Mantém em sync manualmente.
TABELAS_EXPOSTAS = (
    "v_saldo_atual", "v_mudancas", "v_resumo_runs",
    "items", "stock_snapshots", "stock_movements",
    "routes", "route_items", "stock_changes", "api_runs",
    "chat_sessions", "chat_messages", "chat_session_summaries",
)

# Sessões com last_message_at mais antigo que isto sofrem destilação no próximo boot.
INATIVIDADE_DESTILAR_MINUTOS = 30

# Truncar tabelas grandes no retorno do run_query (proteção UI + custo de tokens).
MAX_LINHAS_RUN_QUERY = 200
MAX_CHARS_RUN_QUERY = 4000


# ============================================================
# Conexões — funções pequenas pra ficar fácil mockar
# ============================================================
def _admin_conn():
    """Conexão psycopg como postgres admin (escreve em chat_*)."""
    return psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15)


def _readonly_conn():
    """Conexão psycopg como ia_readonly (SELECT em lista branca apenas)."""
    return psycopg.connect(os.environ["DATABASE_URL_READONLY"], connect_timeout=15)


# ============================================================
# Tool exposta à IA: run_query(sql)
# ============================================================
def run_query(sql: str) -> str:
    """Executa uma consulta SQL SELECT no banco Postgres e devolve o resultado.

    Use para responder perguntas sobre os dados do dashboard (estoque, rotas,
    movimentações, mudanças detectadas, saúde do robô coletor). Apenas SELECT
    é permitido — qualquer outra operação retorna ERRO. Tabelas/views
    disponíveis são listadas no system prompt.

    Args:
        sql: consulta SQL completa começando com SELECT. Se a consulta retornar
             muitas linhas, adicione LIMIT — o sistema adiciona LIMIT 200 se
             nenhum LIMIT for declarado.

    Returns:
        String com o resultado formatado como tabela markdown, ou mensagem de
        erro começando com "ERRO:" se a query falhar.
    """
    sql_limpo = sql.strip().rstrip(";").strip()
    sql_baixo = sql_limpo.lower().lstrip()

    if not sql_baixo.startswith(("select", "with")):
        return "ERRO: só queries SELECT (ou WITH ... SELECT) são permitidas."

    # LIMIT automático se a query não tem um. Evita IA pedir 11k linhas.
    if " limit " not in sql_baixo:
        sql_limpo = f"{sql_limpo} limit {MAX_LINHAS_RUN_QUERY}"

    try:
        with _readonly_conn() as conn, conn.cursor() as cur:
            cur.execute(sql_limpo)
            cols = [c.name for c in cur.description] if cur.description else []
            rows = cur.fetchall()
    except psycopg.errors.InsufficientPrivilege as exc:
        return f"ERRO: permissão negada. Possivelmente acessando tabela fora da lista branca. ({exc})"
    except psycopg.Error as exc:
        return f"ERRO: {type(exc).__name__}: {str(exc)[:300]}"
    except Exception as exc:
        return f"ERRO inesperado: {type(exc).__name__}: {str(exc)[:300]}"

    if not rows:
        return "(consulta sem resultados)"

    df = pd.DataFrame(rows, columns=cols)
    md = df.to_markdown(index=False)

    if len(md) > MAX_CHARS_RUN_QUERY:
        md = md[:MAX_CHARS_RUN_QUERY] + f"\n... (truncado em {MAX_CHARS_RUN_QUERY} chars — {len(df)} linhas no total)"

    return md


# ============================================================
# Schema dinâmico — gerado uma vez por sessão e cacheado pelo chamador
# ============================================================
def gerar_schema_resumo() -> str:
    """Lê information_schema e devolve string compacta com colunas das tabelas
    da lista branca. Formato: `tabela.coluna: tipo` por linha, separadas por
    linha em branco entre tabelas.
    """
    sql = """
        select table_name, column_name, data_type
        from information_schema.columns
        where table_schema = 'public'
          and table_name = any(%s)
        order by table_name, ordinal_position
    """
    with _admin_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (list(TABELAS_EXPOSTAS),))
        rows = cur.fetchall()

    blocos: dict[str, list[str]] = {}
    for tabela, coluna, tipo in rows:
        blocos.setdefault(tabela, []).append(f"  {coluna}: {tipo}")

    partes = []
    for tabela in TABELAS_EXPOSTAS:
        if tabela in blocos:
            partes.append(f"{tabela}\n" + "\n".join(blocos[tabela]))
    return "\n\n".join(partes)


# ============================================================
# Persistência de sessões e mensagens
# ============================================================
def criar_sessao() -> UUID:
    with _admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "insert into chat_sessions (started_at) values (now()) returning id"
        )
        return cur.fetchone()[0]


def salvar_mensagem(
    session_id: UUID,
    role: str,
    content: Optional[str],
    tool_calls: Optional[list] = None,
    tokens_input: int = 0,
    tokens_output: int = 0,
) -> None:
    with _admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into chat_messages (session_id, role, content, tool_calls,
                                       tokens_input, tokens_output)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (
                session_id,
                role,
                content,
                Jsonb(tool_calls) if tool_calls else None,
                tokens_input,
                tokens_output,
            ),
        )
        cur.execute(
            "update chat_sessions set last_message_at = now() where id = %s",
            (session_id,),
        )


def carregar_ultimo_resumo() -> str:
    """Pega o resumo destilado mais recente de qualquer sessão encerrada.
    Vazio na primeira execução (sem sessões antigas).
    """
    with _admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "select summary from chat_session_summaries order by created_at desc limit 1"
        )
        row = cur.fetchone()
        return row[0] if row else ""


def carregar_historico_sessao(session_id: UUID) -> list[dict]:
    """Recarrega o histórico (user + assistant text) de uma sessão.
    Usado quando o Streamlit reinicia (page reload) e perde o session_state."""
    with _admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select role, content
            from chat_messages
            where session_id = %s
              and role in ('user', 'assistant')
              and content is not null
            order by created_at
            """,
            (session_id,),
        )
        return [{"role": r, "content": c} for r, c in cur.fetchall()]


# ============================================================
# Cap mensal de gasto Gemini
# ============================================================
def _mes_corrente() -> str:
    return datetime.now().strftime("%Y-%m")


def verificar_cap() -> tuple[bool, float]:
    """Retorna (disponivel, gasto_atual_usd) do mês corrente."""
    with _admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "select custo_usd from chat_usage_mensal where mes_ano = %s",
            (_mes_corrente(),),
        )
        row = cur.fetchone()
    gasto = float(row[0]) if row else 0.0
    return (gasto < CAP_MENSAL_USD), gasto


def calcular_custo(tokens_in: int, tokens_out: int) -> float:
    return (
        tokens_in  * PRICE_INPUT_PER_MTOK  / 1_000_000
        + tokens_out * PRICE_OUTPUT_PER_MTOK / 1_000_000
    )


def registrar_uso(tokens_in: int, tokens_out: int) -> None:
    custo = calcular_custo(tokens_in, tokens_out)
    with _admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into chat_usage_mensal (mes_ano, tokens_in, tokens_out, custo_usd, atualizado_em)
            values (%s, %s, %s, %s, now())
            on conflict (mes_ano) do update set
                tokens_in     = chat_usage_mensal.tokens_in  + excluded.tokens_in,
                tokens_out    = chat_usage_mensal.tokens_out + excluded.tokens_out,
                custo_usd     = chat_usage_mensal.custo_usd  + excluded.custo_usd,
                atualizado_em = now()
            """,
            (_mes_corrente(), tokens_in, tokens_out, custo),
        )


# ============================================================
# System prompt
# ============================================================
def build_system_prompt(schema_str: str, summary_str: str) -> str:
    base = f"""\
Você é o assistente do dashboard de estoque da Qualy. Responde em pt-BR, direto e objetivo. Cita números, comparações, e listagens curtas extraídas do banco. Quando precisar de dados, USE a ferramenta `run_query` com SQL SELECT — não invente números.

Domínio:
- Verleih é o sistema de gestão de aluguel de equipamentos da Qualy.
- Equipamentos têm 3 níveis: GR (grupo, agregado), SG (subgrupo, onde fica o saldo real), IT (patrimônio individual).
- Coletor roda 3x/dia (8h, 12h, 18h SP, seg-sáb), salva snapshots e detecta mudanças.
- Mudanças têm status_explicacao: explicada (com evento Compra/Baixa/Saída), inferida (devolução/manutenção sem evento), nao_explicada (suspeita).
- "Divergência" = mudança nao_explicada.

Regras:
- Sempre que o usuário pedir dado numérico, comparação, ou listagem: chame run_query.
- Se a query retornar ERRO, NÃO invente o resultado. Diga que não conseguiu obter o dado e proponha alternativa.
- Use markdown leve (negrito, listas) e evite tabelas longas — prefira sumarizar.
- Nunca exponha CNPJ ou nomes pessoais completos em respostas. Use só razão social ou primeiro nome quando relevante.
- Pra perguntas sobre o próprio chat (\"do que falamos antes?\"), consulte chat_sessions/chat_messages/chat_session_summaries.

Tabelas/views disponíveis:

{schema_str}
"""
    if summary_str:
        base += f"\n\nMemória de conversas anteriores (uso interno, não cite literalmente):\n{summary_str}"
    return base


# ============================================================
# Cliente Gemini — instanciado sob demanda (evita travar boot sem key)
# ============================================================
def _client():
    from google import genai  # lazy import — pacote pode não estar instalado
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY não configurada. Veja README.md.")
    return genai.Client(api_key=api_key)


def configurada() -> bool:
    """True quando GOOGLE_API_KEY e DATABASE_URL_READONLY estão presentes."""
    return bool(os.environ.get("GOOGLE_API_KEY")) and bool(os.environ.get("DATABASE_URL_READONLY"))


# ============================================================
# Envio de mensagem — generator de chunks de texto
# ============================================================
def send_message(
    history: list[dict],
    user_msg: str,
    schema_str: str,
    summary_str: str,
) -> Generator[str, None, dict]:
    """Manda user_msg pro Gemini e devolve generator de chunks de texto.

    O generator termina retornando dict {"tokens_in": N, "tokens_out": N,
    "tool_calls": [...]} via StopIteration.value — capturar com:

        gen = send_message(...)
        for chunk in gen:
            ...
        meta = gen.value  # só funciona em runtime via wrapper
    """
    from google import genai
    from google.genai import types

    client = _client()
    system_prompt = build_system_prompt(schema_str, summary_str)

    # Converte history (lista de {"role","content"}) pro formato Gemini.
    historico_gemini = []
    for msg in history:
        papel = "user" if msg["role"] == "user" else "model"
        historico_gemini.append(
            types.Content(role=papel, parts=[types.Part.from_text(text=msg["content"])])
        )

    chat = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[run_query],
            # Limita o número de function calls automáticos no mesmo turno.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=5,
            ),
        ),
        history=historico_gemini,
    )

    tokens_in = 0
    tokens_out = 0
    for chunk in chat.send_message_stream(user_msg):
        if chunk.text:
            yield chunk.text
        if getattr(chunk, "usage_metadata", None):
            # O último chunk traz usage_metadata acumulado da resposta.
            tokens_in = chunk.usage_metadata.prompt_token_count or 0
            tokens_out = chunk.usage_metadata.candidates_token_count or 0

    # Como Python não permite return em generator simples, retornamos os
    # metadados pelo wrapper send_message_collect abaixo.
    return {"tokens_in": tokens_in, "tokens_out": tokens_out}


def send_message_collect(
    history: list[dict],
    user_msg: str,
    schema_str: str,
    summary_str: str,
) -> tuple[Generator[str, None, None], dict]:
    """Wrapper que separa o stream de texto dos metadados.

    Uso típico no Streamlit:

        stream, meta = send_message_collect(...)
        texto_final = st.write_stream(stream)
        # após stream consumido, meta["tokens_in"] e meta["tokens_out"] estão preenchidos
    """
    meta: dict = {"tokens_in": 0, "tokens_out": 0}

    def _stream() -> Generator[str, None, None]:
        gen = send_message(history, user_msg, schema_str, summary_str)
        try:
            while True:
                chunk = next(gen)
                yield chunk
        except StopIteration as stop:
            if stop.value:
                meta.update(stop.value)

    return _stream(), meta


# ============================================================
# Destilação de sessões antigas
# ============================================================
def destilar_resumo_pendentes() -> int:
    """Encontra sessões com >INATIVIDADE_DESTILAR_MINUTOS sem mensagem e ainda
    não encerradas. Pra cada uma, gera resumo destilado via Gemini, salva em
    chat_session_summaries e marca como encerrada. Retorna quantas processou.
    """
    if not configurada():
        return 0  # nada a fazer sem API key

    from google import genai
    from google.genai import types

    with _admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            select id
            from chat_sessions
            where encerrada_em is null
              and last_message_at < now() - interval '{INATIVIDADE_DESTILAR_MINUTOS} minutes'
            """
        )
        pendentes = [row[0] for row in cur.fetchall()]

    if not pendentes:
        return 0

    client = _client()
    processadas = 0

    for sid in pendentes:
        try:
            with _admin_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    select role, content
                    from chat_messages
                    where session_id = %s
                      and content is not null
                    order by created_at
                    """,
                    (sid,),
                )
                mensagens = cur.fetchall()

            if not mensagens:
                # Sessão vazia — só marca como encerrada, não tenta destilar.
                with _admin_conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "update chat_sessions set encerrada_em = now() where id = %s",
                        (sid,),
                    )
                processadas += 1
                continue

            transcript = "\n".join(f"[{r}] {c}" for r, c in mensagens)
            prompt_destilar = (
                "Resuma em até 2000 tokens o que aprendemos nesta conversa que "
                "vale carregar pras próximas. Inclua: preferências do usuário, "
                "termos internos do domínio que ele usa, perguntas frequentes, "
                "fatos sobre os dados que valem lembrar. NÃO enumere nomes "
                "próprios de clientes desnecessariamente. Escreva em pt-BR.\n\n"
                f"Conversa:\n{transcript}"
            )

            resp = client.models.generate_content(
                model=MODEL,
                contents=prompt_destilar,
            )
            summary = (resp.text or "").strip()
            if not summary:
                summary = "(não foi possível destilar — sessão vazia ou resposta em branco)"

            tokens_in = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
            tokens_out = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
            registrar_uso(tokens_in, tokens_out)

            with _admin_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    insert into chat_session_summaries (session_id, summary, model)
                    values (%s, %s, %s)
                    on conflict (session_id) do update set
                        summary = excluded.summary,
                        model   = excluded.model,
                        created_at = now()
                    """,
                    (sid, summary, MODEL),
                )
                cur.execute(
                    "update chat_sessions set encerrada_em = now() where id = %s",
                    (sid,),
                )
            processadas += 1

        except Exception as exc:
            # Log no stderr (Streamlit Cloud captura) e segue — não bloqueia boot.
            print(f"[destilar_resumo_pendentes] erro na sessão {sid}: {exc}", flush=True)
            continue

    return processadas

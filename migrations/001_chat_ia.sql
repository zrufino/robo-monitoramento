-- ============================================================
-- Tabelas pro chat IA no dashboard.
-- Aplicar com: supabase db query --linked -f migrations/001_chat_ia.sql
-- ============================================================

-- Sessão de chat: cada vez que o dashboard abre, abre uma sessão nova.
-- encerrada_em é populado quando a destilação roda (>30min sem mensagem).
create table if not exists chat_sessions (
    id              uuid        primary key default gen_random_uuid(),
    started_at      timestamptz not null    default now(),
    last_message_at timestamptz,
    encerrada_em    timestamptz
);

create index if not exists ix_chat_sessions_pendentes_destilar
    on chat_sessions (last_message_at)
    where encerrada_em is null;

-- Mensagens: 1 linha por turno (user, assistant, ou tool).
-- tool_calls guarda as chamadas SQL que a IA fez naquele turno.
create table if not exists chat_messages (
    id            uuid        primary key default gen_random_uuid(),
    session_id    uuid        not null    references chat_sessions(id) on delete cascade,
    role          text        not null    check (role in ('user','assistant','tool')),
    content       text,
    tool_calls    jsonb,
    tokens_input  int         not null    default 0,
    tokens_output int         not null    default 0,
    created_at    timestamptz not null    default now()
);

create index if not exists ix_chat_messages_session
    on chat_messages (session_id, created_at);

-- Memória persistente entre sessões: 1 resumo destilado por sessão encerrada.
-- O system prompt da nova sessão injeta o MAIS RECENTE.
create table if not exists chat_session_summaries (
    id         uuid        primary key default gen_random_uuid(),
    session_id uuid        not null    unique references chat_sessions(id) on delete cascade,
    summary    text        not null,
    model      text,
    created_at timestamptz not null    default now()
);

create index if not exists ix_chat_session_summaries_recente
    on chat_session_summaries (created_at desc);

-- Cap mensal de gasto Gemini ($5,00 USD).
-- Toda chamada à API incrementa esta tabela. Bloqueio quando custo_usd >= 5.
create table if not exists chat_usage_mensal (
    mes_ano       text          primary key,  -- 'YYYY-MM'
    tokens_in     bigint        not null default 0,
    tokens_out    bigint        not null default 0,
    custo_usd     numeric(10,4) not null default 0,
    atualizado_em timestamptz   not null default now()
);

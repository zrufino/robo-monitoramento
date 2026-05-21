"""
Coletor de estoque — Robô de Monitoramento Qualy.

A cada execução:
  1. Cria uma linha em api_runs ("começou agora")
  2. Baixa /cadastros/equipamentos     → upsert items + insert stock_snapshots
  3. Baixa /quantitativos?operacao=Compra   (incremental por id)
  4. Baixa /quantitativos?operacao=Baixa    (incremental por id)
  5. Baixa /rotas?operacao=Saída     (incremental por id_liberacao)
  6. Pra cada rota NOVA chama /rotas/details e popula route_items
  7. Calcula diferenças entre o snapshot atual e o anterior por item
  8. Tenta explicar cada diferença com Compra/Baixa (movements) ou Saída (route_items),
     e marca padrões coerentes de devolução/fluxo interno como 'inferida'
  9. Marca a run como sucesso/parcial/erro

Idempotência:
  - movements usa id da API D&O como PK            → upsert evita duplicar
  - routes  usa id_liberacao como PK               → upsert evita duplicar
  - route_items usa id_liberacaoitens como PK      → upsert evita duplicar
  - items   usa codigo como PK                     → upsert atualiza o cadastro
  - stock_snapshots tem unique(run_id, codigo)     → 1 foto por item por run
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, date
from typing import Any, Optional

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

load_dotenv()

BASE_URL = "https://api.dosistemas.com.br/qualy/api"
TOKEN = os.environ["DOSISTEMAS_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
}

PAGE_SIZE_EQUIP = 5000
PAGE_SIZE_QUANT = 1000
PAGE_SIZE_ROTAS = 50000  # /rotas ignora 'page' — pega tudo numa chamada só

# Janelas (em dias)
# - JANELA_ROUTE_DETAILS: rotas até essa idade pegam /rotas/details (backfill self-healing)
# - JANELA_MATCH_ROTA: ao explicar uma mudança, considera rotas dessa idade
# 14 dias cobre a maior parte do ciclo aluguel típico sem inflar custo de coleta.
JANELA_ROUTE_DETAILS = 14
JANELA_MATCH_ROTA = 14

CAMPOS_QUANT = (
    "qtde_total", "qtde_disponivel", "qtde_locada", "qtde_manutencao",
    "qtde_transito", "qtde_expedicao", "qtde_baixa", "qtde_filial",
    "qtde_patrimonio", "qtde_franquia", "qtde_reserva_filial",
)


# ============================================================
# Helpers de log
# ============================================================
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================
# Cliente da API D&O
# ============================================================
def api_get(path: str, params: dict | None = None) -> tuple[int, Any]:
    """GET genérico. Retorna (status_code, payload) — payload pode ser list, dict ou texto."""
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=120)
    except requests.RequestException as exc:
        return 0, {"erro_rede": str(exc)}

    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {"raw_text": resp.text}


def parse_data_br(s: str | None) -> Optional[date]:
    """Converte '15/05/2026' em date. Aceita também '15/05/2026T08:36:39'."""
    if not s:
        return None
    s = s.split("T", 1)[0]
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        return None


# ============================================================
# Banco de dados
# ============================================================
def iniciar_run(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "insert into api_runs (status) values ('rodando') returning id"
        )
        return cur.fetchone()[0]


def finalizar_run(conn, run_id: int, status: str, stats: dict, erros: list | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update api_runs
            set finalizado_em = now(),
                status = %s,
                duracao_segundos = extract(epoch from (now() - iniciado_em)),
                qtd_items_coletados = %s,
                qtd_movimentos_novos = %s,
                qtd_rotas_novas = %s,
                qtd_mudancas_detectadas = %s,
                erros = %s
            where id = %s
            """,
            (
                status,
                stats.get("items"),
                stats.get("movimentos_novos"),
                stats.get("rotas_novas"),
                stats.get("mudancas"),
                Jsonb(erros) if erros else None,
                run_id,
            ),
        )


def salvar_raw(conn, run_id: int, endpoint: str, params: dict, status: int, payload: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into raw_api_responses (run_id, endpoint, params, status_code, payload) values (%s,%s,%s,%s,%s)",
            (run_id, endpoint, Jsonb(params), status, Jsonb(payload)),
        )


# ============================================================
# Coleta 1 — Equipamentos (cadastro + saldo) → items + stock_snapshots
# ============================================================
def coletar_equipamentos(conn, run_id: int) -> int:
    log("coletando /cadastros/equipamentos ...")
    params = {"first": str(PAGE_SIZE_EQUIP), "sort_by": "codigo"}
    status, payload = api_get("/cadastros/equipamentos", params)
    salvar_raw(conn, run_id, "/cadastros/equipamentos", params, status, payload)

    if status != 200 or not isinstance(payload, list):
        log(f"  ERRO status={status} payload tipo={type(payload).__name__}")
        return 0

    log(f"  {len(payload)} itens recebidos. Salvando...")

    with conn.cursor() as cur:
        for item in payload:
            codigo = item.get("codigo")
            if not codigo:
                continue

            cur.execute(
                """
                insert into items (codigo, descricao, tipo, tipo_equipamento, quantitativo,
                                   codsuperior, unidade, vlcompra, vlmercado, status, guid,
                                   atualizado_na_api, atualizado_em_robo, payload)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), %s)
                on conflict (codigo) do update set
                    descricao = excluded.descricao,
                    tipo = excluded.tipo,
                    tipo_equipamento = excluded.tipo_equipamento,
                    quantitativo = excluded.quantitativo,
                    codsuperior = excluded.codsuperior,
                    unidade = excluded.unidade,
                    vlcompra = excluded.vlcompra,
                    vlmercado = excluded.vlmercado,
                    status = excluded.status,
                    guid = excluded.guid,
                    atualizado_na_api = excluded.atualizado_na_api,
                    atualizado_em_robo = now(),
                    payload = excluded.payload
                """,
                (
                    codigo,
                    item.get("descricao"),
                    item.get("tipo"),
                    item.get("tipo_equipamento"),
                    item.get("quantitativo"),
                    item.get("codsuperior"),
                    item.get("unidade"),
                    item.get("vlcompra"),
                    item.get("vlmercado"),
                    item.get("status"),
                    item.get("guid"),
                    item.get("atualizado_em"),
                    Jsonb(item),
                ),
            )

            cur.execute(
                """
                insert into stock_snapshots (run_id, codigo, qtde_total, qtde_disponivel,
                  qtde_locada, qtde_manutencao, qtde_transito, qtde_expedicao, qtde_baixa,
                  qtde_filial, qtde_patrimonio, qtde_franquia, qtde_reserva_filial, status)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    run_id, codigo,
                    item.get("qtde_total"), item.get("qtde_disponivel"),
                    item.get("qtde_locada"), item.get("qtde_manutencao"),
                    item.get("qtde_transito"), item.get("qtde_expedicao"),
                    item.get("qtde_baixa"), item.get("qtde_filial"),
                    item.get("qtde_patrimonio"), item.get("qtde_franquia"),
                    item.get("qtde_reserva_filial"), item.get("status"),
                ),
            )
    return len(payload)


# ============================================================
# Coleta 2 — Movimentações de /quantitativos (Compra, Baixa)
# ============================================================
def maior_movement_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("select coalesce(max(id), 0) from stock_movements")
        return cur.fetchone()[0]


def coletar_quantitativos(conn, run_id: int, operacao: str) -> int:
    log(f"coletando /quantitativos operacao={operacao} ...")
    params = {"first": str(PAGE_SIZE_QUANT), "operacao": operacao, "sort_by": "desc(id)"}
    status, payload = api_get("/cadastros/equipamentos/quantitativos", params)
    salvar_raw(conn, run_id, "/cadastros/equipamentos/quantitativos", params, status, payload)

    if status != 200 or not isinstance(payload, list):
        log(f"  ERRO status={status}")
        return 0

    log(f"  {len(payload)} movimentos retornados. Aplicando upsert por id...")

    novos = 0
    with conn.cursor() as cur:
        for mov in payload:
            data_dt = parse_data_br(mov.get("data"))
            cur.execute(
                """
                insert into stock_movements (id, codigo, operacao, qtde, vlunitario, valor,
                                             data_api, data_movimento, nf, codclifor, nomeclifor,
                                             observacao, cod_deposito, run_id)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (id) do nothing
                """,
                (
                    mov.get("id"), mov.get("codigo"), mov.get("operacao"),
                    mov.get("qtde"), mov.get("vlunitario"), mov.get("valor"),
                    mov.get("data"), data_dt, mov.get("nf"),
                    mov.get("codclifor"), mov.get("nomeclifor"),
                    mov.get("observacao"), mov.get("cod_deposito"), run_id,
                ),
            )
            if cur.rowcount:
                novos += 1
    return novos


# ============================================================
# Coleta 3 — Rotas (Saída) incremental por id_liberacao
# ============================================================
def maior_route_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("select coalesce(max(id_liberacao), 0) from routes")
        return cur.fetchone()[0]


def coletar_rotas(conn, run_id: int) -> tuple[int, list[tuple[int, int, str]]]:
    """
    A API /rotas IGNORA o parâmetro 'page' — sempre retorna a primeira página.
    Mas aceita page_size grande (testado até 50000). Estratégia: pega tudo numa
    chamada só e deixa o ON CONFLICT (id_liberacao) cuidar de não duplicar.

    Retorna (qtd_novos, lista_novos) onde lista_novos é
        [(id_liberacao, id_origem, tipo_ficha), ...]
    pra alimentar a coleta de route_items.
    """
    log("coletando /rotas operacao=Saída ...")

    params = {
        "page_size": str(PAGE_SIZE_ROTAS),
        "operacao": "Saída",
        "sort_by": "desc(id_liberacao)",
    }
    status, payload = api_get("/rotas", params)
    salvar_raw(conn, run_id, "/rotas", params, status, payload)

    if status != 200 or not isinstance(payload, dict):
        log(f"  ERRO status={status}")
        return 0, []

    registros = payload.get("data", [])
    total = payload.get("total_items")
    log(f"  total_items={total}, retornados={len(registros)}")

    novos = 0
    novos_lista: list[tuple[int, int, str]] = []
    with conn.cursor() as cur:
        for r in registros:
            id_lib = r.get("id_liberacao")
            if id_lib is None:
                continue

            destino = (r.get("destinatario") or {}).get("destino") or {}
            contato = r.get("contato") or {}
            destinatario = r.get("destinatario") or {}

            cur.execute(
                """
                insert into routes (id_liberacao, id_recepcao, id_origem, data_api, data_rota,
                                    tipo_ficha, operacao, status, status_rota,
                                    destinatario_cod, destinatario_nome,
                                    destinatario_razaosocial, contato_nome,
                                    contato_telefone, endereco_cidade, endereco_estado,
                                    run_id, payload)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (id_liberacao) do nothing
                """,
                (
                    id_lib, r.get("id_recepcao"), r.get("id_origem"),
                    r.get("data"), parse_data_br(r.get("data")),
                    r.get("tipo_ficha"), r.get("operacao"), r.get("status"),
                    r.get("status_rota"),
                    destinatario.get("cod_cliente"), destinatario.get("nom_cliente"),
                    destinatario.get("razaosocial"),
                    contato.get("contato_nome"),
                    contato.get("contato_telefone") or contato.get("contato_departamento"),
                    destino.get("cidade"), destino.get("estado"),
                    run_id, Jsonb(r),
                ),
            )
            if cur.rowcount:
                novos += 1
                if r.get("id_origem") and r.get("tipo_ficha"):
                    novos_lista.append((id_lib, r["id_origem"], r["tipo_ficha"]))

    return novos, novos_lista


# ============================================================
# Coleta 4 — Detalhes da rota (route_items) — só pras NOVAS
# ============================================================
def coletar_route_items(conn, run_id: int, novas_rotas: list[tuple[int, int, str]]) -> int:
    """
    Pra cada rota nova, chama /rotas/details?tipo_ficha=X&id_origem=Y
    e popula route_items. Self-healing: também busca detalhes pras rotas
    dos últimos 3 dias que ainda não têm route_items (cobre runs anteriores
    que rodaram antes deste código existir, ou rotas com falha de detalhe).
    """
    pendentes: list[tuple[int, int, str]] = list(novas_rotas)

    # Backfill: rotas recentes sem route_items
    ids_ja_na_lista = [x[0] for x in novas_rotas]
    with conn.cursor() as cur:
        cur.execute(
            """
            select r.id_liberacao, r.id_origem, r.tipo_ficha
            from routes r
            left join route_items ri on ri.id_liberacao = r.id_liberacao
            where r.data_rota >= (now()::date - %s * interval '1 day')
              and r.id_origem is not null
              and r.tipo_ficha is not null
              and ri.id_liberacaoitens is null
              and r.id_liberacao <> all(%s::int[])
            group by r.id_liberacao, r.id_origem, r.tipo_ficha
            """,
            (JANELA_ROUTE_DETAILS, ids_ja_na_lista),
        )
        for id_lib, id_origem, tipo_ficha in cur.fetchall():
            pendentes.append((id_lib, id_origem, tipo_ficha))

    if not pendentes:
        log("coletando /rotas/details ... nada a fazer (0 rotas pendentes)")
        return 0

    log(f"coletando /rotas/details pra {len(pendentes)} rotas "
        f"({len(novas_rotas)} novas + {len(pendentes) - len(novas_rotas)} backfill {JANELA_ROUTE_DETAILS}d) ...")

    itens_inseridos = 0
    erros_detalhe = 0
    with conn.cursor() as cur:
        for id_lib, id_origem, tipo_ficha in pendentes:
            params = {"tipo_ficha": tipo_ficha, "id_origem": id_origem}
            status, payload = api_get("/rotas/details", params)

            if status != 200 or not isinstance(payload, dict):
                erros_detalhe += 1
                log(f"  liberação {id_lib} (id_origem={id_origem}): status={status} — pulando")
                continue

            for item in (payload.get("liberacoes") or []):
                id_item = item.get("id_liberacaoitens")
                if id_item is None:
                    continue

                linha = item.get("linha") or {}
                pat = item.get("patrimonio") or {}

                cur.execute(
                    """
                    insert into route_items (
                        id_liberacaoitens, id_liberacao, id_liberacaomovimento,
                        cod_linha, nom_linha, quantitativo, lin_pesobruto, lin_pesoliquido,
                        cod_patrimonio, nom_patrimonio, marca, modelo, serie,
                        pat_pesobruto, pat_pesoliquido,
                        qtde_liberada, qtde_rota, qtde_entregue,
                        run_id, payload
                    )
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    on conflict (id_liberacaoitens) do nothing
                    """,
                    (
                        id_item, item.get("id_liberacao") or id_lib,
                        item.get("id_liberacaomovimento"),
                        linha.get("cod_linha"), linha.get("nom_linha"),
                        linha.get("quantitativo"),
                        linha.get("lin_pesobruto"), linha.get("lin_pesoliquido"),
                        pat.get("cod_patrimonio"), pat.get("nom_patrimonio"),
                        pat.get("marca"), pat.get("modelo"), pat.get("serie"),
                        pat.get("pat_pesobruto"), pat.get("pat_pesoliquido"),
                        item.get("qtde_liberada"), item.get("qtde_rota"),
                        item.get("qtde_entregue"),
                        run_id, Jsonb(item),
                    ),
                )
                if cur.rowcount:
                    itens_inseridos += 1

    log(f"  {itens_inseridos} route_items inseridos. ({erros_detalhe} rotas com erro de detalhe)")
    return itens_inseridos


# ============================================================
# Coleta 5 — Calcular mudanças entre snapshots
# ============================================================
def calcular_mudancas(conn, run_id: int) -> int:
    """
    Compara o snapshot deste run com o snapshot anterior de cada item.
    Insere uma linha em stock_changes para cada (codigo, campo) que mudou.
    Tenta explicar a mudança com um movement do mesmo dia (mesmo código, qtde compatível).
    """
    log("calculando mudanças entre snapshots ...")
    campos = ", ".join(CAMPOS_QUANT)
    quoted = ",".join(f"'{c}'" for c in CAMPOS_QUANT)

    sql = f"""
    with atual as (
        select s.id, s.codigo, {campos}
        from stock_snapshots s
        where s.run_id = %s
    ),
    anterior as (
        select distinct on (codigo) id, codigo, {campos}
        from stock_snapshots
        where run_id <> %s
        order by codigo, capturado_em desc
    ),
    unpivoted as (
        select
            a.codigo,
            a.id as snap_atual_id,
            p.id as snap_anterior_id,
            t.campo,
            t.valor_atual,
            t.valor_anterior
        from atual a
        left join anterior p using (codigo)
        cross join lateral (values
            {",".join(f"('{c}', a.{c}::int, p.{c}::int)" for c in CAMPOS_QUANT)}
        ) as t(campo, valor_atual, valor_anterior)
        where p.codigo is not null
          and coalesce(a.{CAMPOS_QUANT[0]},0) is not null
    )
    insert into stock_changes (run_id, codigo, campo, valor_anterior, valor_atual, delta,
                               snapshot_anterior_id, snapshot_atual_id, status_explicacao)
    select %s, codigo, campo, valor_anterior, valor_atual, (valor_atual - valor_anterior),
           snap_anterior_id, snap_atual_id, 'nao_explicada'
    from unpivoted
    where coalesce(valor_atual,0) <> coalesce(valor_anterior,0)
    returning id
    """

    with conn.cursor() as cur:
        cur.execute(sql, (run_id, run_id, run_id))
        novas = cur.rowcount

    log(f"  {novas} mudanças detectadas.")
    if novas == 0:
        return 0

    # ---------- ETAPA A: explicar Compra/Baixa via stock_movements ----------
    log("  A) explicando Compra/Baixa via stock_movements ...")
    with conn.cursor() as cur:
        cur.execute(
            """
            update stock_changes c
            set explicada_por_movimento_id = m.id,
                status_explicacao = 'explicada'
            from stock_movements m
            where c.run_id = %s
              and c.status_explicacao = 'nao_explicada'
              and m.codigo = c.codigo
              and m.data_movimento >= (now()::date - interval '2 days')
              and abs(c.delta) = m.qtde
              and (
                    (m.operacao = 'Compra' and c.campo in ('qtde_total','qtde_disponivel') and c.delta > 0)
                 or (m.operacao = 'Baixa'  and c.campo in ('qtde_baixa') and c.delta > 0)
                 or (m.operacao = 'Baixa'  and c.campo in ('qtde_total','qtde_disponivel') and c.delta < 0)
              )
            """,
            (run_id,),
        )
        explicadas_mov = cur.rowcount
    log(f"     {explicadas_mov} explicadas por movimento.")

    # ---------- ETAPA B: explicar Saída via route_items ----------
    # Saída causa: qtde_disponivel↓ + (qtde_expedicao OU qtde_transito OU qtde_locada)↑.
    # Pega a rota de Saída mais recente (3 dias) com cod_linha = c.codigo.
    log("  B) explicando Saída via route_items ...")
    with conn.cursor() as cur:
        cur.execute(
            """
            with rota_por_codigo as (
                select distinct on (ri.cod_linha)
                       ri.cod_linha,
                       ri.id_liberacao,
                       r.data_rota
                from route_items ri
                join routes r on r.id_liberacao = ri.id_liberacao
                where r.operacao = 'Saída'
                  and r.data_rota >= (now()::date - %s * interval '1 day')
                order by ri.cod_linha, r.data_rota desc, ri.id_liberacao desc
            )
            update stock_changes c
            set explicada_por_rota_id = rpc.id_liberacao,
                status_explicacao = 'explicada'
            from rota_por_codigo rpc
            where c.run_id = %s
              and c.status_explicacao = 'nao_explicada'
              and rpc.cod_linha = c.codigo
              and (
                    (c.campo = 'qtde_disponivel' and c.delta < 0)
                 or (c.campo in ('qtde_expedicao','qtde_transito','qtde_locada') and c.delta > 0)
              )
            """,
            (JANELA_MATCH_ROTA, run_id),
        )
        explicadas_saida = cur.rowcount
    log(f"     {explicadas_saida} explicadas por route_item (Saída).")

    # ---------- ETAPA C: inferir devolução, manutenção e fluxo interno ----------
    # A API não expõe esses eventos como movimento atômico. Marca como 'inferida'
    # os padrões que casam com comportamentos internos esperados.
    log("  C) inferindo devolução, manutenção e fluxo interno ...")
    with conn.cursor() as cur:
        cur.execute(
            """
            update stock_changes
            set status_explicacao = 'inferida'
            where run_id = %s
              and status_explicacao = 'nao_explicada'
              and (
                    -- devolução: disponivel sobe, ou locada/transito/expedicao caem
                    (campo = 'qtde_disponivel' and delta > 0)
                 or (campo in ('qtde_locada','qtde_transito','qtde_expedicao') and delta < 0)
                    -- manutenção: entrada/saída de manutenção (não tem evento na API)
                 or (campo = 'qtde_manutencao')
              )
            """,
            (run_id,),
        )
        inferidas = cur.rowcount
    log(f"     {inferidas} inferidas (devolução/manutenção/fluxo interno).")

    total_resolvidas = explicadas_mov + explicadas_saida + inferidas
    log(f"  resumo: {total_resolvidas}/{novas} resolvidas "
        f"({explicadas_mov} mov + {explicadas_saida} rota + {inferidas} inferidas).")
    return novas


# ============================================================
# Main
# ============================================================
def main() -> int:
    t0 = time.time()
    with psycopg.connect(DATABASE_URL, connect_timeout=15, autocommit=False) as conn:
        run_id = iniciar_run(conn)
        conn.commit()
        log(f"=== RUN {run_id} iniciada ===")

        stats: dict = {}
        erros: list = []
        status_final = "sucesso"

        try:
            stats["items"] = coletar_equipamentos(conn, run_id)
            conn.commit()
        except Exception as exc:
            log(f"  EXCEÇÃO em equipamentos: {exc}")
            erros.append({"etapa": "equipamentos", "erro": str(exc)})
            conn.rollback()
            status_final = "parcial"

        for op in ("Compra", "Baixa"):
            try:
                key = f"mov_{op.lower()}"
                stats[key] = coletar_quantitativos(conn, run_id, op)
                conn.commit()
            except Exception as exc:
                log(f"  EXCEÇÃO em quantitativos {op}: {exc}")
                erros.append({"etapa": f"quantitativos.{op}", "erro": str(exc)})
                conn.rollback()
                status_final = "parcial"
        stats["movimentos_novos"] = stats.get("mov_compra", 0) + stats.get("mov_baixa", 0)

        novas_rotas_lista: list[tuple[int, int, str]] = []
        try:
            stats["rotas_novas"], novas_rotas_lista = coletar_rotas(conn, run_id)
            conn.commit()
        except Exception as exc:
            log(f"  EXCEÇÃO em rotas: {exc}")
            erros.append({"etapa": "rotas", "erro": str(exc)})
            conn.rollback()
            status_final = "parcial"

        try:
            stats["route_items_novos"] = coletar_route_items(conn, run_id, novas_rotas_lista)
            conn.commit()
        except Exception as exc:
            log(f"  EXCEÇÃO em route_items: {exc}")
            erros.append({"etapa": "route_items", "erro": str(exc)})
            conn.rollback()
            status_final = "parcial"

        try:
            stats["mudancas"] = calcular_mudancas(conn, run_id)
            conn.commit()
        except Exception as exc:
            log(f"  EXCEÇÃO em changes: {exc}")
            erros.append({"etapa": "changes", "erro": str(exc)})
            conn.rollback()
            status_final = "parcial"

        if erros and stats.get("items", 0) == 0:
            status_final = "erro"

        finalizar_run(conn, run_id, status_final, stats, erros or None)
        conn.commit()

    duracao = time.time() - t0
    log(f"=== RUN {run_id} {status_final.upper()} em {duracao:.1f}s ===")
    log(f"    items={stats.get('items')}  movs_novos={stats.get('movimentos_novos')}  "
        f"rotas_novas={stats.get('rotas_novas')}  mudanças={stats.get('mudancas')}")
    return 0 if status_final == "sucesso" else 1


if __name__ == "__main__":
    sys.exit(main())

"""
Dashboard de monitoramento de estoque — Qualy / Verleih.

Roda com:
    streamlit run dashboard.py

Lê direto do Supabase via DATABASE_URL (mesmo .env do coletor).
Cache de 5 min: dados atualizam após cada coleta sem precisar reiniciar.
"""

import os
from datetime import datetime

import altair as alt
import pandas as pd
import psycopg
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

st.set_page_config(
    page_title="Estoque Qualy",
    page_icon="📦",
    layout="wide",
)


# ============================================================
# Acesso ao banco — cache de 5 min, recarrega após cada coleta
# ============================================================
@st.cache_data(ttl=300)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    with psycopg.connect(DATABASE_URL, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c.name for c in cur.description] if cur.description else []
            return pd.DataFrame(cur.fetchall(), columns=cols)


def humano(x):
    if pd.isna(x):
        return "—"
    if isinstance(x, (int, float)) and float(x).is_integer():
        return f"{int(x):,}".replace(",", ".")
    return str(x)


# ============================================================
# Sidebar — navegação e info da última coleta
# ============================================================
st.sidebar.title("📦 Estoque Qualy")
pagina = st.sidebar.radio(
    "Navegação",
    ["Saldo atual", "Linha do tempo por item", "Divergências", "Saúde do robô"],
    label_visibility="collapsed",
)

# Info da última run no rodapé da sidebar (defensivo — não pode quebrar a sidebar)
try:
    ultima = query(
        "select run_id, iniciado_em, status, qtd_items_coletados from v_resumo_runs order by run_id desc limit 1"
    )
    if not ultima.empty:
        st.sidebar.markdown("---")
        run_em = ultima.iloc[0]["iniciado_em"]
        st.sidebar.caption(
            f"**Última coleta:** run #{ultima.iloc[0]['run_id']} — "
            f"{run_em.strftime('%d/%m %H:%M') if hasattr(run_em, 'strftime') else run_em}\n\n"
            f"**Status:** {ultima.iloc[0]['status']} · "
            f"{ultima.iloc[0]['qtd_items_coletados']} itens"
        )
except Exception as exc:
    st.sidebar.error(f"Falha ao ler última run: {exc}")

st.sidebar.button("Recarregar dados", on_click=st.cache_data.clear)


# ============================================================
# Página 1 — Saldo atual
# ============================================================
if pagina == "Saldo atual":
    st.title("Saldo atual")

    with st.spinner("Carregando saldo..."):
        df = query("select * from v_saldo_atual order by codigo")
    if df.empty:
        st.warning("Nenhum item no banco. Rode o coletor primeiro.")
        st.stop()

    # KPIs de quantidade — somam SÓ o nível SG (subgrupo), que é onde o Verleih
    # guarda a contabilidade real. Somar GR+SG+IT dá triple-counting porque
    # GR é soma dos SG abaixo, e IT (patrimônio individual) não tem qtde.
    df_sg = df[df["item_tipo"] == "SG"]
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Total em estoque", humano(df_sg["qtde_total"].sum()))
    k2.metric("Disponível", humano(df_sg["qtde_disponivel"].sum()))
    k3.metric("Locado", humano(df_sg["qtde_locada"].sum()))
    k4.metric("Manutenção", humano(df_sg["qtde_manutencao"].sum()))
    k5.metric("Trânsito", humano(df_sg["qtde_transito"].sum()))

    # KPIs de cadastro — quantos itens existem em cada nível
    c1, c2, c3 = st.columns(3)
    c1.metric("Grupos", f"{(df['item_tipo']=='GR').sum():,}".replace(",", "."))
    c2.metric("Subgrupos", f"{(df['item_tipo']=='SG').sum():,}".replace(",", "."))
    c3.metric("Patrimônios", f"{(df['item_tipo']=='IT').sum():,}".replace(",", "."))

    st.markdown("---")

    # Filtros pra tabela
    col1, col2, col3, col4 = st.columns([3, 1, 1, 1])
    with col1:
        busca = st.text_input("Buscar (código ou descrição)", "")
    with col2:
        tipo = st.selectbox(
            "Tipo", ["SG (subgrupo)", "GR (grupo)", "IT (patrimônio)", "Todos"]
        )
    with col3:
        com_saldo = st.checkbox("Só com saldo > 0", value=False)
    with col4:
        com_locada = st.checkbox("Tem em locação", value=False)

    df_f = df.copy()
    if busca:
        mask = (
            df_f["codigo"].str.contains(busca, case=False, na=False)
            | df_f["descricao"].fillna("").str.contains(busca, case=False)
        )
        df_f = df_f[mask]
    if tipo != "Todos":
        df_f = df_f[df_f["item_tipo"] == tipo[:2]]
    if com_saldo:
        df_f = df_f[df_f["qtde_total"].fillna(0) > 0]
    if com_locada:
        df_f = df_f[df_f["qtde_locada"].fillna(0) > 0]

    st.caption(f"Mostrando **{len(df_f):,}** linhas".replace(",", "."))

    st.dataframe(
        df_f[[
            "codigo", "descricao", "item_tipo", "quantitativo",
            "qtde_total", "qtde_disponivel", "qtde_locada",
            "qtde_manutencao", "qtde_transito", "qtde_expedicao", "qtde_baixa",
            "snapshot_em",
        ]],
        use_container_width=True,
        hide_index=True,
    )

# ============================================================
# Página 2 — Linha do tempo por item
# ============================================================
elif pagina == "Linha do tempo por item":
    st.title("Linha do tempo por item")

    # Selecionar item
    itens = query(
        "select codigo, descricao from items where descricao is not null order by codigo"
    )
    itens["label"] = itens["codigo"] + " · " + itens["descricao"].str.slice(0, 60)

    escolhido = st.selectbox(
        "Item",
        itens["codigo"].tolist(),
        format_func=lambda c: itens.set_index("codigo").loc[c, "label"],
    )

    if escolhido:
        # Cadastro
        cad = query("select * from v_saldo_atual where codigo = %s", (escolhido,))
        if not cad.empty:
            r = cad.iloc[0]
            st.subheader(f"{r['codigo']} — {r['descricao']}")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total", humano(r["qtde_total"]))
            c2.metric("Disponível", humano(r["qtde_disponivel"]))
            c3.metric("Locado", humano(r["qtde_locada"]))
            c4.metric("Manutenção", humano(r["qtde_manutencao"]))

        st.markdown("### Saldo ao longo do tempo")
        snaps = query(
            """
            select capturado_em,
                   qtde_total, qtde_disponivel, qtde_locada,
                   qtde_manutencao, qtde_transito, qtde_expedicao, qtde_baixa
            from stock_snapshots
            where codigo = %s
            order by capturado_em
            """,
            (escolhido,),
        )

        if not snaps.empty and len(snaps) > 1:
            longo = snaps.melt(
                id_vars=["capturado_em"], var_name="campo", value_name="qtde"
            )
            chart = (
                alt.Chart(longo)
                .mark_line(point=True)
                .encode(
                    x=alt.X("capturado_em:T", title="Coleta"),
                    y=alt.Y("qtde:Q", title="Quantidade"),
                    color=alt.Color("campo:N", title="Campo"),
                    tooltip=["capturado_em:T", "campo:N", "qtde:Q"],
                )
                .properties(height=320)
            )
            st.altair_chart(chart, use_container_width=True)
        else:
            st.info("Ainda só há 1 snapshot — gráfico aparece a partir de 2 coletas.")

        st.markdown("### Mudanças detectadas")
        muds = query(
            """
            select detectada_em, campo, valor_anterior, valor_atual, delta,
                   status_explicacao,
                   coalesce(cliente_nome, mov_cliente, '') as parte,
                   coalesce(rota_id_liberacao::text, mov_nf::text, '') as referencia,
                   mov_operacao
            from v_mudancas
            where codigo = %s
            order by detectada_em desc, change_id desc
            """,
            (escolhido,),
        )
        if muds.empty:
            st.caption("Sem mudanças registradas pra esse item.")
        else:
            st.dataframe(muds, use_container_width=True, hide_index=True)

# ============================================================
# Página 3 — Divergências
# ============================================================
elif pagina == "Divergências":
    st.title("Divergências")
    st.caption(
        "Mudanças de saldo que o robô **não conseguiu explicar** com um evento da API "
        "(Compra/Baixa/Saída). Pode indicar movimento manual no sistema, baixa de patrimônio "
        "fora do fluxo ou bug na regra de explicação."
    )

    col1, col2 = st.columns([1, 2])
    with col1:
        somente_runs_recentes = st.selectbox(
            "Período",
            ["Últimas 3 coletas", "Última coleta", "Tudo"],
        )
    with col2:
        incluir_inferida = st.checkbox(
            "Incluir 'inferida' (devolução/manutenção sem evento da API)",
            value=False,
        )

    if somente_runs_recentes == "Última coleta":
        where_run = "and m.run_id = (select max(id) from api_runs where status='sucesso')"
    elif somente_runs_recentes == "Últimas 3 coletas":
        where_run = (
            "and m.run_id in (select id from api_runs where status='sucesso' "
            "order by id desc limit 3)"
        )
    else:
        where_run = ""

    if incluir_inferida:
        status_filter = "and m.status_explicacao in ('nao_explicada','inferida')"
    else:
        status_filter = "and m.status_explicacao = 'nao_explicada'"

    sql = f"""
        select m.detectada_em, m.run_id, m.codigo, m.item_descricao,
               m.campo, m.valor_anterior, m.valor_atual, m.delta,
               m.status_explicacao
        from v_mudancas m
        where 1=1 {where_run} {status_filter}
        order by m.detectada_em desc, m.change_id desc
    """
    df = query(sql)

    # KPIs
    k1, k2, k3 = st.columns(3)
    k1.metric("Mudanças no recorte", f"{len(df):,}".replace(",", "."))
    k2.metric(
        "Não explicadas",
        f"{(df['status_explicacao']=='nao_explicada').sum():,}".replace(",", "."),
    )
    k3.metric(
        "Inferidas",
        f"{(df['status_explicacao']=='inferida').sum():,}".replace(",", "."),
    )

    if df.empty:
        st.success("Nenhuma divergência no recorte selecionado.")
    else:
        st.markdown("### Por campo")
        por_campo = (
            df.groupby(["campo", "status_explicacao"])
            .size()
            .reset_index(name="ocorrencias")
        )
        chart = (
            alt.Chart(por_campo)
            .mark_bar()
            .encode(
                x=alt.X("campo:N", title="Campo de saldo"),
                y=alt.Y("ocorrencias:Q"),
                color=alt.Color("status_explicacao:N"),
                tooltip=["campo", "status_explicacao", "ocorrencias"],
            )
            .properties(height=260)
        )
        st.altair_chart(chart, use_container_width=True)

        st.markdown("### Detalhe")
        st.dataframe(df, use_container_width=True, hide_index=True)

# ============================================================
# Página 4 — Saúde do robô
# ============================================================
elif pagina == "Saúde do robô":
    st.title("Saúde do robô")

    df = query("select * from v_resumo_runs order by run_id desc limit 50")

    if df.empty:
        st.warning("Sem runs registradas ainda.")
    else:
        ultima = df.iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Última run", f"#{ultima['run_id']}")
        c2.metric("Status", ultima["status"])
        c3.metric("Duração", f"{ultima['duracao_segundos']:.0f}s" if ultima['duracao_segundos'] else "—")
        c4.metric("Itens coletados", humano(ultima["qtd_items_coletados"]))

        st.markdown("### Cobertura de explicação por run")
        cob = df[["run_id", "mudancas_explicadas", "mudancas_inferidas", "mudancas_nao_explicadas"]].copy()
        cob_long = cob.melt(id_vars=["run_id"], var_name="status", value_name="qtd")
        chart = (
            alt.Chart(cob_long)
            .mark_bar()
            .encode(
                x=alt.X("run_id:O", title="Run"),
                y=alt.Y("qtd:Q", stack="zero"),
                color=alt.Color("status:N"),
                tooltip=["run_id", "status", "qtd"],
            )
            .properties(height=260)
        )
        st.altair_chart(chart, use_container_width=True)

        st.markdown("### Histórico de runs")
        st.dataframe(df, use_container_width=True, hide_index=True)

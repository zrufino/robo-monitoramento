"""
Sonda inicial da API D&O / Do Sistemas.

Roda só GETs (seguro, não altera nada em produção).
Salva as respostas brutas em ./amostras_api/ pra a gente analisar depois
e decidir quais campos representam o saldo "oficial" do estoque.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.dosistemas.com.br/qualy/api"
TOKEN = os.getenv("DOSISTEMAS_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "DOSISTEMAS_TOKEN não encontrado. "
        "Crie um arquivo .env na raiz do projeto com: "
        "DOSISTEMAS_TOKEN=seu_token_aqui"
    )

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
}

SAIDA = Path("amostras_api")
SAIDA.mkdir(exist_ok=True)


def salvar(nome, dados):
    caminho = SAIDA / f"{nome}.json"
    with caminho.open("w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    print(f"  salvo: {caminho}")


def get(nome, path, params=None):
    url = f"{BASE_URL}{path}"

    print()
    print(f"GET {url}")
    print(f"  params: {params or {}}")

    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=60)
    except requests.RequestException as exc:
        print(f"  ERRO de rede: {exc}")
        salvar(nome, {"erro_rede": str(exc)})
        return None

    print(f"  status: {resp.status_code}")
    print(f"  content-type: {resp.headers.get('content-type')}")

    try:
        dados = resp.json()
    except ValueError:
        dados = {"raw_text": resp.text}

    salvar(nome, dados)

    if isinstance(dados, list):
        print(f"  itens retornados: {len(dados)}")
        if dados and isinstance(dados[0], dict):
            print("  campos do primeiro item:")
            for campo in sorted(dados[0].keys()):
                print(f"    - {campo}")
    elif isinstance(dados, dict):
        print("  campos do objeto:")
        for campo in sorted(dados.keys()):
            print(f"    - {campo}")

    return dados


def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 60)
    print("SONDA 1/5 — Versão da API (healthcheck e validação do token)")
    print("=" * 60)
    get(f"{ts}_01_versao", "/versao")

    print()
    print("=" * 60)
    print("SONDA 2/5 — Equipamentos (cadastro + saldos)")
    print("=" * 60)
    equipamentos = get(
        f"{ts}_02_equipamentos",
        "/cadastros/equipamentos",
        {"first": "20", "sort_by": "codigo"},
    )

    print()
    print("=" * 60)
    print("SONDA 3/5 — Quantitativos (candidato a histórico de movimentação)")
    print("=" * 60)
    get(
        f"{ts}_03_quantitativos",
        "/cadastros/equipamentos/quantitativos",
        {"first": "20", "sort_by": "desc(data)"},
    )

    codigo_exemplo = None
    if isinstance(equipamentos, list) and equipamentos:
        primeiro = equipamentos[0]
        if isinstance(primeiro, dict):
            codigo_exemplo = primeiro.get("codigo")

    if codigo_exemplo:
        print()
        print("=" * 60)
        print(f"SONDA 4/5 — Estoque por filial (item de exemplo: {codigo_exemplo})")
        print("=" * 60)
        get(
            f"{ts}_04_estoque_filiais",
            "/cadastros/equipamentos/estoquefiliais",
            {"codigo": codigo_exemplo},
        )
    else:
        print()
        print("Pulando sonda 4 (não foi possível extrair um código de exemplo).")

    print()
    print("=" * 60)
    print("SONDA 5/5 — Rotas (contexto de saídas/chamados)")
    print("=" * 60)
    get(f"{ts}_05_rotas", "/rotas", {"page_size": "10"})

    print()
    print("=" * 60)
    print("Fim. Olhe a pasta amostras_api/ para os JSONs salvos.")
    print("=" * 60)


if __name__ == "__main__":
    main()

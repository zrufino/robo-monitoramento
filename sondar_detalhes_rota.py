"""
Sondagem — descobrir como puxar a lista de itens dentro de uma liberação/rota.

Hipóteses a testar contra o id_liberacao 32599 e id_origem 12872:
  a) /rotas/details                       (citado no Swagger Rota)
  b) /rotas/details?id_liberacao=32599
  c) /rotas/details?id=32599
  d) /rotas/32599
  e) /cadastros/liberacoes/itens?id=12872
  f) /cadastros/contratos/itens?id=12872
"""

import json
import os
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.dosistemas.com.br/qualy/api"
TOKEN = os.environ["DOSISTEMAS_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

SAIDA = Path("amostras_api")
SAIDA.mkdir(exist_ok=True)
TS = datetime.now().strftime("%Y%m%d_%H%M%S")

ID_LIBERACAO = 32599
ID_ORIGEM = 12872


def sonda(label, path, params=None):
    url = f"{BASE_URL}{path}"
    print(f"\n[{label}]")
    print(f"  GET {path}  params={params or {}}")
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=60)
    except requests.RequestException as e:
        print(f"  ERRO de rede: {e}")
        return
    print(f"  status: {r.status_code}")
    try:
        d = r.json()
    except ValueError:
        d = {"raw_text": r.text[:300]}

    nome = f"{TS}_{label}.json"
    (SAIDA / nome).write_text(json.dumps(d, ensure_ascii=False, indent=2))
    print(f"  salvo: {nome}")

    if isinstance(d, list):
        print(f"  lista com {len(d)} itens")
        if d and isinstance(d[0], dict):
            print(f"  chaves do 1º: {sorted(d[0].keys())}")
    elif isinstance(d, dict):
        if "data" in d and isinstance(d["data"], list):
            print(f"  data: lista com {len(d['data'])} itens")
            if d["data"] and isinstance(d["data"][0], dict):
                print(f"  chaves do 1º item de data: {sorted(d['data'][0].keys())}")
        else:
            print(f"  chaves: {sorted(d.keys())[:15]}")


# Candidatos
sonda("a_rotas_details_sem_params", "/rotas/details")
sonda("b_rotas_details_id_liberacao", "/rotas/details", {"id_liberacao": ID_LIBERACAO})
sonda("c_rotas_details_id", "/rotas/details", {"id": ID_LIBERACAO})
sonda("d_rotas_path_id", f"/rotas/{ID_LIBERACAO}")
sonda("e_rotas_details_id_origem", "/rotas/details", {"id_origem": ID_ORIGEM})
sonda("f_rotas_itens", "/rotas/itens", {"id_liberacao": ID_LIBERACAO})
sonda("g_rotas_equipamentos", "/rotas/equipamentos", {"id_liberacao": ID_LIBERACAO})

print(f"\nFim. Amostras com prefixo {TS} em amostras_api/")

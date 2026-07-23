"""
Polling CV CRM -> Meta Conversions API
Cliente: Victa Engenharia - Vista Coqueiral

O QUE ESTE SCRIPT FAZ
---------------------
1. Descobre automaticamente o ID da situacao "Cancelado" no workflow de Leads
   do CV CRM (via GET /workflows/{funcionalidade}), para nao depender de um
   numero fixo que pode mudar entre ambientes.
2. Busca os leads cancelados (GET /v1/comercial/leads?idsituacao=...) desde a
   ultima execucao (controle feito por um arquivo local state.json).
3. Filtra apenas os leads cujo "motivo_cancelamento.nome" esteja na lista de
   motivos-alvo combinada com o cliente.
4. Para cada lead filtrado, monta o payload e envia o evento
   "LeadDesqualificado" via Meta CAPI (reaproveitando a logica de
   meta_lead_desqualificado_capi.py).
5. Atualiza o state.json com o timestamp mais recente processado, para a
   proxima execucao nao reprocessar os mesmos leads.

CREDENCIAIS (variaveis de ambiente, nunca hardcode):
    CV_CRM_SUBDOMINIO        -> ex: "victa" (a base fica em victa.cvcrm.com.br)
    CV_CRM_EMAIL             -> e-mail do usuario administrativo com token gerado
    CV_CRM_TOKEN             -> token gerado no painel do gestor
    META_CAPI_ACCESS_TOKEN   -> token de acesso do dataset pixel-coqueiral

ATENCAO / PONTOS PRA VALIDAR:
- O nome da "funcionalidade" usado em /workflows/{funcionalidade} para Leads
  foi assumido como "leads". Se a chamada retornar vazio/erro, verifique o
  nome exato com o suporte do CV CRM ou no proprio painel (Configuracoes >
  Workflows).
- leadgen_id/ctwa_clid: o schema padrao de retorno do lead NAO tem esses
  campos nativamente. O script tenta achar em "campos_adicionais" (lista de
  slug/valor customizados) usando alguns nomes candidatos comuns. Se o CV CRM
  de voces nao tiver esse campo configurado, o evento cai automaticamente
  para o fallback de email/telefone hasheados (ainda funciona, so com EMQ
  um pouco mais baixo).
"""

import json
import os
from pathlib import Path

import requests

from meta_lead_desqualificado_capi import enviar_lead_desqualificado

# ---------------------------------------------------------------------------
# Configuracao
# ---------------------------------------------------------------------------

CV_CRM_SUBDOMINIO = os.environ["CV_CRM_SUBDOMINIO"]
CV_CRM_EMAIL = os.environ["CV_CRM_EMAIL"]
CV_CRM_TOKEN = os.environ["CV_CRM_TOKEN"]

CV_CRM_BASE_URL = f"https://{CV_CRM_SUBDOMINIO}.cvcrm.com.br/api"
HEADERS = {"email": CV_CRM_EMAIL, "token": CV_CRM_TOKEN}

STATE_FILE = Path(__file__).parent / "state.json"

MOTIVOS_DESQUALIFICACAO_ALVO = {
    "Impossível contatar",
    "Não deseja ser contatada",
    "Não tem perfil Financeiro",
    "Engano",
}

# Nomes de slug candidatos em campos_adicionais para os IDs de clique da Meta.
# Ajuste conforme o nome real configurado no CV CRM, se existir.
SLUGS_LEADGEN_ID_CANDIDATOS = ["leadgen_id", "cf_leadgen_id", "meta_leadgen_id"]
SLUGS_CTWA_CLID_CANDIDATOS = ["ctwa_clid", "cf_ctwa_clid", "meta_ctwa_clid"]


# ---------------------------------------------------------------------------
# Estado (evita reprocessar os mesmos leads a cada execucao)
# ---------------------------------------------------------------------------

def carregar_estado() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"ultima_data_cancelamento_processada": "1970-01-01 00:00:00"}


def salvar_estado(estado: dict) -> None:
    STATE_FILE.write_text(json.dumps(estado, indent=2))


# ---------------------------------------------------------------------------
# Descoberta do ID da situacao "Cancelado"
# ---------------------------------------------------------------------------

def obter_id_situacao_cancelado() -> int:
    """
    Busca as situacoes do workflow de Leads e retorna o id daquela com
    flag "cancelada". Assume funcionalidade="leads" -- ajuste se necessario.
    """
    url = f"{CV_CRM_BASE_URL}/v1/workflows/leads"
    response = requests.get(url, headers=HEADERS, timeout=15)
    response.raise_for_status()
    situacoes = response.json()

    # A estrutura exata de retorno pode variar; tentamos alguns formatos comuns.
    lista = situacoes.get("situacoes") if isinstance(situacoes, dict) else situacoes

    for situacao in lista:
        flag = (situacao.get("flag") or "").lower()
        if flag in ("cancelada", "cancelado"):
            return situacao["id"]

    raise RuntimeError(
        "Nao foi possivel encontrar a situacao com flag 'cancelada' no "
        "workflow de leads. Verifique o nome da funcionalidade e a "
        "estrutura de retorno do endpoint /workflows/leads."
    )


# ---------------------------------------------------------------------------
# Busca de leads cancelados
# ---------------------------------------------------------------------------

def buscar_leads_cancelados(idsituacao_cancelado: int, limit: int = 50) -> list:
    """
    Pagina pelo endpoint de leads filtrando por idsituacao. Retorna todos os
    leads encontrados (sem filtrar motivo ainda -- isso e feito depois).
    """
    leads = []
    offset = 0

    while True:
        params = {
            "idsituacao": idsituacao_cancelado,
            "limit": limit,
            "offset": offset,
        }
        url = f"{CV_CRM_BASE_URL}/v1/comercial/leads"
        response = requests.get(url, headers=HEADERS, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        pagina_leads = data.get("leads", [])
        leads.extend(pagina_leads)

        if len(pagina_leads) < limit:
            break
        offset += limit

    return leads


# ---------------------------------------------------------------------------
# Mapeamento CV CRM -> payload esperado pelo enviar_lead_desqualificado
# ---------------------------------------------------------------------------

def extrair_campo_adicional(campos_adicionais: list, slugs_candidatos: list) -> str | None:
    for campo in campos_adicionais or []:
        if campo.get("slug") in slugs_candidatos:
            return campo.get("valor")
    return None


def identificar_origem(lead: dict) -> str:
    """
    Usa midia_principal/midias para inferir a origem (lead_ads, whatsapp,
    form_lp). Ajuste os termos de busca conforme o que aparece de fato nas
    midias cadastradas para a Vista Coqueiral.
    """
    midia = (lead.get("midia_principal") or "").lower()
    midias = [m.lower() for m in (lead.get("midias") or [])]
    todas_midias = [midia] + midias

    if any("whatsapp" in m for m in todas_midias):
        return "whatsapp"
    if any("lead ad" in m or "facebook" in m or "instagram" in m for m in todas_midias):
        return "lead_ads"
    return "form_lp"


def mapear_lead(lead_cv: dict) -> dict:
    campos_adicionais = lead_cv.get("campos_adicionais", [])

    return {
        "origem": identificar_origem(lead_cv),
        "leadgen_id": extrair_campo_adicional(campos_adicionais, SLUGS_LEADGEN_ID_CANDIDATOS),
        "ctwa_clid": extrair_campo_adicional(campos_adicionais, SLUGS_CTWA_CLID_CANDIDATOS),
        "email": lead_cv.get("email"),
        "telefone": lead_cv.get("telefone"),
        "motivo_cancelamento": (lead_cv.get("motivo_cancelamento") or {}).get("nome"),
    }


# ---------------------------------------------------------------------------
# Execucao principal
# ---------------------------------------------------------------------------

def main() -> None:
    estado = carregar_estado()
    ultima_data_processada = estado["ultima_data_cancelamento_processada"]

    idsituacao_cancelado = obter_id_situacao_cancelado()
    leads = buscar_leads_cancelados(idsituacao_cancelado)

    maior_data_cancelamento = ultima_data_processada
    enviados = 0
    ignorados_motivo = 0

    for lead_cv in leads:
        data_cancelamento = lead_cv.get("data_cancelamento", "")

        # So processa leads cancelados depois da ultima execucao
        if data_cancelamento <= ultima_data_processada:
            continue

        motivo = (lead_cv.get("motivo_cancelamento") or {}).get("nome")
        if motivo not in MOTIVOS_DESQUALIFICACAO_ALVO:
            ignorados_motivo += 1
            continue

        lead_mapeado = mapear_lead(lead_cv)
        resultado = enviar_lead_desqualificado(lead_mapeado)
        print(f"Lead {lead_cv.get('idlead')} -> {resultado}")
        enviados += 1

        if data_cancelamento > maior_data_cancelamento:
            maior_data_cancelamento = data_cancelamento

    estado["ultima_data_cancelamento_processada"] = maior_data_cancelamento
    salvar_estado(estado)

    print(f"Concluido. Enviados: {enviados}. Ignorados (motivo fora do alvo): {ignorados_motivo}.")


if __name__ == "__main__":
    main()

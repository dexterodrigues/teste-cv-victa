"""
Evento LeadDesqualificado -> Meta Conversions API (CAPI)
Cliente: Victa Engenharia - Vista Coqueiral
Dataset (pixel): pixel-coqueiral (ID 1901452027112190)

CONTEXTO
--------
Este modulo deve ser chamado a partir do seu polling existente do CV CRM,
sempre que um lead mudar de status para "desqualificado" E o campo
"Motivo de Cancelamento" for um dos motivos-alvo abaixo.

Motivos que disparam o evento (definido com o cliente):
    - Impossivel contatar
    - Nao deseja ser contatada
    - Nao tem perfil Financeiro
    - Engano

Origem do lead determina qual match key usar (em ordem de prioridade):
    1. Lead Ads (Meta)      -> lead_id (leadgen_id) em user_data.lead_id
    2. WhatsApp (CTWA)      -> ctwa_clid em user_data.ctwa_clid
    3. Formulario de LP     -> email + telefone hasheados (fallback)

Nao envia fbc/fbp: canal principal (Lead Ads + WhatsApp) nao depende deles.
LP e secundaria, entao nao ha script de captura client-side implementado.
"""

import hashlib
import os
import time
import uuid
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Configuracao
# ---------------------------------------------------------------------------

PIXEL_ID = "1901452027112190"  # pixel-coqueiral
GRAPH_API_VERSION = "v20.0"
ACCESS_TOKEN = os.environ["META_CAPI_ACCESS_TOKEN"]  # nunca hardcode o token

CAPI_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PIXEL_ID}/events"

# Motivos de cancelamento do CV CRM que devem gerar o evento negativo.
# Ajustar aqui caso o cliente inclua/remova motivos no futuro.
MOTIVOS_DESQUALIFICACAO_ALVO = {
    "Impossível contatar",
    "Não deseja ser contatada",
    "Não tem perfil Financeiro",
    "Engano",
}


# ---------------------------------------------------------------------------
# Helpers de hashing (padrao Meta: sha256 de string normalizada)
# ---------------------------------------------------------------------------

def _normalize(value: str) -> str:
    return value.strip().lower()


def _sha256(value: str) -> str:
    return hashlib.sha256(_normalize(value).encode("utf-8")).hexdigest()


def hash_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    return _sha256(email)


def hash_phone(phone: Optional[str]) -> Optional[str]:
    """Espera telefone em formato E.164 (ex: 5585999999999), sem '+' ou espacos."""
    if not phone:
        return None
    digits_only = "".join(ch for ch in phone if ch.isdigit())
    return _sha256(digits_only)


# ---------------------------------------------------------------------------
# Construcao do payload
# ---------------------------------------------------------------------------

def build_user_data(lead: dict) -> dict:
    """
    Espera um dict `lead` vindo do seu pipeline de polling do CV CRM, com
    (pelo menos um destes preenchido, conforme a origem):
        lead["origem"]        -> "lead_ads" | "whatsapp" | "form_lp"
        lead["leadgen_id"]    -> string, se origem == lead_ads
        lead["ctwa_clid"]     -> string, se origem == whatsapp
        lead["email"]         -> string, se origem == form_lp
        lead["telefone"]      -> string, se origem == form_lp
    Ajuste os nomes de chave para bater com o dict real do seu pipeline.
    """
    user_data = {}

    origem = lead.get("origem")

    if origem == "lead_ads" and lead.get("leadgen_id"):
        user_data["lead_id"] = lead["leadgen_id"]

    elif origem == "whatsapp" and lead.get("ctwa_clid"):
        user_data["ctwa_clid"] = lead["ctwa_clid"]

    # Email/telefone sempre que disponiveis, mesmo como reforco adicional
    # (nao atrapalha o match, so melhora).
    email_hash = hash_email(lead.get("email"))
    phone_hash = hash_phone(lead.get("telefone"))
    if email_hash:
        user_data["em"] = [email_hash]
    if phone_hash:
        user_data["ph"] = [phone_hash]

    return user_data


def build_event_payload(lead: dict) -> dict:
    user_data = build_user_data(lead)

    action_source = "business_messaging" if lead.get("origem") == "whatsapp" else "system_generated"

    event = {
        "event_name": "LeadDesqualificado",
        "event_time": int(time.time()),
        "event_id": str(uuid.uuid4()),  # dedup, caso reenviado
        "action_source": action_source,
        "value": 0,
        "currency": "BRL",
        "user_data": user_data,
    }

    if lead.get("origem") == "whatsapp":
        event["messaging_channel"] = "whatsapp"

    return {"data": [event]}


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------

def enviar_lead_desqualificado(lead: dict) -> dict:
    """
    Chame esta funcao a partir do seu loop de polling do CV CRM quando:
        lead["motivo_cancelamento"] in MOTIVOS_DESQUALIFICACAO_ALVO
    """
    motivo = lead.get("motivo_cancelamento")
    if motivo not in MOTIVOS_DESQUALIFICACAO_ALVO:
        return {"skipped": True, "motivo": motivo}

    payload = build_event_payload(lead)
    params = {"access_token": ACCESS_TOKEN}

    response = requests.post(CAPI_URL, params=params, json=payload, timeout=10)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Exemplo de uso (dentro do seu polling existente)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    exemplo_lead = {
        "origem": "whatsapp",
        "ctwa_clid": "AbCdEfGhIjKlMnOp",
        "email": "lead@exemplo.com",
        "telefone": "5585999999999",
        "motivo_cancelamento": "Impossível contatar",
    }

    resultado = enviar_lead_desqualificado(exemplo_lead)
    print(resultado)

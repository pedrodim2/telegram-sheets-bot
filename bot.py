import os
import re
import json
import requests
from datetime import datetime
from math import pow

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# =========================
# CONFIG (ENV VARS)
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SHEET_TAB_NAME = os.environ.get("SHEET_TAB_NAME", "Registros")

# Projeções: taxa fixa
DAILY_RATE = 0.0128       # 1.28% ao dia
DAYS_PER_MONTH = 22       # 22 dias por mês

# =========================
# GOOGLE SHEETS (Render-safe)
# =========================
def ensure_service_account_file():
    """
    Espera a env var GOOGLE_SERVICE_JSON com o JSON inteiro da service account.
    Cria o arquivo service_account.json em runtime (ideal p/ Render).
    """
    p = "service_account.json"
    if os.path.exists(p):
        return
    raw = os.environ.get("GOOGLE_SERVICE_JSON")
    if not raw:
        raise RuntimeError("Falta a env var GOOGLE_SERVICE_JSON com o JSON da Service Account.")
    data = json.loads(raw)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f)

def gs_client():
    ensure_service_account_file()
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file("service_account.json", scopes=scopes)
    return gspread.authorize(creds)

def worksheet():
    client = gs_client()
    sh = client.open_by_key(GOOGLE_SHEET_ID)
    return sh.worksheet(SHEET_TAB_NAME)

def get_all_rows():
    ws = worksheet()
    values = ws.get_all_values()
    if len(values) <= 1:
        return [], []
    return values[0], values[1:]  # header, rows

def append_row_to_sheet(row_values):
    ws = worksheet()
    ws.append_row(row_values, value_input_option="USER_ENTERED")

def get_last_row():
    ws = worksheet()
    values = ws.get_all_values()
    if len(values) <= 1:
        return None
    return values[-1]

# =========================
# UTIL (números, cotação, cálculo)
# =========================
def _to_float(raw: str) -> float:
    if raw is None:
        return 0.0
    raw = raw.strip().replace("R$", "").replace("USDT", "").replace("USD", "").strip()

    # aceita 1.234,56
    if re.search(r"\d+,\d+", raw):
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", ".")

    m = re.search(r"-?\d+(\.\d+)?", raw)
    return float(m.group(0)) if m else 0.0

def get_usdbrl() -> float:
    # AwesomeAPI USD-BRL
    r = requests.get("https://economia.awesomeapi.com.br/last/USD-BRL", timeout=10)
    r.raise_for_status()
    return float(r.json()["USDBRL"]["bid"])

def calc_profit(initial, deposit, withdraw, final):
    # Ganhos = final + saques - depósitos - inicial
    profit = final + withdraw - deposit - initial
    donation = max(0.0, profit * 0.05)
    return profit, donation

def parse_message(text: str) -> dict:
    """
    Aceita:
    1) Linhas: Campo: valor
    2) Uma linha: chave=valor; chave=valor...
       Ex:
       cidade=Uberaba; nome=João; id=445; mes=02/2026; inicial=500; deposito=60.35; saque=24; final=522.65; obs=teste
    """
    text = text.strip()

    # Caso chave=valor
    if "=" in text and (";" in text or "\n" not in text):
        parts = [p.strip() for p in text.split(";") if p.strip()]
        data = {}
        for p in parts:
            if "=" not in p:
                continue
            k, v = p.split("=", 1)
            data[k.strip().lower()] = v.strip()

        return {
            "cidade": data.get("cidade", ""),
            "nome": data.get("nome", ""),
            "id_conta": data.get("id", data.get("id da conta", "")),
            "mes": data.get("mes", data.get("mês", data.get("mês transação", data.get("mes transação", "")))),
            "inicial": _to_float(data.get("inicial", data.get("transação inicial", data.get("transacao inicial", "0")))),
            "deposito": _to_float(data.get("deposito", data.get("depósito", "0"))),
            "saque": _to_float(data.get("saque", "0")),
            "final": _to_float(data.get("final", data.get("saldo final", "0"))),
            "obs": data.get("obs", data.get("observacao", data.get("observação", ""))),
        }

    # Caso linhas Campo: valor
    data = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        data[k.strip().lower()] = v.strip()

    def get_any(*keys, default=""):
        for k in keys:
            if k in data:
                return data[k]
        return default

    return {
        "cidade": get_any("cidade", default=""),
        "nome": get_any("nome", default=""),
        "id_conta": get_any("id da conta", "id", default=""),
        "mes": get_any("mês transação", "mes transação", "mês", "mes", default=""),
        "inicial": _to_float(get_any("transação inicial", "transacao inicial", "inicial", default="0")),
        "deposito": _to_float(get_any("depósito", "deposito", default="0")),
        "saque": _to_float(get_any("saque", default="0")),
        "final": _to_float(get_any("saldo final", "final", default="0")),
        "obs": get_any("observação", "observacao", "obs", default=""),
    }

def fmt_row_summary(r, hm):
    def get(col):
        idx = hm.get(col)
        return r[idx] if idx is not None and idx < len(r) else ""
    return (
        f"🗓 {get('Data/Hora')} | Cidade {get('Cidade')} | Conta {get('ID da Conta')} | Mês {get('Mês transação')}\n"
        f"Ganhos: {get('Ganhos período (USDT)')} USDT | 5%: {get('Doação 5% (USDT)')} USDT | BRL: R$ {get('Doação 5% (BRL)')}"
    )

# =========================
# PROJEÇÕES (leigo-friendly)
# =========================
def _num_list(args):
    """
    Extrai números mesmo se a pessoa escrever "1000 USD 10 meses aporte 100"
    """
    s = " ".join(args).lower()
    return [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", s)]

def _fator_mensal():
    return pow(1.0 + DAILY_RATE, DAYS_PER_MONTH)

def _format_money(x: float) -> str:
    # 2 casas; troca para padrão BR (apenas visual)
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _linhas_mensais_padrão(valores, meses_total):
    linhas = []
    limite = min(12, len(valores))
    for i in range(limite):
        m, saldo = valores[i]
        linhas.append(f"M{m}: {_format_money(saldo)} USDT")
    if meses_total > 12:
        linhas.append(f"... (+{meses_total-12} meses)")
    return "\n".join(linhas)

def _linhas_mensais_p3(valores, meses_total):
    linhas = []
    limite = min(12, len(valores))
    for i in range(limite):
        m, saldo, sacado = valores[i]
        linhas.append(f"M{m}: saldo {_format_money(saldo)} | sacado {_format_money(sacado)}")
    if meses_total > 12:
        linhas.append(f"... (+{meses_total-12} meses)")
    return "\n".join(linhas)

def projecao_1(inicial: float, meses: int):
    f = _fator_mensal()
    saldo = inicial
    valores = []
    for m in range(1, meses + 1):
        saldo *= f
        valores.append((m, saldo))
    return saldo, valores

def projecao_2(inicial: float, meses_total: int, aporte: float, meses_aporte: int):
    f = _fator_mensal()
    saldo = inicial
    valores = []
    for m in range(1, meses_total + 1):
        saldo *= f
        if m <= meses_aporte:
            saldo += aporte
        valores.append((m, saldo))
    return saldo, valores

def projecao_3(inicial: float, meses: int):
    """
    Todo mês:
    - rende 22 dias
    - calcula lucro do mês
    - saca 50% do lucro
    - reinveste 50%
    """
    f = _fator_mensal()
    saldo = inicial
    total_sacado = 0.0
    valores = []  # (mes, saldo, sacado_mes)

    for m in range(1, meses + 1):
        inicio = saldo
        fim = inicio * f
        lucro = fim - inicio

        sacado = max(0.0, lucro * 0.5)
        saldo = fim - sacado
        total_sacado += sacado

        valores.append((m, saldo, sacado))

    return saldo, total_sacado, valores

# =========================
# TELEGRAM COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot online (multiusuário)!\n\n"
        "📌 Para REGISTRAR na planilha, mande assim (em linhas):\n"
        "Cidade: Uberaba\n"
        "Nome: João\n"
        "ID da Conta: 445\n"
        "Mês transação: 02/2026\n"
        "Transação Inicial: 500\n"
        "Depósito: 60.35\n"
        "Saque: 24\n"
        "Saldo final: 522.65\n"
        "Observação: opcional\n\n"
        "📌 Ou em UMA LINHA:\n"
        "cidade=Uberaba; nome=João; id=445; mes=02/2026; inicial=500; deposito=60.35; saque=24; final=522.65; obs=opcional\n\n"
        "📌 Comandos úteis:\n"
        "/ultimo\n"
        "/meus\n"
        "/meus_resumo 02/2026\n"
        "/resumo 02/2026\n\n"
        "📈 Projeções (bem simples):\n"
        "/p1 1000 10  → só crescer por 10 meses\n"
        "/p2 1000 10 100 6  → aporta 100 por 6 meses e simula 10\n"
        "/p3 1000 10  → saca 50% do lucro mensal"
    )

async def ultimo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = get_last_row()
    if not row:
        await update.message.reply_text("Ainda não tem registros na planilha.")
        return
    header, _ = get_all_rows()
    hm = {h.strip(): i for i, h in enumerate(header)} if header else {}
    await update.message.reply_text("📌 Último registro:\n" + fmt_row_summary(row, hm))

async def resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /resumo 02/2026")
        return
    mes = context.args[0].strip()

    header, rows = get_all_rows()
    if not rows:
        await update.message.reply_text("Ainda não tem registros.")
        return

    hm = {h.strip(): i for i, h in enumerate(header)}
    idx_mes = hm.get("Mês transação")
    if idx_mes is None:
        await update.message.reply_text("Coluna 'Mês transação' não encontrada na planilha.")
        return

    total_ganhos = total_5usdt = total_5brl = 0.0
    count = 0

    for r in rows:
        if len(r) <= idx_mes:
            continue
        if r[idx_mes].strip() != mes:
            continue
        count += 1
        total_ganhos += _to_float(r[hm.get("Ganhos período (USDT)", 0)] if hm.get("Ganhos período (USDT)") is not None else "0")
        total_5usdt += _to_float(r[hm.get("Doação 5% (USDT)", 0)] if hm.get("Doação 5% (USDT)") is not None else "0")
        total_5brl += _to_float(r[hm.get("Doação 5% (BRL)", 0)] if hm.get("Doação 5% (BRL)") is not None else "0")

    if count == 0:
        await update.message.reply_text(f"Não encontrei registros para {mes}.")
        return

    await update.message.reply_text(
        f"📊 Resumo geral {mes}\n"
        f"Registros: {count}\n"
        f"Ganhos totais (USDT): {_format_money(total_ganhos)}\n"
        f"5% total (USDT): {_format_money(total_5usdt)}\n"
        f"5% total (BRL): R$ {_format_money(total_5brl)}"
    )

async def meus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    header, rows = get_all_rows()
    if not rows:
        await update.message.reply_text("Ainda não tem registros.")
        return

    hm = {h.strip(): i for i, h in enumerate(header)}
    idx_uid = hm.get("Telegram ID")
    if idx_uid is None:
        await update.message.reply_text("Coluna 'Telegram ID' não encontrada na planilha.")
        return

    mine = [r for r in rows if len(r) > idx_uid and r[idx_uid].strip() == user_id]
    if not mine:
        await update.message.reply_text("Você ainda não tem registros.")
        return

    last5 = mine[-5:]
    msg = "📌 Seus últimos registros:\n\n" + "\n\n".join(fmt_row_summary(r, hm) for r in last5)
    await update.message.reply_text(msg)

async def meus_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /meus_resumo 02/2026")
        return

    mes = context.args[0].strip()
    user_id = str(update.effective_user.id)

    header, rows = get_all_rows()
    if not rows:
        await update.message.reply_text("Ainda não tem registros.")
        return

    hm = {h.strip(): i for i, h in enumerate(header)}
    idx_uid = hm.get("Telegram ID")
    idx_mes = hm.get("Mês transação")
    if idx_uid is None or idx_mes is None:
        await update.message.reply_text("Colunas 'Telegram ID' e/ou 'Mês transação' não encontradas na planilha.")
        return

    total_ganhos = total_5usdt = total_5brl = 0.0
    count = 0

    for r in rows:
        if len(r) <= max(idx_uid, idx_mes):
            continue
        if r[idx_uid].strip() != user_id:
            continue
        if r[idx_mes].strip() != mes:
            continue
        count += 1
        total_ganhos += _to_float(r[hm.get("Ganhos período (USDT)", 0)] if hm.get("Ganhos período (USDT)") is not None else "0")
        total_5usdt += _to_float(r[hm.get("Doação 5% (USDT)", 0)] if hm.get("Doação 5% (USDT)") is not None else "0")
        total_5brl += _to_float(r[hm.get("Doação 5% (BRL)", 0)] if hm.get("Doação 5% (BRL)") is not None else "0")

    if count == 0:
        await update.message.reply_text(f"Você não tem registros em {mes}.")
        return

    await update.message.reply_text(
        f"📊 Seu resumo {mes}\n"
        f"Registros: {count}\n"
        f"Ganhos totais (USDT): {_format_money(total_ganhos)}\n"
        f"5% total (USDT): {_format_money(total_5usdt)}\n"
        f"5% total (BRL): R$ {_format_money(total_5brl)}"
    )

# ---- Projeções leigo-friendly ----
async def p1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nums = _num_list(context.args)
    if len(nums) < 2:
        await update.message.reply_text(
            "📌 Projeção 1 (simples)\n"
            "Use: /p1 1000 10\n"
            "➡️ Começa com 1000 e simula por 10 meses."
        )
        return

    inicial = nums[0]
    meses = int(nums[1])
    saldo_final, valores = projecao_1(inicial, meses)

    await update.message.reply_text(
        "📈 Projeção 1 — só crescimento\n"
        f"Você começou com: {_format_money(inicial)} USDT\n"
        f"Tempo: {meses} meses\n"
        "Regra: 22 dias/mês, 1.28% ao dia\n\n"
        f"🏁 Resultado final estimado: {_format_money(saldo_final)} USDT\n\n"
        "📌 Evolução mês a mês:\n" + _linhas_mensais_padrão(valores, meses)
    )

async def p2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nums = _num_list(context.args)
    if len(nums) < 4:
        await update.message.reply_text(
            "📌 Projeção 2 (com aporte)\n"
            "Use: /p2 1000 10 100 6\n\n"
            "➡️ Significa:\n"
            "- começa com 1000\n"
            "- simula 10 meses\n"
            "- aporta 100 por mês\n"
            "- só por 6 meses (depois para)"
        )
        return

    inicial = nums[0]
    meses_total = int(nums[1])
    aporte = nums[2]
    meses_aporte = int(nums[3])

    saldo_final, valores = projecao_2(inicial, meses_total, aporte, meses_aporte)

    await update.message.reply_text(
        "📈 Projeção 2 — com aporte\n"
        f"Você começou com: {_format_money(inicial)} USDT\n"
        f"Tempo: {meses_total} meses\n"
        f"Aporte: {_format_money(aporte)} USDT por mês (por {meses_aporte} meses)\n"
        "Regra: 22 dias/mês, 1.28% ao dia\n\n"
        f"🏁 Resultado final estimado: {_format_money(saldo_final)} USDT\n\n"
        "📌 Evolução mês a mês:\n" + _linhas_mensais_padrão(valores, meses_total)
    )

async def p3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nums = _num_list(context.args)
    if len(nums) < 2:
        await update.message.reply_text(
            "📌 Projeção 3 (sacar 50% do lucro)\n"
            "Use: /p3 1000 10\n\n"
            "➡️ Regra:\n"
            "- todo mês calcula o lucro\n"
            "- saca 50% do lucro\n"
            "- deixa 50% rendendo"
        )
        return

    inicial = nums[0]
    meses = int(nums[1])

    saldo_final, total_sacado, valores = projecao_3(inicial, meses)

    await update.message.reply_text(
        "📈 Projeção 3 — saca 50% do lucro mensal\n"
        f"Você começou com: {_format_money(inicial)} USDT\n"
        f"Tempo: {meses} meses\n"
        "Regra: 22 dias/mês, 1.28% ao dia\n"
        "Todo mês: saca 50% do lucro e reinveste 50%\n\n"
        f"🏁 Saldo final estimado: {_format_money(saldo_final)} USDT\n"
        f"💸 Total sacado no período: {_format_money(total_sacado)} USDT\n\n"
        "📌 Evolução mês a mês:\n" + _linhas_mensais_p3(valores, meses)
    )

# =========================
# MAIN MESSAGE HANDLER (registro na planilha)
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    try:
        f = parse_message(text)

        # validação mínima
        if not f["mes"]:
            await update.message.reply_text("Faltou o campo 'Mês transação' (ex: 02/2026).")
            return

        usdbrl = get_usdbrl()
        profit, donation = calc_profit(f["inicial"], f["deposito"], f["saque"], f["final"])
        donation_brl = donation * usdbrl

        # dados do usuário do Telegram
        tg_id = str(update.effective_user.id)
        tg_user = update.effective_user.username or ""
        if tg_user and not tg_user.startswith("@"):
            tg_user = "@" + tg_user

        now = datetime.now().strftime("%d/%m/%Y %H:%M")

        # Ordem deve bater com o cabeçalho da planilha
        row = [
            now,
            tg_id,
            tg_user,
            f["cidade"],
            f["nome"],
            f["id_conta"],
            f["mes"],
            round(f["inicial"], 2),
            round(f["deposito"], 2),
            round(f["saque"], 2),
            round(f["final"], 2),
            round(profit, 2),
            round(donation, 2),
            round(usdbrl, 4),
            round(donation_brl, 2),
            f["obs"],
        ]
        append_row_to_sheet(row)

        await update.message.reply_text(
            "✅ Registrado na planilha!\n"
            f"Cidade: {f['cidade'] or '-'}\n"
            f"Ganhos (USDT): {_format_money(profit)}\n"
            f"Doação 5% (USDT): {_format_money(donation)}\n"
            f"USD/BRL: {usdbrl:.4f}\n"
            f"Doação 5% (BRL): R$ {_format_money(donation_brl)}"
        )

    except Exception as e:
        await update.message.reply_text(
            "Não consegui processar.\n\n"
            "✅ Exemplo (uma linha):\n"
            "cidade=Uberaba; nome=João; id=445; mes=02/2026; inicial=500; deposito=60.35; saque=24; final=522.65; obs=opcional\n\n"
            f"Erro: {e}"
        )

# =========================
# APP START
# =========================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ultimo", ultimo))
    app.add_handler(CommandHandler("resumo", resumo))
    app.add_handler(CommandHandler("meus", meus))
    app.add_handler(CommandHandler("meus_resumo", meus_resumo))

    # projeções
    app.add_handler(CommandHandler("p1", p1))
    app.add_handler(CommandHandler("p2", p2))
    app.add_handler(CommandHandler("p3", p3))

    # mensagem “normal” (registro)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()

if __name__ == "__main__":
    main()

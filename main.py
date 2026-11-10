"""Google Sheets -> filtro de estoque -> e-mail. Python 3.10+."""
import argparse
import csv
import json
import logging
import os
from pathlib import Path
import smtplib
import ssl
import time
from email.message import EmailMessage

BASE = Path(__file__).resolve().parent
STATE = BASE / 'estado.json'
LOG = logging.getLogger('estoque')


def required(name):
    value = os.getenv(name, '').strip()
    if not value:
        raise ValueError(f'Preencha {name} no .env.')
    return value


def read_sheet():
    """Autentica com conta de serviço e lê somente a planilha configurada."""
    import gspread
    credentials = Path(required('GOOGLE_CREDENTIALS'))
    if not credentials.is_absolute():
        credentials = BASE / credentials
    client = gspread.service_account(
        filename=str(credentials),
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'],
    )
    client.set_timeout(20)
    sheet = client.open_by_key(required('SHEET_ID'))
    return sheet.worksheet(os.getenv('SHEET_TAB', 'Estoque')).get_all_records()


def low_stock(rows):
    """Valida todas as linhas antes de enviar; quantidade <= mínimo dispara."""
    alerts = []
    names = set()
    for number, row in enumerate(rows, start=2):
        if not any(str(value).strip() for value in row.values()):
            continue
        name = str(row.get('produto', '')).strip()
        if not name or name.casefold() in names:
            raise ValueError(f'Linha {number}: produto vazio ou duplicado.')
        names.add(name.casefold())
        values = []
        for column in ('quantidade', 'minimo'):
            raw = str(row.get(column, '')).strip()
            if not raw.isascii() or not raw.isdigit():
                raise ValueError(f'Linha {number}: {column} deve ser inteiro >= 0.')
            values.append(int(raw))
        quantity, minimum = values
        if quantity <= minimum:
            alerts.append({'produto': name, 'quantidade': quantity, 'minimo': minimum})
    return sorted(alerts, key=lambda item: item['produto'].casefold())


def message_body(alerts):
    lines = ['Produtos que precisam de reposição:', '']
    for item in alerts:
        lines.append(f"- {item['produto']}: {item['quantidade']} unidade(s); mínimo: {item['minimo']}")
    return '\n'.join(lines)


def send_email(body):
    """SMTP autenticado com TLS; nunca imprime a senha nos logs."""
    message = EmailMessage()
    message['Subject'] = 'Alerta de estoque baixo'
    message['From'] = required('SMTP_USER')
    message['To'] = required('EMAIL_TO')
    message.set_content(body)
    with smtplib.SMTP(required('SMTP_HOST'), int(os.getenv('SMTP_PORT', '587')), timeout=20) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(required('SMTP_USER'), required('SMTP_PASSWORD'))
        server.send_message(message)


def check(rows, dry_run=False):
    alerts = low_stock(rows)
    body = message_body(alerts)
    if dry_run:
        print(body if alerts else 'Nenhum produto com estoque baixo.')
        return  # Não envia nem altera o histórico na simulação.
    previous = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else None
    if alerts == previous:
        LOG.info('Estoque sem alterações; nenhum e-mail repetido.')
        return
    if alerts:
        send_email(body)  # Só registra o estado depois de o servidor aceitar.
        LOG.info('E-mail aceito pelo servidor SMTP com %s produto(s).', len(alerts))
    else:
        LOG.info('Nenhum produto com estoque baixo.')
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(alerts, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(STATE)


def main():
    from dotenv import load_dotenv
    load_dotenv(BASE / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', help='Executa uma consulta e termina.')
    parser.add_argument('--dry-run', action='store_true', help='Mostra a mensagem sem enviar e-mail.')
    parser.add_argument('--demo', action='store_true', help='Usa o CSV local, sem Google Sheets.')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    try:
        interval = int(os.getenv('CHECK_INTERVAL_SECONDS', '300'))
        if interval < 10:
            raise ValueError('CHECK_INTERVAL_SECONDS deve ser pelo menos 10.')
    except ValueError:
        LOG.error('Intervalo inválido; configure um inteiro de pelo menos 10 segundos.')
        return 1
    while True:
        try:
            if args.demo:
                with (BASE / 'estoque_exemplo.csv').open(encoding='utf-8-sig', newline='') as file:
                    rows = list(csv.DictReader(file))
            else:
                rows = read_sheet()
            check(rows, dry_run=args.dry_run)
        except Exception as error:
            # Não mostra o texto de respostas externas: pode conter dados privados.
            LOG.error('Consulta/envio falhou (%s). Confira rede, planilha e credenciais.', type(error).__name__)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(interval)  # Polling: novas tentativas mesmo após falha.


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nMonitor encerrado.')

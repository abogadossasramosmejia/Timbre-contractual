"""
Alerta de contratos firmados por la Alcaldía de San José de Cúcuta.

Consulta los datos abiertos de SECOP II (datos.gov.co, dataset jbjy-vk9h),
detecta los contratos con fecha de firma que aún no se han notificado y envía
un correo con el detalle. Guarda los contratos ya notificados en estado.json.

Variables de entorno:
  GMAIL_USER          Cuenta de Gmail que envía (p. ej. abogadossasramosmejia@gmail.com)
  GMAIL_APP_PASSWORD  Contraseña de aplicación de Gmail (16 caracteres)
  EMAIL_TO            Destinatario(s) separados por coma (por defecto GMAIL_USER)
  DRY_RUN=1           No envía correo; imprime lo que enviaría
"""

import csv
import html
import io
import json
import os
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

NIT_ALCALDIA = "890501434"  # MUNICIPIO DE SAN JOSE DE CUCUTA
DATASET_URL = "https://www.datos.gov.co/resource/jbjy-vk9h.json"
VENTANA_DIAS = 60  # SECOP publica con rezago; revisamos firmas de los últimos 60 días
STATE_FILE = os.environ.get("ESTADO_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "estado.json")
COLOMBIA = timezone(timedelta(hours=-5))

CAMPOS = [
    "id_contrato", "referencia_del_contrato", "fecha_de_firma", "objeto_del_contrato",
    "proveedor_adjudicado", "documento_proveedor", "valor_del_contrato",
    "modalidad_de_contratacion", "tipo_de_contrato", "estado_contrato",
    "duraci_n_del_contrato", "urlproceso",
]


def consultar_contratos(desde):
    """Devuelve los contratos de la Alcaldía firmados desde la fecha indicada."""
    resultados, offset, pagina = [], 0, 5000
    while True:
        params = {
            "$select": ",".join(CAMPOS),
            "$where": f"nit_entidad='{NIT_ALCALDIA}' AND fecha_de_firma >= '{desde}'",
            "$order": "fecha_de_firma DESC, id_contrato",
            "$limit": pagina,
            "$offset": offset,
        }
        url = DATASET_URL + "?" + urllib.parse.urlencode(params)
        lote = _get_json(url)
        resultados.extend(lote)
        if len(lote) < pagina:
            return resultados
        offset += pagina


def _get_json(url, intentos=4):
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "alerta-contratos-cucuta/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:  # red inestable o API lenta: reintentar
            if i == intentos - 1:
                raise
            print(f"Error consultando SECOP ({e}); reintentando...", file=sys.stderr)
            time.sleep(15 * (i + 1))


def cargar_estado():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def guardar_estado(notificados):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"actualizado": datetime.now(COLOMBIA).isoformat(timespec="seconds"),
             "notificados": dict(sorted(notificados.items()))},
            f, ensure_ascii=False, indent=1,
        )
        f.write("\n")


def pesos(valor):
    try:
        return "$" + f"{float(valor):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return valor or ""


def url_de(c):
    u = c.get("urlproceso")
    return u.get("url", "") if isinstance(u, dict) else (u or "")


def fecha_corta(f):
    return (f or "")[:16].replace("T", " ")


def construir_correo(nuevos):
    n = len(nuevos)
    total = sum(float(c.get("valor_del_contrato") or 0) for c in nuevos)
    asunto = f"🔔 {n} contrato{'s' if n != 1 else ''} nuevo{'s' if n != 1 else ''} firmado{'s' if n != 1 else ''} – Alcaldía de Cúcuta"

    filas_txt, filas_html = [], []
    for c in nuevos:
        objeto = " ".join((c.get("objeto_del_contrato") or "").split())
        filas_txt.append(
            f"- {fecha_corta(c.get('fecha_de_firma'))} | {c.get('referencia_del_contrato', '')} | "
            f"{pesos(c.get('valor_del_contrato'))}\n  Contratista: {c.get('proveedor_adjudicado', '')}\n"
            f"  Objeto: {objeto}\n  {url_de(c)}\n"
        )
        e = lambda k: html.escape(str(c.get(k) or ""))
        filas_html.append(f"""
<tr>
 <td style="padding:10px;border-bottom:1px solid #ddd;vertical-align:top;white-space:nowrap">{html.escape(fecha_corta(c.get('fecha_de_firma')))}<br><small>{e('referencia_del_contrato')}</small></td>
 <td style="padding:10px;border-bottom:1px solid #ddd;vertical-align:top">
   <b>{html.escape(objeto[:400])}{'…' if len(objeto) > 400 else ''}</b><br>
   <small>Contratista: {e('proveedor_adjudicado')} ({e('documento_proveedor')})<br>
   {e('modalidad_de_contratacion')} · {e('tipo_de_contrato')} · {e('duraci_n_del_contrato')} · Estado: {e('estado_contrato')}</small><br>
   <a href="{html.escape(url_de(c))}">Ver en SECOP II</a>
 </td>
 <td style="padding:10px;border-bottom:1px solid #ddd;vertical-align:top;text-align:right;white-space:nowrap"><b>{pesos(c.get('valor_del_contrato'))}</b></td>
</tr>""")

    texto = (f"{n} contrato(s) nuevo(s) firmados por la Alcaldía de Cúcuta. Valor total: {pesos(total)}\n\n"
             + "\n".join(filas_txt) + "\nFuente: SECOP II – datos.gov.co")
    cuerpo_html = f"""<div style="font-family:Arial,sans-serif;color:#222">
<h2 style="margin:0 0 4px">Alcaldía de San José de Cúcuta</h2>
<p style="margin:0 0 16px">{n} contrato(s) nuevo(s) firmados · Valor total: <b>{pesos(total)}</b></p>
<table style="border-collapse:collapse;width:100%;font-size:14px">
<tr style="background:#f2f2f2"><th style="padding:8px;text-align:left">Firma</th><th style="padding:8px;text-align:left">Contrato</th><th style="padding:8px;text-align:right">Valor</th></tr>
{''.join(filas_html)}
</table>
<p style="color:#777;font-size:12px">Fuente: SECOP II – datos abiertos (datos.gov.co). Se adjunta CSV con el detalle.</p>
</div>"""

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["fecha_firma", "referencia", "objeto", "contratista", "documento", "valor",
                "modalidad", "tipo", "estado", "duracion", "id_contrato", "url"])
    for c in nuevos:
        w.writerow([c.get("fecha_de_firma"), c.get("referencia_del_contrato"),
                    " ".join((c.get("objeto_del_contrato") or "").split()),
                    c.get("proveedor_adjudicado"), c.get("documento_proveedor"),
                    c.get("valor_del_contrato"), c.get("modalidad_de_contratacion"),
                    c.get("tipo_de_contrato"), c.get("estado_contrato"),
                    c.get("duraci_n_del_contrato"), c.get("id_contrato"), url_de(c)])
    return asunto, texto, cuerpo_html, buf.getvalue()


def enviar_correo(asunto, texto, cuerpo_html, csv_texto=None):
    usuario = os.environ["GMAIL_USER"]
    clave = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    destino = os.environ.get("EMAIL_TO") or usuario
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = asunto, f"Alerta Contratos Cúcuta <{usuario}>", destino
    msg.set_content(texto)
    msg.add_alternative(cuerpo_html, subtype="html")
    if csv_texto:
        nombre = f"contratos_cucuta_{datetime.now(COLOMBIA):%Y%m%d_%H%M}.csv"
        msg.add_attachment(("﻿" + csv_texto).encode("utf-8"), maintype="text",
                           subtype="csv", filename=nombre)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(usuario, clave)
        s.send_message(msg)


def cuerpo_issue(intro, contratos, max_items=60):
    """Texto Markdown del aviso que GitHub envía por correo."""
    dueno = os.environ.get("GITHUB_REPOSITORY_OWNER")
    partes = [(f"@{dueno} " if dueno else "") + intro, ""]
    for c in contratos[:max_items]:
        objeto = " ".join((c.get("objeto_del_contrato") or "").split())
        if len(objeto) > 500:
            objeto = objeto[:500] + "…"
        partes.append(
            f"### {pesos(c.get('valor_del_contrato'))} · {c.get('referencia_del_contrato', '')}\n"
            f"**Firma:** {fecha_corta(c.get('fecha_de_firma'))}  \n"
            f"**Contratista:** {c.get('proveedor_adjudicado', '')} ({c.get('documento_proveedor', '')})  \n"
            f"**Objeto:** {objeto}  \n"
            f"{c.get('modalidad_de_contratacion', '')} · {c.get('tipo_de_contrato', '')} · "
            f"{c.get('duraci_n_del_contrato', '')} · Estado: {c.get('estado_contrato', '')}  \n"
            f"[Ver en SECOP II]({url_de(c)})\n"
        )
    if len(contratos) > max_items:
        partes.append(f"_…y {len(contratos) - max_items} contrato(s) más. Consulte SECOP II._")
    partes.append("\n---\nFuente: SECOP II – datos abiertos (datos.gov.co).")
    return "\n".join(partes)


def crear_issue(titulo, cuerpo):
    """Publica el aviso como 'issue' del repositorio; GitHub lo envía por correo al dueño."""
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=json.dumps({"title": titulo, "body": cuerpo}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "alerta-contratos-cucuta/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        print("Aviso publicado:", json.load(r).get("html_url"))


def avisar(dry, asunto, texto, cuerpo_html, csv_texto, contratos, intro_md):
    """Envía por Gmail si hay contraseña de aplicación; si no, por notificación de GitHub."""
    if dry:
        print("[DRY_RUN] Enviaría:", asunto)
        print(cuerpo_issue(intro_md, contratos)[:3000])
    elif os.environ.get("GMAIL_APP_PASSWORD") and os.environ.get("GMAIL_USER"):
        enviar_correo(asunto, texto, cuerpo_html, csv_texto)
        print("Correo enviado por Gmail.")
    elif os.environ.get("GITHUB_TOKEN"):
        crear_issue(asunto, cuerpo_issue(intro_md, contratos))
    else:
        raise SystemExit("No hay forma de avisar: configure GMAIL_APP_PASSWORD o ejecute en GitHub Actions.")


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    desde_dt = datetime.now(COLOMBIA) - timedelta(days=VENTANA_DIAS)
    desde = desde_dt.strftime("%Y-%m-%dT00:00:00")

    contratos = consultar_contratos(desde)
    print(f"Contratos firmados desde {desde[:10]}: {len(contratos)}")

    estado = cargar_estado()
    primera_vez = estado is None
    notificados = {} if primera_vez else estado.get("notificados", {})

    nuevos = [c for c in contratos if c.get("id_contrato") and c["id_contrato"] not in notificados]
    print(f"Nuevos sin notificar: {len(nuevos)}{' (primera ejecución)' if primera_vez else ''}")

    if primera_vez:
        # No inundar el correo con el histórico: se registra todo y se avisa que quedó activo.
        asunto = "✅ Alerta de contratos de la Alcaldía de Cúcuta activada"
        ultimos = contratos[:10]
        texto = (f"La alerta quedó activa. Se registraron {len(contratos)} contratos firmados en los "
                 f"últimos {VENTANA_DIAS} días como punto de partida. Desde ahora recibirá un correo "
                 f"cada vez que aparezcan contratos nuevos en SECOP II.")
        _, _, tabla_html, _ = construir_correo(ultimos) if ultimos else ("", "", "", "")
        cuerpo = f"<p style='font-family:Arial'>{html.escape(texto)}</p><p style='font-family:Arial'>Últimos 10 contratos firmados (de muestra):</p>{tabla_html}"
        avisar(dry, asunto, texto, cuerpo, None, ultimos, texto + "\n\n**Últimos 10 contratos firmados (muestra):**")
    elif nuevos:
        nuevos.sort(key=lambda c: c.get("fecha_de_firma") or "", reverse=True)
        asunto, texto, cuerpo, csv_texto = construir_correo(nuevos)
        total = sum(float(c.get("valor_del_contrato") or 0) for c in nuevos)
        avisar(dry, asunto, texto, cuerpo, csv_texto, nuevos,
               f"{len(nuevos)} contrato(s) nuevo(s) firmados por la Alcaldía de Cúcuta · "
               f"Valor total: **{pesos(total)}**")

    for c in contratos:
        if c.get("id_contrato"):
            notificados.setdefault(c["id_contrato"], (c.get("fecha_de_firma") or "")[:10])
    # Olvidar contratos que ya salieron de la ventana (con margen) para que el archivo no crezca.
    limite = (desde_dt - timedelta(days=30)).strftime("%Y-%m-%d")
    notificados = {k: v for k, v in notificados.items() if v >= limite}

    if not dry:
        guardar_estado(notificados)


if __name__ == "__main__":
    main()

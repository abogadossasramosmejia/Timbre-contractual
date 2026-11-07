"""
Alerta de contratos firmados por la Alcaldía de San José de Cúcuta y sus entidades.

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

# Entidades vigiladas (NIT tal como aparece en SECOP II -> nombre corto para los avisos)
ENTIDADES = {
    "890501434": "Alcaldía de Cúcuta",
    "807001836": "IMRD (Instituto Municipal de Recreación y Deporte)",
    "807004352": "ESE IMSALUD",
    "8905014335": "Central de Transportes Estación Cúcuta",
    "890500529": "EIS Cúcuta S.A. E.S.P.",
    "800153197": "Área Metropolitana de Cúcuta",
    "890505717": "Concejo Municipal de Cúcuta",
    "8001924348": "Personería Municipal de Cúcuta",
    "800056348": "Contraloría Municipal de Cúcuta",
}
NIT_ALCALDIA = "890501434"
DATASET_URL = "https://www.datos.gov.co/resource/jbjy-vk9h.json"
VENTANA_DIAS = 60  # SECOP publica con rezago; revisamos firmas de los últimos 60 días
STATE_FILE = os.environ.get("ESTADO_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "estado.json")
COLOMBIA = timezone(timedelta(hours=-5))

CAMPOS = [
    "nit_entidad", "id_contrato", "referencia_del_contrato", "fecha_de_firma", "objeto_del_contrato",
    "proveedor_adjudicado", "documento_proveedor", "valor_del_contrato",
    "modalidad_de_contratacion", "tipo_de_contrato", "estado_contrato",
    "duraci_n_del_contrato", "urlproceso",
]


def consultar_contratos(desde):
    """Devuelve los contratos de las entidades vigiladas firmados desde la fecha indicada."""
    resultados, offset, pagina = [], 0, 5000
    nits = ",".join(f"'{n}'" for n in ENTIDADES)
    while True:
        params = {
            "$select": ",".join(CAMPOS),
            "$where": f"nit_entidad in({nits}) AND fecha_de_firma >= '{desde}'",
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
             "entidades": sorted(ENTIDADES),
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


def entidad_de(c):
    return ENTIDADES.get(c.get("nit_entidad"), c.get("nit_entidad", ""))


def resumen_por_entidad(contratos):
    cuenta = {}
    for c in contratos:
        cuenta[entidad_de(c)] = cuenta.get(entidad_de(c), 0) + 1
    return ", ".join(f"{k}: {v}" for k, v in sorted(cuenta.items(), key=lambda x: -x[1]))


def construir_correo(nuevos):
    n = len(nuevos)
    total = sum(float(c.get("valor_del_contrato") or 0) for c in nuevos)
    asunto = f"🔔 {n} contrato{'s' if n != 1 else ''} nuevo{'s' if n != 1 else ''} firmado{'s' if n != 1 else ''} – Cúcuta (Alcaldía y entidades)"

    filas_txt, filas_html = [], []
    for c in nuevos:
        objeto = " ".join((c.get("objeto_del_contrato") or "").split())
        filas_txt.append(
            f"- [{entidad_de(c)}] {fecha_corta(c.get('fecha_de_firma'))} | {c.get('referencia_del_contrato', '')} | "
            f"{pesos(c.get('valor_del_contrato'))}\n  Contratista: {c.get('proveedor_adjudicado', '')}\n"
            f"  Objeto: {objeto}\n  {url_de(c)}\n"
        )
        e = lambda k: html.escape(str(c.get(k) or ""))
        filas_html.append(f"""
<tr>
 <td style="padding:10px;border-bottom:1px solid #ddd;vertical-align:top;white-space:nowrap">{html.escape(fecha_corta(c.get('fecha_de_firma')))}<br><small>{e('referencia_del_contrato')}</small></td>
 <td style="padding:10px;border-bottom:1px solid #ddd;vertical-align:top">
   <span style="color:#0b5cad;font-size:12px;font-weight:bold">{html.escape(entidad_de(c))}</span><br>
   <b>{html.escape(objeto[:400])}{'…' if len(objeto) > 400 else ''}</b><br>
   <small>Contratista: {e('proveedor_adjudicado')} ({e('documento_proveedor')})<br>
   {e('modalidad_de_contratacion')} · {e('tipo_de_contrato')} · {e('duraci_n_del_contrato')} · Estado: {e('estado_contrato')}</small><br>
   <a href="{html.escape(url_de(c))}">Ver en SECOP II</a>
 </td>
 <td style="padding:10px;border-bottom:1px solid #ddd;vertical-align:top;text-align:right;white-space:nowrap"><b>{pesos(c.get('valor_del_contrato'))}</b></td>
</tr>""")

    texto = (f"{n} contrato(s) nuevo(s) firmados en Cúcuta. Valor total: {pesos(total)}\n"
             f"Por entidad: {resumen_por_entidad(nuevos)}\n\n"
             + "\n".join(filas_txt) + "\nFuente: SECOP II – datos.gov.co")
    cuerpo_html = f"""<div style="font-family:Arial,sans-serif;color:#222">
<h2 style="margin:0 0 4px">Contratos nuevos – Cúcuta</h2>
<p style="margin:0 0 4px">{n} contrato(s) nuevo(s) firmados · Valor total: <b>{pesos(total)}</b></p>
<p style="margin:0 0 16px;color:#555;font-size:13px">{html.escape(resumen_por_entidad(nuevos))}</p>
<table style="border-collapse:collapse;width:100%;font-size:14px">
<tr style="background:#f2f2f2"><th style="padding:8px;text-align:left">Firma</th><th style="padding:8px;text-align:left">Contrato</th><th style="padding:8px;text-align:right">Valor</th></tr>
{''.join(filas_html)}
</table>
<p style="color:#777;font-size:12px">Fuente: SECOP II – datos abiertos (datos.gov.co). Se adjunta CSV con el detalle.</p>
</div>"""

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["entidad", "fecha_firma", "referencia", "objeto", "contratista", "documento", "valor",
                "modalidad", "tipo", "estado", "duracion", "id_contrato", "url"])
    for c in nuevos:
        w.writerow([entidad_de(c), c.get("fecha_de_firma"), c.get("referencia_del_contrato"),
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
            f"**Entidad:** {entidad_de(c)}  \n"
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
    # Entidades agregadas después de la primera ejecución: se registran sin avisar su histórico.
    ya_vigiladas = set(ENTIDADES) if primera_vez else set(estado.get("entidades", [NIT_ALCALDIA]))
    agregadas = [n for n in ENTIDADES if n not in ya_vigiladas]

    nuevos = [c for c in contratos if c.get("id_contrato") and c["id_contrato"] not in notificados
              and c.get("nit_entidad") not in agregadas]
    print(f"Nuevos sin notificar: {len(nuevos)}{' (primera ejecución)' if primera_vez else ''}")

    if primera_vez or agregadas:
        # No inundar el correo con el histórico: se registra todo y se avisa qué quedó vigilado.
        base = contratos if primera_vez else [c for c in contratos if c.get("nit_entidad") in agregadas]
        lista = [ENTIDADES[n] for n in (ENTIDADES if primera_vez else agregadas)]
        asunto = ("✅ Alerta de contratos de Cúcuta activada" if primera_vez
                  else f"✅ Se agregaron {len(agregadas)} entidades a la alerta de contratos")
        texto = (f"Entidades {'vigiladas' if primera_vez else 'agregadas'}: {'; '.join(lista)}. "
                 f"Se registraron {len(base)} contratos firmados en los últimos {VENTANA_DIAS} días como "
                 f"punto de partida ({resumen_por_entidad(base) or 'ninguno'}). Desde ahora recibirá un "
                 f"aviso cada vez que aparezcan contratos nuevos en SECOP II.")
        ultimos = base[:10]
        _, _, tabla_html, _ = construir_correo(ultimos) if ultimos else ("", "", "", "")
        cuerpo = f"<p style='font-family:Arial'>{html.escape(texto)}</p><p style='font-family:Arial'>Últimos contratos (muestra):</p>{tabla_html}"
        avisar(dry, asunto, texto, cuerpo, None, ultimos, texto + "\n\n**Últimos contratos firmados (muestra):**")
    if nuevos and not primera_vez:
        nuevos.sort(key=lambda c: c.get("fecha_de_firma") or "", reverse=True)
        asunto, texto, cuerpo, csv_texto = construir_correo(nuevos)
        total = sum(float(c.get("valor_del_contrato") or 0) for c in nuevos)
        avisar(dry, asunto, texto, cuerpo, csv_texto, nuevos,
               f"{len(nuevos)} contrato(s) nuevo(s) firmados en Cúcuta · Valor total: **{pesos(total)}**  \n"
               f"Por entidad: {resumen_por_entidad(nuevos)}")

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

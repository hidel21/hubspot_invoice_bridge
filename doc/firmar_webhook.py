#!/usr/bin/env python3
"""Firma y envía un webhook simulado de HubSpot contra el endpoint local.

Reproduce exactamente la firma X-HubSpot-Signature-v3 que valida el
controlador: HMAC-SHA256, en Base64, sobre la concatenación

    método + URI completa + cuerpo crudo + timestamp

La URI debe ser la misma que Odoo reconstruye, es decir la URL pública
configurada en Ajustes (o web.base.url) más la ruta del endpoint. Si no
coinciden, la firma no cuadra y el endpoint responde 401.

Uso:
    python3 firmar_webhook.py --secret SECRETO --body evento_ejemplo.json
    python3 firmar_webhook.py --secret SECRETO --body evento_ejemplo.json --enviar

Sin --enviar imprime la orden curl equivalente, por si se prefiere lanzarla
a mano. Solo usa la biblioteca estándar.
"""

import argparse
import base64
import hashlib
import hmac
import shlex
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = 'http://localhost:8069/hubspot/webhook/deal'


def firmar(secret, method, url, body, timestamp):
    source = method.encode() + url.encode() + body + str(timestamp).encode()
    digest = hmac.new(secret.encode(), source, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--secret', required=True,
                        help="Client secret configurado en Odoo.")
    parser.add_argument('--url', default=DEFAULT_URL,
                        help="URL completa del endpoint (por defecto %s)." % DEFAULT_URL)
    parser.add_argument('--body', required=True,
                        help="Fichero JSON con el array de eventos.")
    parser.add_argument('--metodo', default='POST')
    parser.add_argument('--timestamp', type=int, default=None,
                        help="Epoch en milisegundos. Por defecto, ahora. "
                             "Reste mas de 300000 para probar el rechazo "
                             "por peticion caducada.")
    parser.add_argument('--enviar', action='store_true',
                        help="Lanza la peticion en lugar de imprimir el curl.")
    args = parser.parse_args()

    with open(args.body, 'rb') as handle:
        body = handle.read()

    timestamp = args.timestamp if args.timestamp is not None else int(time.time() * 1000)
    signature = firmar(args.secret, args.metodo, args.url, body, timestamp)

    headers = {
        'Content-Type': 'application/json',
        'X-HubSpot-Signature-v3': signature,
        'X-HubSpot-Request-Timestamp': str(timestamp),
    }

    if not args.enviar:
        partes = ['curl', '-i', '-X', args.metodo, args.url]
        for clave, valor in headers.items():
            partes += ['-H', '%s: %s' % (clave, valor)]
        partes += ['--data-binary', '@' + args.body]
        print(' '.join(shlex.quote(p) for p in partes))
        return 0

    peticion = urllib.request.Request(
        args.url, data=body, headers=headers, method=args.metodo)
    try:
        with urllib.request.urlopen(peticion, timeout=30) as respuesta:
            print('HTTP %s' % respuesta.status)
            print(respuesta.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as err:
        print('HTTP %s' % err.code)
        print(err.read().decode('utf-8', 'replace'))
        return 1
    except urllib.error.URLError as err:
        print('No se pudo conectar con %s: %s' % (args.url, err.reason))
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

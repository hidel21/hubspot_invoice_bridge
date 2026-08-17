import base64
import hashlib
import hmac
import json
import logging
import time

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# HubSpot descarta como replay cualquier petición de más de 5 minutos.
MAX_REQUEST_AGE_MS = 5 * 60 * 1000


class HubspotWebhookController(http.Controller):
    """Punto de entrada de los webhooks de HubSpot.

    Se usa ``type='http'`` deliberadamente: ``type='json'`` en Odoo espera un
    sobre JSON-RPC con clave ``params``, mientras que HubSpot envía un array
    JSON plano.

    El handler hace lo mínimo —validar la firma y encolar— porque HubSpot
    espera un 2xx en unos 5 segundos y reintenta durante 24 h si no lo recibe.
    Crear la factura aquí provocaría timeouts y eventos duplicados.
    """

    @http.route(
        '/hubspot/webhook/deal',
        type='http', auth='public', methods=['POST'], csrf=False, save_session=False)
    def hubspot_deal_webhook(self, **kwargs):
        raw_body = request.httprequest.get_data()

        error = self._verify_signature(raw_body)
        if error:
            _logger.warning("Webhook de HubSpot rechazado: %s", error)
            # 401 hace que HubSpot reintente; es lo correcto ante un desajuste
            # transitorio de configuración y es inocuo ante una petición falsa.
            return self._respond({'status': 'unauthorized', 'detail': error}, 401)

        try:
            payload = json.loads(raw_body.decode('utf-8') or '[]')
        except (ValueError, UnicodeDecodeError) as err:
            _logger.warning("Webhook de HubSpot con cuerpo ilegible: %s", err)
            # 400: el cuerpo no va a mejorar con un reintento.
            return self._respond({'status': 'bad_request'}, 400)

        # HubSpot siempre agrupa los eventos en un array.
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            return self._respond({'status': 'bad_request'}, 400)

        events = request.env['hubspot.webhook.event'].sudo().ingest_batch(payload)
        _logger.info(
            "Webhook de HubSpot: %s eventos recibidos, %s nuevos encolados.",
            len(payload), len(events))

        return self._respond({'status': 'ok', 'queued': len(events)}, 200)

    # ------------------------------------------------------------------

    def _respond(self, data, status):
        return request.make_json_response(data, status=status)

    def _verify_signature(self, raw_body):
        """Valida ``X-HubSpot-Signature-v3``.

        La firma es HMAC-SHA256, en Base64, sobre la concatenación
        ``método + URI completa + cuerpo crudo + timestamp``, usando el client
        secret como clave. Debe usarse el cuerpo **tal cual llegó**: volver a
        serializar el JSON cambia los bytes y la firma deja de coincidir.

        Devuelve ``None`` si la petición es válida, o el motivo del rechazo.
        """
        params = request.env['ir.config_parameter'].sudo()
        if params.get_param('hubspot_invoice_bridge.verify_signature', 'True') == 'False':
            _logger.warning(
                "La validación de firma de HubSpot está DESACTIVADA. "
                "No debe usarse así en producción.")
            return None

        secret = params.get_param('hubspot_invoice_bridge.client_secret', '')
        if not secret:
            return "no hay client secret configurado"

        signature = request.httprequest.headers.get('X-HubSpot-Signature-v3')
        if not signature:
            return "falta la cabecera X-HubSpot-Signature-v3"

        timestamp = request.httprequest.headers.get('X-HubSpot-Request-Timestamp')
        if not timestamp:
            return "falta la cabecera X-HubSpot-Request-Timestamp"

        try:
            age = int(time.time() * 1000) - int(timestamp)
        except (TypeError, ValueError):
            return "timestamp con formato inválido"
        if age > MAX_REQUEST_AGE_MS:
            return "petición caducada (%.0f s de antigüedad)" % (age / 1000.0)

        uri = self._get_request_uri()
        source = (
            request.httprequest.method.encode()
            + uri.encode()
            + raw_body
            + str(timestamp).encode()
        )
        expected = base64.b64encode(
            hmac.new(secret.encode(), source, hashlib.sha256).digest()
        ).decode()

        if not hmac.compare_digest(expected, signature):
            _logger.debug(
                "Firma no coincidente. URI usada para el cálculo: %s", uri)
            return "firma no válida (revise el client secret y la URL pública)"

        return None

    def _get_request_uri(self):
        """URI exacta que HubSpot usó para firmar.

        Detrás de un proxy inverso, ``request.httprequest.url`` refleja la URL
        interna (http://localhost:8070/...) y no la pública, por lo que la firma
        nunca coincidiría. Por eso se reconstruye a partir de la URL pública
        configurada.
        """
        httprequest = request.httprequest
        params = request.env['ir.config_parameter'].sudo()
        base = (params.get_param('hubspot_invoice_bridge.webhook_base_url', '')
                or params.get_param('web.base.url', '')).rstrip('/')
        if not base:
            return httprequest.url

        uri = base + httprequest.path
        if httprequest.query_string:
            uri += '?' + httprequest.query_string.decode()
        return uri

import logging
import time

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

BASE_URL = 'https://api.hubapi.com'
TIMEOUT = 30

# HubSpot limita las private apps a ~110 peticiones / 10 s. Se reintenta con
# backoff ante 429 y ante errores 5xx transitorios.
RETRY_STATUSES = (429, 502, 503, 504)
MAX_RETRIES = 4


class HubspotClient(models.AbstractModel):
    """Envoltorio fino sobre la API v3/v4 del CRM de HubSpot.

    Se implementa como AbstractModel (y no como clase suelta) para que sea
    extensible por herencia y tenga acceso natural a ``self.env``.
    """

    _name = 'hubspot.client'
    _description = 'Cliente de la API de HubSpot'

    # ------------------------------------------------------------------
    # Transporte
    # ------------------------------------------------------------------

    @api.model
    def _get_access_token(self):
        token = self.env['ir.config_parameter'].sudo().get_param(
            'hubspot_invoice_bridge.access_token', '')
        if not token:
            raise UserError(_(
                "No hay access token de HubSpot configurado. "
                "Ajustes → Contabilidad → Integración HubSpot."))
        return token

    @api.model
    def _request(self, method, path, params=None, payload=None):
        url = path if path.startswith('http') else BASE_URL + path
        headers = {
            'Authorization': 'Bearer %s' % self._get_access_token(),
            'Content-Type': 'application/json',
        }
        delay = 1
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = requests.request(
                    method, url, headers=headers, params=params, json=payload,
                    timeout=TIMEOUT,
                )
            except requests.RequestException as err:
                last_error = str(err)
                _logger.warning(
                    "HubSpot %s %s falló (intento %s/%s): %s",
                    method, url, attempt + 1, MAX_RETRIES, err)
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code in RETRY_STATUSES:
                last_error = "HTTP %s: %s" % (response.status_code, response.text[:500])
                # HubSpot devuelve Retry-After en algunos 429.
                wait = int(response.headers.get('Retry-After') or delay)
                _logger.warning(
                    "HubSpot %s %s devolvió %s, reintentando en %ss",
                    method, url, response.status_code, wait)
                time.sleep(wait)
                delay *= 2
                continue

            if response.status_code == 404:
                return None

            if not response.ok:
                raise UserError(_(
                    "Error de la API de HubSpot en %(method)s %(path)s:\n"
                    "HTTP %(status)s — %(body)s",
                    method=method, path=path,
                    status=response.status_code, body=response.text[:1000],
                ))

            if not response.content:
                return {}
            return response.json()

        raise UserError(_(
            "La API de HubSpot no respondió tras %(tries)s intentos "
            "(%(method)s %(path)s): %(error)s",
            tries=MAX_RETRIES, method=method, path=path, error=last_error,
        ))

    # ------------------------------------------------------------------
    # Objetos del CRM
    # ------------------------------------------------------------------

    @api.model
    def get_object(self, object_type, object_id, properties=None, with_history=None):
        params = {}
        if properties:
            params['properties'] = ','.join(properties)
        if with_history:
            params['propertiesWithHistory'] = ','.join(with_history)
        return self._request(
            'GET', '/crm/v3/objects/%s/%s' % (object_type, object_id), params=params)

    @api.model
    def batch_read(self, object_type, object_ids, properties):
        """Lee varios registros en una sola llamada (máx. 100 por lote)."""
        results = []
        ids = [str(i) for i in object_ids]
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            data = self._request(
                'POST', '/crm/v3/objects/%s/batch/read' % object_type,
                payload={
                    'properties': list(properties),
                    'inputs': [{'id': i} for i in chunk],
                },
            ) or {}
            results.extend(data.get('results') or [])
        return results

    @api.model
    def get_associated_ids(self, from_type, from_id, to_type):
        """IDs asociados usando el endpoint dedicado de la v4.

        Se usa ``/crm/v4/objects/{from}/{id}/associations/{to}`` en lugar del
        parámetro ``?associations=`` de la v3: la v4 devuelve además los tipos
        de asociación, lo que permite distinguir la asociación primaria.

        Devuelve una lista de tuplas ``(id, is_primary)`` respetando el orden
        de HubSpot.
        """
        path = '/crm/v4/objects/%s/%s/associations/%s' % (from_type, from_id, to_type)
        out = []
        params = {'limit': 500}
        while True:
            data = self._request('GET', path, params=params) or {}
            for row in data.get('results') or []:
                to_id = row.get('toObjectId')
                if not to_id:
                    continue
                labels = [
                    (t.get('label') or '') for t in (row.get('associationTypes') or [])
                ]
                is_primary = any('primary' in (label or '').lower() for label in labels)
                out.append((str(to_id), is_primary))
            after = (data.get('paging') or {}).get('next', {}).get('after')
            if not after:
                break
            params = {'limit': 500, 'after': after}
        return out

    # ------------------------------------------------------------------
    # Pipelines
    # ------------------------------------------------------------------

    @api.model
    def get_deal_pipelines(self):
        data = self._request('GET', '/crm/v3/pipelines/deals') or {}
        return data.get('results') or []

    # ------------------------------------------------------------------
    # Escritura opcional de vuelta a HubSpot
    # ------------------------------------------------------------------

    @api.model
    def update_deal_properties(self, deal_id, properties):
        return self._request(
            'PATCH', '/crm/v3/objects/deals/%s' % deal_id,
            payload={'properties': properties})

"""Pruebas técnicas del endpoint: quién puede meter trabajo en la cola.

El webhook es público y sin sesión. Lo único que separa a HubSpot de
cualquiera que conozca la URL es la firma, así que estas pruebas valen lo que
vale la puerta: si alguna se pusiera verde cuando debe estar roja, bastaría
con conocer la dirección para provocar presupuestos.

Son de extremo a extremo de verdad: levantan el servidor y hacen la petición
HTTP, no llaman al método por dentro.
"""

import base64
import hashlib
import hmac
import json
import time

from odoo.tests.common import HttpCase, tagged

RUTA = "/hubspot/webhook/deal"
SECRETO = "secreto-de-prueba-no-usado-en-ningun-portal"


@tagged("post_install", "-at_install")
class TestFirmaDelWebhook(HttpCase):

    def setUp(self):
        super().setUp()
        self.param = self.env["ir.config_parameter"].sudo()
        self.param.set_param("hubspot_invoice_bridge.client_secret", SECRETO)
        self.param.set_param("hubspot_invoice_bridge.verify_signature", "True")
        # La firma se calcula sobre la URI pública, que detrás de un proxy no
        # es la que ve Odoo. En la prueba, la pública es la del propio
        # servidor de test.
        self.param.set_param(
            "hubspot_invoice_bridge.webhook_base_url", self.base_url())
        # Sin commit, a propósito. En modo test la petición HTTP comparte el
        # cursor, así que estos valores ya se ven desde el controlador, y al
        # terminar la transacción se deshace entera. Eso importa: si esta
        # prueba confirmara, dejaría un client secret inventado en la base
        # contra la que se ejecute.

    # ── utilidades ───────────────────────────────────────────────────
    def _cuerpo(self, identificador="firma-1"):
        return json.dumps([{
            "eventId": identificador,
            "subscriptionType": "deal.propertyChange",
            "objectId": "88000111222",
            "propertyName": "dealstage",
            "propertyValue": "closedwon",
            "changeSource": "CRM_UI",
        }]).encode()

    def _firmar(self, cuerpo, marca, secreto=SECRETO):
        origen = b"POST" + (self.base_url() + RUTA).encode() + cuerpo + marca.encode()
        return base64.b64encode(
            hmac.new(secreto.encode(), origen, hashlib.sha256).digest()).decode()

    def _enviar(self, cuerpo=None, firma=None, marca=None, cabeceras=None):
        """Envía la petición. `False` en firma o marca significa «sin esa cabecera».

        La marca que se firma y la que se envía se llevan por separado: para
        probar que falta la cabecera hay que firmar igual, o no habría nada
        que rechazar.
        """
        cuerpo = cuerpo if cuerpo is not None else self._cuerpo()
        ahora = str(int(time.time() * 1000))
        marca_firmada = marca if isinstance(marca, str) else ahora
        firma = firma if firma is not None else self._firmar(cuerpo, marca_firmada)

        cab = {"Content-Type": "application/json"}
        if firma is not False:
            cab["X-HubSpot-Signature-v3"] = firma
        if marca is not False:
            cab["X-HubSpot-Request-Timestamp"] = marca_firmada
        cab.update(cabeceras or {})
        return self.url_open(RUTA, data=cuerpo, headers=cab, timeout=30)

    def _eventos(self):
        return self.env["hubspot.webhook.event"].search_count(
            [("object_id", "=", "88000111222")])

    # ── lo que debe pasar ────────────────────────────────────────────
    def test_una_peticion_bien_firmada_entra(self):
        respuesta = self._enviar()
        self.assertEqual(respuesta.status_code, 200, respuesta.text)
        self.assertEqual(respuesta.json().get("status"), "ok")
        self.assertEqual(self._eventos(), 1)

    # ── lo que NO debe pasar ─────────────────────────────────────────
    def test_sin_firma_se_rechaza(self):
        respuesta = self._enviar(firma=False)
        self.assertEqual(respuesta.status_code, 401)
        self.assertIn("Signature", respuesta.json().get("detail", ""))
        self.assertEqual(self._eventos(), 0)

    def test_con_firma_inventada_se_rechaza(self):
        respuesta = self._enviar(firma="esto-no-es-una-firma")
        self.assertEqual(respuesta.status_code, 401)
        self.assertEqual(self._eventos(), 0)

    def test_firmada_con_otro_secreto_se_rechaza(self):
        cuerpo = self._cuerpo()
        marca = str(int(time.time() * 1000))
        respuesta = self._enviar(
            cuerpo=cuerpo, marca=marca,
            firma=self._firmar(cuerpo, marca, secreto="otro-secreto"))
        self.assertEqual(respuesta.status_code, 401)
        self.assertEqual(self._eventos(), 0)

    def test_el_cuerpo_no_se_puede_cambiar_despues_de_firmar(self):
        """La firma cubre el cuerpo: alterarlo la invalida.

        Es lo que impide que alguien intercepte una petición buena y le cambie
        el negocio por otro.
        """
        original = self._cuerpo()
        marca = str(int(time.time() * 1000))
        firma = self._firmar(original, marca)
        alterado = original.replace(b"88000111222", b"88000999999")
        respuesta = self._enviar(cuerpo=alterado, firma=firma, marca=marca)
        self.assertEqual(respuesta.status_code, 401)

    def test_una_peticion_vieja_se_rechaza(self):
        """Sin esto, una petición válida capturada serviría para siempre."""
        vieja = str(int(time.time() * 1000) - 48 * 60 * 60 * 1000)
        respuesta = self._enviar(marca=vieja)
        self.assertEqual(respuesta.status_code, 401)
        self.assertIn("caducada", respuesta.json().get("detail", ""))
        self.assertEqual(self._eventos(), 0)

    def test_sin_marca_de_tiempo_se_rechaza(self):
        respuesta = self._enviar(marca=False)
        self.assertEqual(respuesta.status_code, 401)
        self.assertEqual(self._eventos(), 0)

    def test_una_marca_que_no_es_un_numero_se_rechaza(self):
        respuesta = self._enviar(marca="ayer")
        self.assertEqual(respuesta.status_code, 401)
        self.assertEqual(self._eventos(), 0)

    def test_sin_secreto_configurado_no_entra_nada(self):
        """Mejor rechazarlo todo que aceptar sin comprobar."""
        self.param.set_param("hubspot_invoice_bridge.client_secret", "")
        respuesta = self._enviar()
        self.assertEqual(respuesta.status_code, 401)
        self.assertEqual(self._eventos(), 0)

    # ── el mismo evento dos veces ────────────────────────────────────
    def test_un_reenvio_de_hubspot_no_duplica(self):
        """HubSpot reintenta si no recibe el 200 a tiempo."""
        cuerpo = self._cuerpo("firma-reenvio")
        marca = str(int(time.time() * 1000))
        firma = self._firmar(cuerpo, marca)
        primera = self._enviar(cuerpo=cuerpo, firma=firma, marca=marca)
        segunda = self._enviar(cuerpo=cuerpo, firma=firma, marca=marca)
        self.assertEqual(primera.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        self.assertEqual(self._eventos(), 1)

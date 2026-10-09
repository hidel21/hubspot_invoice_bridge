"""Prueba de aceptación: de un negocio ganado a un presupuesto en Odoo.

Estas no miran una función suelta: ponen un negocio entero de HubSpot delante
del módulo y comprueban lo que sale por el otro lado. Es lo que de verdad
importa a quien factura — que el cliente, los productos, el descuento y la
moneda sean los que estaban en el CRM.

HubSpot se sustituye por un portal de mentira que responde lo mismo que la
API real. Así la prueba no depende de la red, del token ni de que alguien
cambie un negocio en el portal.
"""

from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

CLIENTE = "odoo.addons.hubspot_invoice_bridge.models.hubspot_client.HubspotClient"

NEGOCIO = "99000111222"
EMPRESA = "99000333444"
COTIZACION = "99000555666"


class PortalFalso:
    """Responde como la API de HubSpot, con lo justo para este negocio.

    Se construye a partir de un diccionario para que cada prueba pueda cambiar
    un dato —quitar la empresa, vaciar las cotizaciones— sin reescribirlo todo.
    """

    def __init__(self, deal=None, empresa=None, lineas=None,
                 empresas_asociadas=None, cotizaciones_asociadas=None):
        self.deal = deal if deal is not None else {
            "dealname": "Negocio de aceptación",
            "dealstage": "closedwon",
            "pipeline": "default",
            "deal_currency_code": "USD",
            "cotizaciones_aprobadas": "20260720-110220277",
        }
        self.empresa = empresa if empresa is not None else {
            "name": "ZETAPRUEBA ACEPTACION SAS",
            "nit": "999000111",
            "address": "Calle Falsa 123",
            "phone": "6011112222",
        }
        self.lineas = lineas if lineas is not None else [
            {"id": "L1", "properties": {
                "name": "ZETAPRUEBA Reloj de aceptación",
                "hs_sku": "ZP-ACEPT-01",
                "quantity": "4",
                "price": "250",
                "hs_discount_percentage": "10",
            }},
        ]
        self.empresas_asociadas = (
            empresas_asociadas if empresas_asociadas is not None
            else [(EMPRESA, True)])
        self.cotizaciones_asociadas = (
            cotizaciones_asociadas if cotizaciones_asociadas is not None
            else [(COTIZACION, True)])

    # ── las tres llamadas que hace el módulo ─────────────────────────
    def get_object(self, tipo, ident, properties=None, with_history=None):
        if tipo == "deals":
            return {"id": ident, "properties": self.deal}
        if tipo == "companies":
            return {"id": ident, "properties": self.empresa}
        return {"id": ident, "properties": {}}

    def get_associated_ids(self, desde, ident, hacia):
        if hacia == "companies":
            return self.empresas_asociadas
        if hacia == "quotes":
            return self.cotizaciones_asociadas
        if hacia == "line_items":
            return [(l["id"], False) for l in self.lineas]
        if hacia == "contacts":
            return []
        return []

    def batch_read(self, tipo, ids, properties):
        if tipo == "quotes":
            return [{"id": COTIZACION, "properties": {
                "hs_quote_number": "20260720-110220277",
                "hs_status": "APPROVAL_NOT_NEEDED"}}]
        if tipo == "line_items":
            return [l for l in self.lineas if l["id"] in ids]
        return []


@tagged("post_install", "-at_install")
class TestFlujoCompleto(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.param = cls.env["ir.config_parameter"].sudo()
        cls.compania = cls.env.company

        # Un portal se factura a una compañía con su diario y su tarifa.
        diario = cls.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", cls.compania.id)],
            limit=1)
        cls.env["hubspot.location.mapping"].search([]).write({"active": False})
        cls.env["hubspot.location.mapping"].create({
            "name": "Aceptación",
            "company_id": cls.compania.id,
            "journal_id": diario.id,
            "is_default": True,
        })

        cls.usd = cls.env["res.currency"].with_context(
            active_test=False).search([("name", "=", "USD")], limit=1)
        cls.usd.active = True
        cls.env["product.pricelist"].create({
            "name": "ZETAPRUEBA aceptación USD",
            "currency_id": cls.usd.id,
            "company_id": cls.compania.id,
        })

        # La etapa que dispara, y el producto que el negocio vende.
        cls.env["hubspot.deal.stage"].search(
            [("stage_id", "=", "closedwon")]).unlink()
        cls.env["hubspot.deal.stage"].create({
            "stage_id": "closedwon",
            "label": "Cierre ganado",
            "pipeline_id": "default",
            "pipeline_label": "Pipeline de ventas",
            "trigger_invoice": True,
        })
        cls.producto = cls.env["product.product"].create({
            "name": "ZETAPRUEBA Reloj de aceptación",
            "default_code": "ZP-ACEPT-01",
        })

        cls.param.set_param("hubspot_invoice_bridge.document_mode", "sale_order")
        cls.param.set_param("hubspot_invoice_bridge.trigger_mode", "stage")
        cls.param.set_param(
            "hubspot_invoice_bridge.deal_flag_property", "cotizaciones_aprobadas")
        cls.param.set_param("hubspot_invoice_bridge.sync_contacts", "False")

    def _identificador(self):
        """Un id distinto por evento.

        No vale cr.now(): dentro de una transacción PostgreSQL devuelve
        siempre la misma hora, así que dos eventos seguidos chocarían contra
        la restricción de unicidad.
        """
        self.__class__._contador = getattr(self.__class__, "_contador", 0) + 1
        return "aceptacion-%d" % self.__class__._contador

    def _procesar(self, portal=None):
        portal = portal or PortalFalso()
        evento = self.Evento.create({
            "hubspot_event_id": self._identificador(),
            "subscription_type": "deal.propertyChange",
            "object_id": NEGOCIO,
            "property_name": "dealstage",
            "property_value": "closedwon",
            "change_source": "CRM_UI",
            "state": "pending",
        })
        with patch(CLIENTE + ".get_object", side_effect=portal.get_object), \
             patch(CLIENTE + ".get_associated_ids",
                   side_effect=portal.get_associated_ids), \
             patch(CLIENTE + ".batch_read", side_effect=portal.batch_read):
            evento._process_one()
        evento.invalidate_recordset()
        return evento

    # ── el camino feliz ──────────────────────────────────────────────
    def test_un_negocio_ganado_genera_su_presupuesto(self):
        evento = self._procesar()
        self.assertEqual(evento.state, "done", evento.error_message or "")
        self.assertTrue(evento.sale_order_id)

    def test_el_presupuesto_queda_en_borrador(self):
        """Confirmar reserva existencias: esa decisión es de quien factura."""
        pedido = self._procesar().sale_order_id
        self.assertEqual(pedido.state, "draft")

    def test_el_cliente_es_la_empresa_del_negocio(self):
        pedido = self._procesar().sale_order_id
        self.assertEqual(pedido.partner_id.hubspot_company_id, EMPRESA)
        self.assertEqual(pedido.partner_id.vat, "999000111")

    def test_el_cliente_nuevo_nace_marcado(self):
        pedido = self._procesar().sale_order_id
        self.assertTrue(pedido.partner_id.name.startswith("[By HubSpot]"))

    def test_la_linea_trae_producto_cantidad_precio_y_descuento(self):
        pedido = self._procesar().sale_order_id
        self.assertEqual(len(pedido.order_line), 1)
        linea = pedido.order_line
        self.assertEqual(linea.product_id, self.producto)
        # La cantidad se compara con tolerancia, y no por capricho: la
        # precisión decimal de la unidad de medida está en 15 dígitos, así que
        # Odoo redondea con un factor de 1e-15 y un 4 limpio sale como
        # 4.000000000000004. El ruido es del sistema, no del módulo, y se ve
        # también en los pedidos reales.
        self.assertAlmostEqual(linea.product_uom_qty, 4, places=6)
        self.assertEqual(linea.price_unit, 250)
        self.assertEqual(linea.discount, 10)

    def test_el_importe_sale_de_las_lineas(self):
        """4 × 250 − 10% = 900."""
        pedido = self._procesar().sale_order_id
        self.assertAlmostEqual(pedido.amount_untaxed, 900.0, places=2)

    def test_el_pedido_sale_en_la_moneda_del_negocio(self):
        pedido = self._procesar().sale_order_id
        self.assertEqual(pedido.currency_id, self.usd)

    def test_queda_marcado_el_origen_y_el_negocio(self):
        pedido = self._procesar().sale_order_id
        self.assertEqual(pedido.hubspot_deal_id, NEGOCIO)
        self.assertEqual(pedido.source_id.name, "By HubSpot")

    # ── no duplicar ──────────────────────────────────────────────────
    def test_dos_eventos_del_mismo_negocio_dan_un_solo_pedido(self):
        """El barrido reencola un negocio cada vez que alguien lo toca."""
        primero = self._procesar()
        segundo = self._procesar()
        self.assertEqual(segundo.state, "done")
        self.assertEqual(segundo.sale_order_id, primero.sale_order_id)
        self.assertEqual(
            self.env["sale.order"].search_count(
                [("hubspot_deal_id", "=", NEGOCIO)]), 1)

    # ── lo que debe parar, y decir por qué ───────────────────────────
    def test_sin_empresa_asociada_entra_igual(self):
        """Cambió el criterio el 09-10-2026: antes se paraba.

        Hay negocios que no tienen empresa asociada, y dejarlos fuera
        significaba perderlos sin que nadie se enterara. Ahora entran, con la
        ficha a medias y marcada para completar. Los escalones concretos se
        prueban en TestNegocioSinEmpresa.
        """
        evento = self._procesar(PortalFalso(empresas_asociadas=[]))
        self.assertEqual(evento.state, "done", evento.error_message or "")
        self.assertTrue(evento.sale_order_id)
        self.assertTrue(evento.sale_order_id.partner_id)

    def test_un_producto_sin_homologar_para_el_documento_entero(self):
        """Un presupuesto a medias es peor que ninguno: nadie se entera."""
        portal = PortalFalso(lineas=[{"id": "L9", "properties": {
            "name": "Producto que no existe en Odoo",
            "hs_sku": "NO-EXISTE",
            "quantity": "1", "price": "100"}}])
        evento = self._procesar(portal)
        self.assertEqual(evento.state, "error")
        self.assertIn("homologar", evento.error_message)
        self.assertFalse(
            self.env["sale.order"].search([("hubspot_deal_id", "=", NEGOCIO)]))

    def test_una_cotizacion_de_otro_negocio_para_el_proceso(self):
        """Traer productos de otro cliente sería peor que no facturar."""
        portal = PortalFalso()
        portal.deal = dict(portal.deal,
                           cotizaciones_aprobadas="20260101-999999999")
        evento = self._procesar(portal)
        self.assertEqual(evento.state, "error")
        self.assertIn("no están asociadas", evento.error_message)

    def test_una_importacion_masiva_no_factura(self):
        evento = self.Evento.create({
            "hubspot_event_id": self._identificador(),
            "subscription_type": "deal.propertyChange",
            "object_id": NEGOCIO,
            "change_source": "IMPORT",
            "state": "pending",
        })
        evento._process_one()
        evento.invalidate_recordset()
        self.assertEqual(evento.state, "skipped")
        self.assertFalse(evento.sale_order_id)


@tagged("post_install", "-at_install")
class TestNegocioSinEmpresa(TestFlujoCompleto):
    """Un negocio sin empresa asociada tiene que entrar igual.

    Antes se quedaba parado para siempre y se perdía. Un negocio ganado que no
    llega a Odoo es peor que uno con la ficha a medias, porque nadie se entera
    de que falta.
    """

    def _portal_sin_empresa(self, contactos=None):
        portal = PortalFalso(empresas_asociadas=[])
        portal.contactos = contactos if contactos is not None else [
            ("77000111", True)]
        portal.contacto = {
            "firstname": "Jhon Jairo",
            "lastname": "Bustos Espinosa",
            "email": "jhon@zetaprueba.test",
            "phone": "3001112233",
            "jobtitle": "Gerente",
        }

        get_ids = portal.get_associated_ids
        get_obj = portal.get_object

        def asociaciones(desde, ident, hacia):
            if hacia == "contacts":
                return portal.contactos
            return get_ids(desde, ident, hacia)

        def objetos(tipo, ident, properties=None, with_history=None):
            if tipo == "contacts":
                return {"id": ident, "properties": portal.contacto}
            return get_obj(tipo, ident, properties, with_history)

        portal.get_associated_ids = asociaciones
        portal.get_object = objetos
        return portal

    def test_se_factura_al_contacto_del_negocio(self):
        evento = self._procesar(self._portal_sin_empresa())
        self.assertEqual(evento.state, "done", evento.error_message or "")
        cliente = evento.sale_order_id.partner_id
        self.assertIn("Jhon Jairo Bustos Espinosa", cliente.name)
        self.assertEqual(cliente.hubspot_contact_id, "77000111")
        self.assertEqual(cliente.email, "jhon@zetaprueba.test")
        self.assertFalse(cliente.is_company)

    def test_el_contacto_nace_marcado(self):
        """Le falta el NIT igual que a una empresa nueva."""
        evento = self._procesar(self._portal_sin_empresa())
        self.assertTrue(
            evento.sale_order_id.partner_id.name.startswith("[By HubSpot]"))

    def test_sin_empresa_ni_contacto_se_usa_el_nombre_del_negocio(self):
        evento = self._procesar(self._portal_sin_empresa(contactos=[]))
        self.assertEqual(evento.state, "done", evento.error_message or "")
        self.assertIn(
            "Negocio de aceptación", evento.sale_order_id.partner_id.name)

    def test_un_contacto_que_ya_existe_no_se_duplica(self):
        existente = self.env["res.partner"].create({
            "name": "ZETAPRUEBA Jhon ya existente",
            "email": "jhon@zetaprueba.test",
        })
        evento = self._procesar(self._portal_sin_empresa())
        self.assertEqual(evento.sale_order_id.partner_id, existente)
        existente.invalidate_recordset()
        self.assertEqual(existente.hubspot_contact_id, "77000111")

    def test_si_el_contacto_pertenece_a_una_empresa_se_factura_a_la_empresa(self):
        empresa = self.env["res.partner"].create({
            "name": "ZETAPRUEBA Empresa del contacto", "is_company": True})
        self.env["res.partner"].create({
            "name": "ZETAPRUEBA Jhon de empresa",
            "parent_id": empresa.id,
            "hubspot_contact_id": "77000111",
        })
        evento = self._procesar(self._portal_sin_empresa())
        self.assertEqual(evento.sale_order_id.partner_id, empresa)

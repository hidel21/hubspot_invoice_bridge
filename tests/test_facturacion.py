"""Las decisiones que deciden cómo sale el documento.

Compañía y diario, moneda, tarifa, producto y si pasa o no por almacén. Son
las que, cuando fallan, no dan error: dan un documento con el importe o el
cliente equivocados, que se descubre al cobrarlo.
"""

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestLocalizacion(TransactionCase):
    """A qué compañía y con qué diario se factura."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.Mapeo = cls.env["hubspot.location.mapping"]
        cls.diario = cls.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", cls.env.company.id)],
            limit=1)
        cls.Mapeo.search([]).write({"active": False})
        cls.predeterminada = cls.Mapeo.create({
            "name": "Predeterminada de prueba",
            "company_id": cls.env.company.id,
            "journal_id": cls.diario.id,
            "is_default": True,
        })

    def test_sin_valor_cae_en_la_predeterminada(self):
        mapeo = self.Evento._resolve_location({})
        self.assertEqual(mapeo, self.predeterminada)

    def test_un_valor_con_linea_propia_gana_a_la_predeterminada(self):
        propia = self.Mapeo.create({
            "name": "JoobPay de prueba",
            "hubspot_value": "pipeline-joobpay",
            "company_id": self.env.company.id,
            "journal_id": self.diario.id,
        })
        mapeo = self.Evento._resolve_location({"pipeline": "pipeline-joobpay"})
        self.assertEqual(mapeo, propia)

    def test_sin_ninguna_linea_se_para_con_un_motivo(self):
        """Prefiere pararse a facturar a la compañía que no era."""
        self.Mapeo.search([]).write({"active": False})
        with self.assertRaises(UserError) as capturado:
            self.Evento._resolve_location({})
        self.assertIn("localización", str(capturado.exception))


@tagged("post_install", "-at_install")
class TestMonedaYTarifa(TransactionCase):
    """El fallo que daba pedidos en pesos con cifras en dólares."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.Moneda = cls.env["res.currency"]
        cls.usd = cls.Moneda.with_context(active_test=False).search(
            [("name", "=", "USD")], limit=1)
        cls.usd.active = True
        cls.propia = cls.env.company.currency_id

        diario = cls.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", cls.env.company.id)],
            limit=1)
        cls.mapeo = cls.env["hubspot.location.mapping"].create({
            "name": "Mapeo de prueba",
            "company_id": cls.env.company.id,
            "journal_id": diario.id,
        })
        cls.cliente = cls.env["res.partner"].create({
            "name": "ZETAPRUEBA MONEDA SAS", "is_company": True})

    def test_la_moneda_sale_del_negocio(self):
        moneda = self.Evento._resolve_currency(
            {"deal_currency_code": "USD"}, self.mapeo)
        self.assertEqual(moneda, self.usd)

    def test_sin_moneda_en_el_negocio_se_usa_la_de_la_compania(self):
        moneda = self.Evento._resolve_currency({}, self.mapeo)
        self.assertEqual(moneda, self.propia)

    def test_una_moneda_desconocida_no_rompe_el_documento(self):
        moneda = self.Evento._resolve_currency(
            {"deal_currency_code": "XYZ"}, self.mapeo)
        self.assertEqual(moneda, self.propia)

    def test_la_tarifa_la_fija_la_moneda(self):
        """En sale.order la moneda se calcula desde la tarifa, no se escribe.

        Por eso el reparto va por tarifa: escribir currency_id no daba error y
        tampoco hacía nada, y el pedido salía en pesos con precios en dólares.
        """
        self.env["product.pricelist"].search(
            [("currency_id", "=", self.usd.id)]).write({"active": False})
        tarifa = self.env["product.pricelist"].create({
            "name": "ZETAPRUEBA USD",
            "currency_id": self.usd.id,
            "company_id": self.env.company.id,
        })
        elegida = self.Evento._resolve_pricelist(
            self.usd, self.mapeo, self.cliente)
        self.assertEqual(elegida, tarifa)

    def test_sin_tarifa_en_esa_moneda_se_para(self):
        """Un importe mal no se nota al crearlo: se nota al cobrarlo."""
        rara = self.Moneda.with_context(active_test=False).search(
            [("name", "=", "JPY")], limit=1)
        if not rara:
            self.skipTest("No hay una moneda sin tarifa con la que probar")
        rara.active = True
        self.env["product.pricelist"].search(
            [("currency_id", "=", rara.id)]).unlink()
        with self.assertRaises(UserError) as capturado:
            self.Evento._resolve_pricelist(rara, self.mapeo, self.cliente)
        self.assertIn("tarifa", str(capturado.exception))


@tagged("post_install", "-at_install")
class TestProductos(TransactionCase):
    """Cómo se homologa un producto de HubSpot con uno de Odoo."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.compania = cls.env.company
        cls.producto = cls.env["product.product"].create({
            "name": "ZETAPRUEBA Reloj Biométrico",
            "default_code": "ZP-RELOJ-01",
        })

    def test_por_referencia_interna(self):
        hallado = self.Evento._resolve_product(
            {"hs_sku": "ZP-RELOJ-01"}, self.compania)
        self.assertEqual(hallado, self.producto)

    def test_por_nombre_exacto(self):
        hallado = self.Evento._resolve_product(
            {"name": "ZETAPRUEBA Reloj Biométrico"}, self.compania)
        self.assertEqual(hallado, self.producto)

    def test_el_vinculo_queda_memorizado(self):
        """La primera vez se busca; la siguiente es directa."""
        self.Evento._resolve_product(
            {"hs_sku": "ZP-RELOJ-01", "hs_product_id": "HS-123"}, self.compania)
        self.producto.invalidate_recordset()
        self.assertEqual(self.producto.hubspot_product_id, "HS-123")

    def test_el_vinculo_manda_sobre_el_nombre(self):
        self.producto.hubspot_product_id = "HS-456"
        hallado = self.Evento._resolve_product(
            {"hs_product_id": "HS-456", "name": "Otro nombre cualquiera"},
            self.compania)
        self.assertEqual(hallado, self.producto)

    def test_un_producto_desconocido_no_se_inventa(self):
        """Mejor parar que facturar un producto que no es.

        El proceso aborta entero y escribe cuáles no pudo homologar, para que
        alguien los dé de alta.
        """
        hallado = self.Evento._resolve_product(
            {"name": "Algo que no existe en el catálogo"}, self.compania)
        self.assertFalse(hallado)


@tagged("post_install", "-at_install")
class TestAlmacen(TransactionCase):
    """Si la venta lleva hardware, tiene que pasar por almacén."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.param = cls.env["ir.config_parameter"].sudo()
        cls.hardware = cls.env["product.product"].create({
            "name": "ZETAPRUEBA Lector facial",
            "is_storable": True,
        })
        cls.servicio = cls.env["product.product"].create({
            "name": "ZETAPRUEBA Licencia anual",
            "type": "service",
        })

    def _lineas(self, *productos):
        return [{"product_id": p.id} for p in productos]

    def test_con_hardware_pasa_por_almacen(self):
        self.param.set_param("hubspot_invoice_bridge.document_mode", "auto")
        self.assertTrue(self.Evento._needs_warehouse(self._lineas(self.hardware)))

    def test_solo_software_no_pasa_por_almacen(self):
        self.param.set_param("hubspot_invoice_bridge.document_mode", "auto")
        self.assertFalse(self.Evento._needs_warehouse(self._lineas(self.servicio)))

    def test_basta_una_linea_de_hardware(self):
        self.param.set_param("hubspot_invoice_bridge.document_mode", "auto")
        self.assertTrue(self.Evento._needs_warehouse(
            self._lineas(self.servicio, self.hardware)))

    def test_el_ajuste_puede_forzar_siempre_pedido(self):
        self.param.set_param("hubspot_invoice_bridge.document_mode", "sale_order")
        self.assertTrue(self.Evento._needs_warehouse(self._lineas(self.servicio)))

    def test_el_ajuste_puede_forzar_siempre_factura(self):
        self.param.set_param("hubspot_invoice_bridge.document_mode", "invoice")
        self.assertFalse(self.Evento._needs_warehouse(self._lineas(self.hardware)))


@tagged("post_install", "-at_install")
class TestOrigenDelCambio(TransactionCase):
    """Una importación masiva no debe generar mil facturas."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.param = cls.env["ir.config_parameter"].sudo()
        cls.param.set_param(
            "hubspot_invoice_bridge.ignored_change_sources", "IMPORT")

    def _evento(self, origen):
        return self.Evento.new({"change_source": origen})

    def test_una_importacion_se_ignora(self):
        self.assertEqual(self._evento("IMPORT")._change_source_ignored(), "IMPORT")

    def test_no_distingue_mayusculas(self):
        self.assertTrue(self._evento("import")._change_source_ignored())

    def test_un_cambio_a_mano_si_factura(self):
        self.assertFalse(self._evento("CRM_UI")._change_source_ignored())

    def test_el_barrido_si_factura(self):
        """El barrido es nuestro, no una importación de HubSpot."""
        self.assertFalse(self._evento("BARRIDO")._change_source_ignored())

    def test_sin_origen_si_factura(self):
        self.assertFalse(self._evento(False)._change_source_ignored())

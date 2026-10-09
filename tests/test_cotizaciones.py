"""Lo que el módulo lee del negocio: cotizaciones, descuentos y fechas.

Los casos del análisis de cotizaciones no son inventados: salen del inventario
que hice sobre los 312 negocios del portal real. De ahí vienen las comas
sueltas, el « y », el salto de línea, el «Si» y el número de una factura de
Odoo metido donde va el de una cotización. Si alguno dejara de tratarse bien,
lo que vuelve es un presupuesto con los productos de otro cliente o un negocio
parado sin motivo.
"""

from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

CLIENTE = "odoo.addons.hubspot_invoice_bridge.models.hubspot_client.HubspotClient"


@tagged("post_install", "-at_install")
class TestCotizacionesAprobadas(TransactionCase):
    """De un campo de texto libre a cotizaciones concretas del negocio."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.env["ir.config_parameter"].sudo().set_param(
            "hubspot_invoice_bridge.deal_flag_property", "cotizaciones_aprobadas")

    def _analizar(self, texto, asociadas=None, cotizaciones=None):
        """Corre el análisis con unas cotizaciones asociadas de mentira."""
        asociadas = asociadas if asociadas is not None else [("Q1", True)]
        cotizaciones = cotizaciones if cotizaciones is not None else [
            {"id": "Q1", "properties": {"hs_quote_number": "20260720-110220277"}},
            {"id": "Q2", "properties": {"hs_quote_number": "20260717-155623401"}},
        ]
        with patch(CLIENTE + ".get_associated_ids", return_value=asociadas), \
             patch(CLIENTE + ".batch_read", return_value=cotizaciones):
            return self.Evento._approved_quotes(
                "DEAL1", {"cotizaciones_aprobadas": texto})

    def test_un_numero_limpio(self):
        ids, perdidas, declara = self._analizar(
            "20260720-110220277",
            asociadas=[("Q1", True), ("Q2", False)])
        self.assertEqual(ids, ["Q1"])
        self.assertFalse(perdidas)
        self.assertTrue(declara)

    def test_varios_separados_por_coma(self):
        ids, perdidas, _ = self._analizar(
            "20260720-110220277,20260717-155623401",
            asociadas=[("Q1", True), ("Q2", False)])
        self.assertEqual(sorted(ids), ["Q1", "Q2"])
        self.assertFalse(perdidas)

    def test_separados_por_la_palabra_y(self):
        """Hay quien escribe «A y B» en vez de «A, B»."""
        ids, _, _ = self._analizar(
            "20260720-110220277 y 20260717-155623401",
            asociadas=[("Q1", True), ("Q2", False)])
        self.assertEqual(sorted(ids), ["Q1", "Q2"])

    def test_separados_por_salto_de_linea(self):
        ids, _, _ = self._analizar(
            "20260720-110220277\n20260717-155623401",
            asociadas=[("Q1", True), ("Q2", False)])
        self.assertEqual(sorted(ids), ["Q1", "Q2"])

    def test_con_texto_alrededor(self):
        """«N.° 20260720-110220277» y «Referencia: …» aparecen en el portal."""
        for texto in ("N.° 20260720-110220277",
                      "Referencia: 20260720-110220277",
                      "  20260720-110220277  "):
            with self.subTest(texto=texto):
                ids, _, _ = self._analizar(texto)
                self.assertEqual(ids, ["Q1"])

    def test_la_coma_final_suelta_no_estorba(self):
        ids, perdidas, _ = self._analizar("20260720-110220277, ")
        self.assertEqual(ids, ["Q1"])
        self.assertFalse(perdidas)

    def test_el_mismo_numero_repetido_cuenta_una_vez(self):
        """En el portal hay negocios que repiten la cotización dos veces."""
        ids, _, _ = self._analizar(
            "20250513-183519817,20250513-183519817",
            asociadas=[("Q9", True)],
            cotizaciones=[{"id": "Q9", "properties": {
                "hs_quote_number": "20250513-183519817"}}])
        self.assertEqual(ids, ["Q9"])

    def test_campo_vacio_no_declara_nada(self):
        ids, perdidas, declara = self._analizar("   ")
        self.assertEqual((ids, perdidas, declara), ([], [], False))

    def test_un_si_declara_pero_no_nombra(self):
        """«Si» significa que alguien aprobó, pero no dice cuál.

        No se inventa nada: se avisa de que declara algo y el proceso cae al
        negocio en vez de adivinar una cotización.
        """
        ids, perdidas, declara = self._analizar("Si")
        self.assertEqual(ids, [])
        self.assertEqual(perdidas, [])
        self.assertTrue(declara)

    def test_un_numero_de_factura_no_es_una_cotizacion(self):
        """«FV1246» no tiene la forma de un número de cotización."""
        ids, perdidas, declara = self._analizar("FV1246")
        self.assertEqual(ids, [])
        self.assertTrue(declara)

    def test_una_cotizacion_de_otro_negocio_se_denuncia(self):
        """Lo más peligroso: traer productos de otro cliente.

        El número existe, pero no es de este negocio. Se devuelve como perdida
        para que el proceso pare, en vez de facturar lo que no es.
        """
        ids, perdidas, _ = self._analizar("20260101-999999999")
        self.assertEqual(ids, [])
        self.assertEqual(perdidas, ["20260101-999999999"])

    def test_sin_cotizaciones_asociadas_todo_son_perdidas(self):
        ids, perdidas, declara = self._analizar(
            "20260720-110220277", asociadas=[])
        self.assertEqual(ids, [])
        self.assertEqual(perdidas, ["20260720-110220277"])
        self.assertTrue(declara)


@tagged("post_install", "-at_install")
class TestDescuentos(TransactionCase):
    """HubSpot trae el descuento de dos formas y una es ambigua."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.param = cls.env["ir.config_parameter"].sudo()

    def test_el_porcentaje_se_usa_tal_cual(self):
        self.assertEqual(
            self.Evento._resolve_discount(
                {"hs_discount_percentage": "25"}, 10, 100), 25.0)

    def test_el_porcentaje_manda_sobre_el_importe(self):
        """Si vienen los dos, el que no admite interpretación gana."""
        self.assertEqual(
            self.Evento._resolve_discount(
                {"hs_discount_percentage": "10", "discount": "500"}, 10, 100),
            10.0)

    def test_el_importe_se_convierte_a_porcentaje(self):
        """200 sobre una línea de 1.000 son un 20%."""
        self.param.set_param(
            "hubspot_invoice_bridge.discount_is_percentage", "False")
        self.assertEqual(
            self.Evento._resolve_discount({"discount": "200"}, 10, 100), 20.0)

    def test_el_importe_puede_leerse_como_porcentaje(self):
        """El inventario del portal documenta 'discount' como porcentaje.

        Como la semántica estándar de HubSpot dice lo contrario, se decide por
        ajuste en vez de asumirlo.
        """
        self.param.set_param(
            "hubspot_invoice_bridge.discount_is_percentage", "True")
        self.assertEqual(
            self.Evento._resolve_discount({"discount": "20"}, 10, 100), 20.0)

    def test_sin_descuento_es_cero(self):
        for props in ({}, {"discount": ""}, {"discount": None},
                      {"discount": "0"}):
            with self.subTest(props=props):
                self.assertEqual(
                    self.Evento._resolve_discount(props, 10, 100), 0.0)

    def test_una_linea_a_cero_no_divide_entre_cero(self):
        self.param.set_param(
            "hubspot_invoice_bridge.discount_is_percentage", "False")
        self.assertEqual(
            self.Evento._resolve_discount({"discount": "50"}, 0, 100), 0.0)

    def test_el_descuento_nunca_se_sale_de_rango(self):
        """Un descuento negativo o de más del 100% no tiene sentido."""
        self.param.set_param(
            "hubspot_invoice_bridge.discount_is_percentage", "True")
        self.assertEqual(
            self.Evento._resolve_discount({"discount": "500"}, 1, 100), 100.0)
        self.assertEqual(
            self.Evento._resolve_discount({"discount": "-30"}, 1, 100), 0.0)

    def test_un_texto_que_no_es_numero_no_rompe_nada(self):
        self.assertEqual(
            self.Evento._resolve_discount({"discount": "no aplica"}, 10, 100),
            0.0)


@tagged("post_install", "-at_install")
class TestFechas(TransactionCase):
    """Las fechas de HubSpot vienen en ISO con Z."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]

    def test_iso_con_z_se_convierte_a_utc_sin_zona(self):
        leida = self.Evento._hubspot_datetime("2026-10-06T18:05:31.123Z")
        self.assertEqual(str(leida), "2026-10-06 18:05:31")

    def test_una_fecha_ilegible_no_rompe_el_barrido(self):
        self.assertIsNone(self.Evento._hubspot_datetime("ayer por la tarde"))
        self.assertIsNone(self.Evento._hubspot_datetime(None))
        self.assertIsNone(self.Evento._hubspot_datetime(""))

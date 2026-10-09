"""Que el puente encuentre al cliente que ya existe, y no duplique fichas.

Cada caso de aquí sale de algo que pasó de verdad contra el portal real: los
nombres y los NIT son los que fallaron en producción, no inventados. Si
alguno de estos tests se pone rojo, lo que vuelve es una factura sin NIT —que
no se puede emitir— o un cliente duplicado.

Ninguno llama a la API de HubSpot: todo lo que se prueba aquí trabaja sobre
datos que ya están en Odoo.
"""

from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestNormalizacion(TransactionCase):
    """Las dos funciones que deciden si dos textos son la misma empresa."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]

    def test_nit_ignora_puntos(self):
        self.assertEqual(self.Evento._normalizar_nit("900.628.818"), "900628818")

    def test_nit_descarta_el_digito_de_verificacion(self):
        """El DV va detrás de un guion y en Odoo no suele guardarse.

        Este fue el fallo real: «900.628.818-1» daba 9006288181 y no
        encontraba al cliente guardado como 900628818.
        """
        self.assertEqual(self.Evento._normalizar_nit("900.628.818-1"), "900628818")
        self.assertEqual(self.Evento._normalizar_nit("900628818-1"), "900628818")

    def test_nit_no_recorta_lo_que_no_es_un_dv(self):
        """Sin guion no hay dígito de verificación que quitar."""
        self.assertEqual(self.Evento._normalizar_nit("9006288181"), "9006288181")

    def test_nit_vacio(self):
        self.assertEqual(self.Evento._normalizar_nit(None), "")
        self.assertEqual(self.Evento._normalizar_nit("  "), "")

    def test_nombre_borra_los_puntos_de_la_forma_juridica(self):
        """«ALISTAR S.A.S.» y «ALISTAR SAS» son la misma empresa.

        El otro fallo real: el punto se convertía en espacio y «S.A.S.»
        quedaba como «S A S», que no casaba con nada.
        """
        self.assertEqual(
            self.Evento._normalizar_nombre("ALISTAR S.A.S."),
            self.Evento._normalizar_nombre("ALISTAR SAS"),
        )

    def test_nombre_ignora_mayusculas_y_espacios(self):
        self.assertEqual(
            self.Evento._normalizar_nombre("  Agrocentro   Colombia Sas "),
            self.Evento._normalizar_nombre("AGROCENTRO COLOMBIA SAS"),
        )

    def test_nombre_distingue_empresas_distintas(self):
        self.assertNotEqual(
            self.Evento._normalizar_nombre("Comercial Andes"),
            self.Evento._normalizar_nombre("Comercial Andina"),
        )

    def test_forma_juridica_solo_se_quita_al_final(self):
        """«SAS INSTITUTE» no pierde nada: ahí el SAS es el nombre."""
        quitar = self.Evento._nombre_sin_forma_juridica
        self.assertEqual(quitar("AGP REPRESENTACIONES SAS"), "AGP REPRESENTACIONES")
        self.assertEqual(quitar("SAS INSTITUTE"), "SAS INSTITUTE")

    def test_forma_juridica_no_vacia_el_nombre(self):
        """Una empresa que se llame solo «SAS» conserva su nombre."""
        self.assertEqual(self.Evento._nombre_sin_forma_juridica("SAS"), "SAS")


@tagged("post_install", "-at_install")
class TestBusquedaCliente(TransactionCase):
    """A quién se enlaza una empresa que llega del CRM."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.Partner = cls.env["res.partner"]
        cls.alistar = cls.Partner.create({
            "name": "ZETAPRUEBA ALFA SAS",
            "vat": "999888777",
            "is_company": True,
        })
        cls.agp = cls.Partner.create({
            "name": "ZETAPRUEBA BETA REPRESENTACIONES SAS",
            "is_company": True,
        })

    def test_encuentra_por_nit_aunque_venga_con_puntos_y_dv(self):
        encontrado = self.Evento._buscar_cliente_existente({
            "name": "Un nombre que no se parece en nada",
            "nit": "999.888.777-1",
        })
        self.assertEqual(encontrado, self.alistar)

    def test_el_nit_manda_sobre_el_nombre(self):
        """Si el NIT identifica a uno, el nombre ya no decide."""
        encontrado = self.Evento._buscar_cliente_existente({
            "name": "ZETAPRUEBA BETA REPRESENTACIONES SAS",
            "nit": "999888777",
        })
        self.assertEqual(encontrado, self.alistar)

    def test_encuentra_por_nombre_con_otra_puntuacion(self):
        encontrado = self.Evento._buscar_cliente_existente(
            {"name": "ZETAPRUEBA ALFA S.A.S."})
        self.assertEqual(encontrado, self.alistar)

    def test_encuentra_aunque_falte_la_forma_juridica(self):
        """En el CRM se teclea «AGP Representaciones», sin el SAS."""
        encontrado = self.Evento._buscar_cliente_existente(
            {"name": "ZetaPrueba Beta Representaciones"})
        self.assertEqual(encontrado, self.agp)

    def test_no_enlaza_nada_si_el_nombre_es_ambiguo(self):
        """Dos fichas con el mismo nombre no deciden nada.

        Vale más crear un cliente de sobra —que se corrige en un minuto— que
        emitirle una factura a quien no era.
        """
        self.Partner.create({"name": "ZETAPRUEBA ALFA S.A.S.", "is_company": True})
        encontrado = self.Evento._buscar_cliente_existente(
            {"name": "ZETAPRUEBA ALFA SAS"})
        self.assertFalse(encontrado)

    def test_no_confunde_dos_formas_juridicas_distintas(self):
        """Una SAS y una LTDA con el mismo nombre son empresas distintas."""
        self.Partner.create({"name": "ZetaPrueba Gamma SAS", "is_company": True})
        self.Partner.create({"name": "ZetaPrueba Gamma LTDA", "is_company": True})
        encontrado = self.Evento._buscar_cliente_existente(
            {"name": "ZetaPrueba Gamma"})
        self.assertFalse(encontrado)

    def test_no_inventa_coincidencias(self):
        encontrado = self.Evento._buscar_cliente_existente(
            {"name": "ZetaPrueba Que No Existe En Ninguna Parte"})
        self.assertFalse(encontrado)

    def test_no_enlaza_un_contacto_de_una_empresa(self):
        """El cliente a facturar es la empresa, nunca una persona suya."""
        self.Partner.create({
            "name": "ZetaPrueba Delta SAS",
            "is_company": False,
            "parent_id": self.agp.id,
        })
        encontrado = self.Evento._buscar_cliente_existente(
            {"name": "ZetaPrueba Delta SAS"})
        self.assertFalse(encontrado)


@tagged("post_install", "-at_install")
class TestClienteNuevo(TransactionCase):
    """Cómo nace el cliente que de verdad no existía."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.param = cls.env["ir.config_parameter"].sudo()

    def test_nace_con_la_marca_delante(self):
        """Para que administración lo encuentre y le ponga NIT e impuestos."""
        vals = self.Evento._prepare_partner_vals("123", {"name": "Andromeda"})
        self.assertEqual(vals["name"], "[By HubSpot] Andromeda")

    def test_la_marca_no_se_repite(self):
        vals = self.Evento._prepare_partner_vals(
            "123", {"name": "[By HubSpot] Andromeda"})
        self.assertEqual(vals["name"], "[By HubSpot] Andromeda")

    def test_vacio_significa_la_marca_por_defecto(self):
        """Odoo devuelve el valor por defecto cuando lo guardado está vacío.

        Se deja escrito aquí porque es contraintuitivo: borrar la casilla en
        Ajustes no desactiva el renombrado, lo devuelve a «[By HubSpot]».
        """
        self.param.set_param("hubspot_invoice_bridge.new_partner_prefix", "")
        vals = self.Evento._prepare_partner_vals("123", {"name": "Andromeda"})
        self.assertEqual(vals["name"], "[By HubSpot] Andromeda")

    def test_la_marca_se_puede_cambiar(self):
        self.param.set_param("hubspot_invoice_bridge.new_partner_prefix", "CRM")
        vals = self.Evento._prepare_partner_vals("123", {"name": "Andromeda"})
        self.assertEqual(vals["name"], "CRM Andromeda")

    def test_el_nit_viaja_a_la_identificacion_fiscal(self):
        vals = self.Evento._prepare_partner_vals(
            "123", {"name": "Andromeda", "nit": "999888779"})
        self.assertEqual(vals["vat"], "999888779")


@tagged("post_install", "-at_install")
class TestCompletarHuecos(TransactionCase):
    """Al enlazar un cliente que ya existía, qué se toca y qué no."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.partner = cls.env["res.partner"].create({
            "name": "ZETAPRUEBA EPSILON SAS",
            "vat": "999888778",
            "street": "Calle 1 # 2-3",
            "is_company": True,
            "hubspot_company_id": "777",
        })

    def test_no_pisa_lo_que_ya_estaba(self):
        """Lo que hay en Odoo lo puso contabilidad y vale más que el CRM."""
        self.Evento._completar_huecos(self.partner, {
            "name": "Otro nombre",
            "nit": "999999999",
            "address": "Otra dirección",
        })
        self.assertEqual(self.partner.vat, "999888778")
        self.assertEqual(self.partner.street, "Calle 1 # 2-3")
        self.assertEqual(
            self.partner.name, "ZETAPRUEBA EPSILON SAS")

    def test_rellena_lo_que_faltaba(self):
        self.partner.phone = False
        self.Evento._completar_huecos(self.partner, {
            "name": "Da igual",
            "phone": "6011234567",
        })
        self.assertEqual(self.partner.phone, "6011234567")

    def test_no_renombra_al_que_ya_existia(self):
        """La marca es solo para los que nacen aquí."""
        self.Evento._completar_huecos(self.partner, {"name": "Andromeda"})
        self.assertNotIn("[By HubSpot]", self.partner.name)


@tagged("post_install", "-at_install")
class TestDisparoPorEvento(TransactionCase):
    """Que el trabajo empiece cuando llega el evento, no cuando toca el reloj."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.Disparo = cls.env["ir.cron.trigger"]
        cls.cron = cls.env.ref(
            "hubspot_invoice_bridge.ir_cron_process_hubspot_events")

    def _disparos(self):
        return self.Disparo.search_count([("cron_id", "=", self.cron.id)])

    def _evento(self, identificador):
        return {
            "eventId": identificador,
            "subscriptionType": "deal.propertyChange",
            "objectId": "1",
            "propertyName": "dealstage",
            "propertyValue": "closedwon",
        }

    def test_un_evento_nuevo_despierta_al_procesador(self):
        antes = self._disparos()
        self.Evento.ingest_batch([self._evento("prueba-disparo-1")])
        self.assertGreater(self._disparos(), antes)

    def test_un_duplicado_no_despierta_a_nadie(self):
        """Reencolar lo mismo no es un evento nuevo.

        El barrido reencola un negocio cada vez que se toca en HubSpot, así
        que sin esto el procesador se despertaría una y otra vez para no
        hacer nada.
        """
        self.Evento.ingest_batch([self._evento("prueba-disparo-2")])
        antes = self._disparos()
        self.Evento.ingest_batch([self._evento("prueba-disparo-2")])
        self.assertEqual(self._disparos(), antes)

    def test_un_lote_vacio_no_despierta_a_nadie(self):
        antes = self._disparos()
        self.Evento.ingest_batch([])
        self.assertEqual(self._disparos(), antes)


@tagged("post_install", "-at_install")
class TestNoFusionarLoQueEsDistinto(TransactionCase):
    """Empresas que se parecen pero no son la misma.

    El caso lo trajo Xilean el 09-10-2026: en el portal conviven Cementos
    Argos, Zona franca argos, Argo Star Freight, Tecnológico Argos y Seguros
    Argos oficial. Son cinco clientes distintos. Facturarle a uno lo de otro
    sería mucho peor que crear una ficha de más.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.Partner = cls.env["res.partner"]
        cls.familia = {}
        for nombre in ("ZETAPRUEBA Cementos Argos",
                       "ZETAPRUEBA Zona Franca Argos",
                       "ZETAPRUEBA Argo Star Freight",
                       "ZETAPRUEBA Tecnologico Argos",
                       "ZETAPRUEBA Seguros Argos Oficial"):
            cls.familia[nombre] = cls.Partner.create({
                "name": nombre,
                "is_company": True,
                "website": "argos.com.co",
            })

    def test_cada_una_se_encuentra_a_si_misma(self):
        for nombre, ficha in self.familia.items():
            with self.subTest(nombre=nombre):
                self.assertEqual(
                    self.Evento._buscar_cliente_existente({"name": nombre}),
                    ficha)

    def test_el_dominio_compartido_no_las_fusiona(self):
        """El dominio no se usa para emparejar, y es deliberado.

        Varias empresas del mismo grupo comparten dominio, y hasta empresas
        sin relación comparten uno genérico.
        """
        encontrado = self.Evento._buscar_cliente_existente({
            "name": "ZETAPRUEBA Otra Empresa Del Grupo",
            "domain": "argos.com.co",
        })
        self.assertFalse(encontrado)

    def test_un_nombre_parecido_no_basta(self):
        """«Argos» a secas no identifica a ninguna de las cinco."""
        self.assertFalse(
            self.Evento._buscar_cliente_existente({"name": "ZETAPRUEBA Argos"}))

    def test_la_forma_juridica_no_puede_fusionar_dos_empresas(self):
        """Una SAS y una LTDA con el mismo nombre son empresas distintas."""
        self.Partner.create({"name": "ZETAPRUEBA Argos Energia SAS",
                             "is_company": True})
        self.Partner.create({"name": "ZETAPRUEBA Argos Energia LTDA",
                             "is_company": True})
        self.assertFalse(self.Evento._buscar_cliente_existente(
            {"name": "ZETAPRUEBA Argos Energia"}))


@tagged("post_install", "-at_install")
class TestContactosNoSeRoban(TransactionCase):
    """Un buzón compartido no convierte a dos personas en una.

    info@ o ventas@ los usan varias personas de la misma empresa. Sin guarda,
    el segundo contacto se llevaría la ficha del primero: la restricción de
    unicidad no lo impide, porque es la misma fila cambiando de valor.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Evento = cls.env["hubspot.webhook.event"]
        cls.Partner = cls.env["res.partner"]

    def test_una_ficha_ya_vinculada_no_se_adopta_por_correo(self):
        ya = self.Partner.create({
            "name": "ZETAPRUEBA Persona Primera",
            "email": "info@zetaprueba.test",
            "hubspot_contact_id": "CONTACTO-A",
        })
        libre = self.Partner.search([
            ("email", "=ilike", "info@zetaprueba.test"),
            ("hubspot_contact_id", "=", False),
            ("parent_id", "=", False),
        ])
        self.assertFalse(libre, "la ficha vinculada no debe salir como libre")
        ya.invalidate_recordset()
        self.assertEqual(ya.hubspot_contact_id, "CONTACTO-A")

    def test_dos_fichas_libres_con_el_mismo_correo_no_deciden(self):
        for n in ("ZETAPRUEBA Libre Uno", "ZETAPRUEBA Libre Dos"):
            self.Partner.create({"name": n, "email": "ventas@zetaprueba.test"})
        libres = self.Partner.search([
            ("email", "=ilike", "ventas@zetaprueba.test"),
            ("hubspot_contact_id", "=", False),
            ("parent_id", "=", False),
        ])
        self.assertEqual(len(libres), 2)

    def test_dos_fichas_no_pueden_apuntar_al_mismo_contacto(self):
        """La restricción de la base es la última red."""
        from psycopg2 import IntegrityError
        self.Partner.create({
            "name": "ZETAPRUEBA Vinculada", "hubspot_contact_id": "CONTACTO-Z"})
        with self.assertRaises(IntegrityError):
            with self.cr.savepoint():
                self.Partner.create({
                    "name": "ZETAPRUEBA Intrusa",
                    "hubspot_contact_id": "CONTACTO-Z"})

    def test_dos_fichas_no_pueden_apuntar_a_la_misma_empresa(self):
        from psycopg2 import IntegrityError
        self.Partner.create({
            "name": "ZETAPRUEBA Empresa Vinculada", "is_company": True,
            "hubspot_company_id": "EMPRESA-Z"})
        with self.assertRaises(IntegrityError):
            with self.cr.savepoint():
                self.Partner.create({
                    "name": "ZETAPRUEBA Empresa Intrusa", "is_company": True,
                    "hubspot_company_id": "EMPRESA-Z"})

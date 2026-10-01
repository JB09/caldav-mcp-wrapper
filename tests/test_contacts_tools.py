"""Tests for server.py's contacts/CardDAV tools and configuration.

Mocks `carddav`'s client-facing functions (already covered against realistic
XML in test_carddav_client.py) so these focus on server.py's own
responsibilities: owner resolution, READ_ONLY gating, UID-based identification,
and wiring tool arguments through to carddav.py correctly.
"""

import importlib
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddav  # noqa: E402

BASE_ENV = {
    "CALDAV_URL": "http://example.test/egroupware/groupdav.php/",
    "CALDAV_USERNAME": "mardjor",
    "CALDAV_PASSWORD": "secret",
}


def _reload_server(extra_env=None):
    """Import (or re-import) server.py with a controlled environment.

    server.py reads its configuration at module import time, so changing
    CARDDAV_CONTACTS_USER between tests requires a fresh import.
    """
    env = dict(BASE_ENV)
    env.update(extra_env or {})
    with mock.patch.dict(os.environ, env, clear=False):
        if "server" in sys.modules:
            return importlib.reload(sys.modules["server"])
        return importlib.import_module("server")


class ContactsUserResolutionTests(unittest.TestCase):
    def test_explicit_contacts_user(self):
        server = _reload_server({"CALDAV_CALENDAR_USER": "joao", "CARDDAV_CONTACTS_USER": "joao"})
        self.assertEqual(server._contacts_user(), "joao")
        self.assertEqual(
            server._egroupware_addressbook_url(),
            "http://example.test/egroupware/groupdav.php/joao/addressbook/",
        )

    def test_empty_contacts_user_falls_back_to_caldav_username(self):
        server = _reload_server({"CARDDAV_CONTACTS_USER": ""})
        self.assertEqual(server._contacts_user(), "mardjor")
        self.assertEqual(
            server._egroupware_addressbook_url(),
            "http://example.test/egroupware/groupdav.php/mardjor/addressbook/",
        )

    def test_contacts_user_independent_of_calendar_user(self):
        # Calendar owner and contacts owner may differ.
        server = _reload_server(
            {"CALDAV_CALENDAR_USER": "ines", "CARDDAV_CONTACTS_USER": "joao"}
        )
        self.assertEqual(server._calendar_user(), "ines")
        self.assertEqual(server._contacts_user(), "joao")


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.server = _reload_server({"CARDDAV_CONTACTS_USER": "joao"})
        self.book_url = self.server._egroupware_addressbook_url()
        # Replace the DAVClient constructor the tools build internally so no
        # network call is ever attempted.
        self._client_patch = mock.patch.object(
            self.server, "_get_carddav_client", return_value=mock.Mock(name="client")
        )
        self._client_patch.start()

    def tearDown(self):
        self._client_patch.stop()

    def _entry(self, uid, fn, href_suffix, etag='"e1"'):
        text = f"BEGIN:VCARD\nVERSION:3.0\nUID:{uid}\nFN:{fn}\nEND:VCARD\n"
        return {"href": f"{self.book_url}{href_suffix}", "etag": etag, "text": text}

    def test_list_contact_books(self):
        books = [{"name": "Contacts", "url": self.book_url, "permissions": ["read", "write"], "components": ["VCARD"]}]
        with mock.patch.object(carddav, "discover_addressbooks", return_value=books):
            result = json.loads(self.server.list_contact_books())
        self.assertEqual(result, books)

    def test_list_contacts_returns_structured_fields(self):
        entries = [self._entry("u1", "Joao Silva", "u1.vcf"), self._entry("u2", "Maria Costa", "u2.vcf")]
        with mock.patch.object(carddav, "fetch_all_vcards", return_value=entries) as fetch:
            result = json.loads(self.server.list_contacts())
        fetch.assert_called_once_with(mock.ANY, self.book_url)
        self.assertEqual(len(result), 2)
        self.assertEqual({c["uid"] for c in result}, {"u1", "u2"})
        self.assertEqual(result[0]["full_name"], "Joao Silva")

    def test_list_contacts_limit_and_offset(self):
        entries = [self._entry(f"u{i}", f"Name {i}", f"u{i}.vcf") for i in range(5)]
        with mock.patch.object(carddav, "fetch_all_vcards", return_value=entries):
            result = json.loads(self.server.list_contacts(limit=2, offset=1))
        self.assertEqual([c["uid"] for c in result], ["u1", "u2"])

    def test_get_contact_found(self):
        entry = self._entry("u1", "Joao Silva", "u1.vcf")
        with mock.patch.object(carddav, "find_vcard_by_uid_server_side", return_value=entry):
            result = json.loads(self.server.get_contact("u1"))
        self.assertEqual(result["uid"], "u1")
        self.assertEqual(result["full_name"], "Joao Silva")

    def test_get_contact_not_found_returns_null(self):
        with mock.patch.object(carddav, "find_vcard_by_uid_server_side", return_value=None), \
             mock.patch.object(carddav, "fetch_all_vcards", return_value=[]):
            result = self.server.get_contact("missing")
        self.assertEqual(json.loads(result), None)

    def test_search_contacts_uses_server_side_first(self):
        entries = [self._entry("u1", "Joao Silva", "u1.vcf")]
        with mock.patch.object(carddav, "search_vcards_server_side", return_value=entries) as search:
            result = json.loads(self.server.search_contacts("joao"))
        search.assert_called_once()
        self.assertEqual(result[0]["uid"], "u1")

    def test_search_contacts_falls_back_client_side_on_server_error(self):
        entries = [self._entry("u1", "Joao Silva", "u1.vcf"), self._entry("u2", "Maria Costa", "u2.vcf")]
        with mock.patch.object(carddav, "search_vcards_server_side", side_effect=RuntimeError("REPORT not supported")), \
             mock.patch.object(carddav, "fetch_all_vcards", return_value=entries):
            result = json.loads(self.server.search_contacts("joao"))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["uid"], "u1")

    def test_create_contact_builds_and_puts_vcard(self):
        with mock.patch.object(carddav, "put_vcard") as put_vcard:
            message = self.server.create_contact(given_name="Pedro", family_name="Alves")
        self.assertIn("created", message.lower())
        put_vcard.assert_called_once()
        args, _kwargs = put_vcard.call_args
        _client, href, vcard_text = args[0], args[1], args[2]
        self.assertTrue(href.startswith(self.book_url))
        self.assertIn("FN:Pedro Alves", vcard_text)

    def test_create_contact_requires_a_name(self):
        with self.assertRaises(ValueError):
            self.server.create_contact()

    def test_update_contact_preserves_unspecified_fields(self):
        entry = self._entry("u1", "Joao Silva", "u1.vcf")
        entry["text"] = (
            "BEGIN:VCARD\nVERSION:3.0\nUID:u1\nFN:Joao Silva\n"
            "EMAIL;TYPE=HOME:joao@example.com\nORG:ACME\nEND:VCARD\n"
        )
        with mock.patch.object(carddav, "find_vcard_by_uid_server_side", return_value=entry), \
             mock.patch.object(carddav, "put_vcard") as put_vcard:
            self.server.update_contact("u1", phones=[{"value": "911111111", "type": "CELL"}])
        put_vcard.assert_called_once()
        args, _kwargs = put_vcard.call_args
        new_text = args[2]
        contact = carddav.vcard_to_contact(new_text)
        self.assertEqual(contact["phones"], [{"type": "CELL", "value": "911111111"}])
        # Untouched fields survive.
        self.assertEqual(contact["organization"], "ACME")
        self.assertEqual(contact["emails"], [{"type": "HOME", "value": "joao@example.com"}])

    def test_delete_contact_uses_uid_not_name(self):
        entry = self._entry("u1", "Joao Silva", "u1.vcf")
        with mock.patch.object(carddav, "find_vcard_by_uid_server_side", return_value=entry), \
             mock.patch.object(carddav, "delete_vcard") as delete_vcard:
            message = self.server.delete_contact("u1")
        delete_vcard.assert_called_once_with(mock.ANY, entry["href"], etag=entry["etag"])
        self.assertIn("u1", message)

    def test_list_birthdays_computes_age_and_window(self):
        from datetime import date, timedelta

        today = date.today()
        in_window = today + timedelta(days=5)
        entry_with_year = self._entry("u1", "Joao Silva", "u1.vcf")
        entry_with_year["text"] = (
            f"BEGIN:VCARD\nVERSION:3.0\nUID:u1\nFN:Joao Silva\n"
            f"BDAY:1990-{in_window.month:02d}-{in_window.day:02d}\nEND:VCARD\n"
        )
        entry_no_year = self._entry("u2", "Maria Costa", "u2.vcf")
        entry_no_year["text"] = (
            f"BEGIN:VCARD\nVERSION:3.0\nUID:u2\nFN:Maria Costa\n"
            f"BDAY:--{in_window.month:02d}-{in_window.day:02d}\nEND:VCARD\n"
        )
        entry_out_of_window = self._entry("u3", "Out Of Window", "u3.vcf")
        far = today + timedelta(days=200)
        entry_out_of_window["text"] = (
            f"BEGIN:VCARD\nVERSION:3.0\nUID:u3\nFN:Out Of Window\n"
            f"BDAY:1990-{far.month:02d}-{far.day:02d}\nEND:VCARD\n"
        )
        with mock.patch.object(
            carddav,
            "fetch_all_vcards",
            return_value=[entry_with_year, entry_no_year, entry_out_of_window],
        ):
            result = json.loads(self.server.list_birthdays())
        uids = {r["uid"] for r in result}
        self.assertEqual(uids, {"u1", "u2"})
        by_uid = {r["uid"]: r for r in result}
        self.assertEqual(by_uid["u1"]["age"], in_window.year - 1990)
        self.assertIsNone(by_uid["u2"]["age"])


class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.server = _reload_server({"READ_ONLY": "true", "CARDDAV_CONTACTS_USER": "joao"})
        self._client_patch = mock.patch.object(
            self.server, "_get_carddav_client", return_value=mock.Mock(name="client")
        )
        self._client_patch.start()

    def tearDown(self):
        self._client_patch.stop()

    def test_create_contact_blocked(self):
        with self.assertRaises(RuntimeError):
            self.server.create_contact(given_name="Pedro")

    def test_update_contact_blocked(self):
        with self.assertRaises(RuntimeError):
            self.server.update_contact("u1", notes="x")

    def test_delete_contact_blocked(self):
        with self.assertRaises(RuntimeError):
            self.server.delete_contact("u1")

    def test_read_tools_still_work(self):
        with mock.patch.object(carddav, "fetch_all_vcards", return_value=[]):
            self.assertEqual(json.loads(self.server.list_contacts()), [])
            self.assertEqual(json.loads(self.server.list_birthdays()), [])


if __name__ == "__main__":
    unittest.main()

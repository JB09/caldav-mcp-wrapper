"""Unit tests for carddav.py: vCard mapping, read-modify-write, birthdays.

No network and no CardDAV server involved — these exercise the pure
parsing/generation/matching logic directly.
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carddav  # noqa: E402

SAMPLE_VCARD = """BEGIN:VCARD
VERSION:3.0
UID:contact-1
FN:Joao Silva
N:Silva;Joao;;;
ORG:ACME Corp
TITLE:Engineer
EMAIL;TYPE=HOME:joao@example.com
EMAIL;TYPE=WORK:joao.silva@acme.example
TEL;TYPE=CELL:912345678
ADR;TYPE=HOME:;;Rua X 1;Lisboa;;1000-000;Portugal
BDAY:1990-10-04
CATEGORIES:Family,Friends
NOTE:Met at a conference.
URL:https://example.com/joao
X-EGROUPWARE-CUSTOM:custom-value
END:VCARD
"""


class VCardToContactTests(unittest.TestCase):
    def test_full_mapping(self):
        contact = carddav.vcard_to_contact(SAMPLE_VCARD, href="/x/joao.vcf", etag='"abc"')
        self.assertEqual(contact["uid"], "contact-1")
        self.assertEqual(contact["full_name"], "Joao Silva")
        self.assertEqual(contact["given_name"], "Joao")
        self.assertEqual(contact["family_name"], "Silva")
        self.assertEqual(contact["organization"], "ACME Corp")
        self.assertEqual(contact["title"], "Engineer")
        self.assertEqual(len(contact["emails"]), 2)
        self.assertIn({"type": "HOME", "value": "joao@example.com"}, contact["emails"])
        self.assertEqual(contact["phones"], [{"type": "CELL", "value": "912345678"}])
        self.assertEqual(len(contact["addresses"]), 1)
        self.assertEqual(contact["addresses"][0]["city"], "Lisboa")
        self.assertEqual(contact["birthday"], "1990-10-04")
        self.assertEqual(set(contact["categories"]), {"Family", "Friends"})
        self.assertEqual(contact["notes"], "Met at a conference.")
        self.assertEqual(contact["url"], "https://example.com/joao")
        self.assertEqual(contact["custom_fields"]["X-EGROUPWARE-CUSTOM"], "custom-value")
        self.assertEqual(contact["href"], "/x/joao.vcf")
        self.assertEqual(contact["etag"], '"abc"')

    def test_does_not_invent_missing_fields(self):
        minimal = "BEGIN:VCARD\nVERSION:3.0\nUID:u1\nFN:Just A Name\nEND:VCARD\n"
        contact = carddav.vcard_to_contact(minimal)
        self.assertIsNone(contact["birthday"])
        self.assertIsNone(contact["organization"])
        self.assertEqual(contact["emails"], [])
        self.assertEqual(contact["categories"], [])


class BuildVCardTests(unittest.TestCase):
    def test_requires_a_name(self):
        with self.assertRaises(ValueError):
            carddav.build_vcard({})

    def test_builds_from_given_family(self):
        text = carddav.build_vcard({"given_name": "Pedro", "family_name": "Alves"})
        contact = carddav.vcard_to_contact(text)
        self.assertEqual(contact["full_name"], "Pedro Alves")
        self.assertEqual(contact["given_name"], "Pedro")
        self.assertEqual(contact["family_name"], "Alves")
        self.assertTrue(contact["uid"])  # a UID was generated

    def test_builds_all_fields(self):
        text = carddav.build_vcard(
            {
                "uid": "fixed-uid",
                "full_name": "Maria Costa",
                "organization": "Acme",
                "title": "CEO",
                "emails": [{"value": "maria@example.com", "type": "WORK"}],
                "phones": [{"value": "911111111", "type": "CELL"}],
                "addresses": [{"type": "HOME", "city": "Porto"}],
                "birthday": "1985-10-18",
                "notes": "Prefers email.",
                "url": "https://example.com/maria",
                "categories": ["VIP"],
            }
        )
        contact = carddav.vcard_to_contact(text)
        self.assertEqual(contact["uid"], "fixed-uid")
        self.assertEqual(contact["emails"], [{"type": "WORK", "value": "maria@example.com"}])
        self.assertEqual(contact["birthday"], "1985-10-18")
        self.assertEqual(contact["categories"], ["VIP"])

    def test_builds_and_updates_additional_vcard_fields(self):
        text = carddav.build_vcard({
            "full_name": "Maria Costa",
            "name_components": {"additional": "de", "prefix": "Dr."},
            "nickname": "Mimi",
            "role": "Director",
            "kind": "individual",
            "anniversary": "2001-05-12",
            "impp": [{"value": "xmpp:maria@example.com", "type": "HOME"}],
            "custom_fields": {"X-ORIGIN": "CRM"},
        })
        contact = carddav.vcard_to_contact(text)
        self.assertEqual(contact["name_components"]["additional"], "de")
        self.assertEqual(contact["name_components"]["prefix"], "Dr.")
        self.assertEqual(contact["nickname"], "Mimi")
        self.assertEqual(contact["role"], "Director")
        self.assertEqual(contact["anniversary"], "2001-05-12")
        self.assertEqual(contact["impp"][0]["value"], "xmpp:maria@example.com")
        self.assertEqual(contact["custom_fields"]["X-ORIGIN"], "CRM")

        updated = carddav.apply_updates(text, {"role": "CEO"})
        result = carddav.vcard_to_contact(updated)
        self.assertEqual(result["role"], "CEO")
        self.assertEqual(result["nickname"], "Mimi")
        self.assertEqual(result["impp"], contact["impp"])
        self.assertEqual(result["custom_fields"], contact["custom_fields"])


class ApplyUpdatesTests(unittest.TestCase):
    def test_update_phone_preserves_everything_else(self):
        updated_text = carddav.apply_updates(
            SAMPLE_VCARD, {"phones": [{"value": "999999999", "type": "CELL"}]}
        )
        contact = carddav.vcard_to_contact(updated_text)
        self.assertEqual(contact["phones"], [{"type": "CELL", "value": "999999999"}])
        # Untouched fields survive exactly.
        self.assertEqual(contact["organization"], "ACME Corp")
        self.assertEqual(len(contact["emails"]), 2)
        self.assertEqual(contact["birthday"], "1990-10-04")
        self.assertEqual(contact["notes"], "Met at a conference.")
        self.assertEqual(len(contact["addresses"]), 1)
        self.assertEqual(set(contact["categories"]), {"Family", "Friends"})
        self.assertEqual(contact["custom_fields"]["X-EGROUPWARE-CUSTOM"], "custom-value")
        self.assertEqual(contact["organization"], "ACME Corp")

    def test_update_email_does_not_touch_phone_or_address(self):
        updated_text = carddav.apply_updates(
            SAMPLE_VCARD, {"emails": [{"value": "new@example.com", "type": "HOME"}]}
        )
        contact = carddav.vcard_to_contact(updated_text)
        self.assertEqual(contact["emails"], [{"type": "HOME", "value": "new@example.com"}])
        self.assertEqual(contact["phones"], [{"type": "CELL", "value": "912345678"}])
        self.assertEqual(len(contact["addresses"]), 1)

    def test_no_fields_is_a_no_op(self):
        updated_text = carddav.apply_updates(SAMPLE_VCARD, {k: None for k in (
            "full_name", "given_name", "family_name", "organization", "title",
            "emails", "phones", "addresses", "birthday", "notes", "url", "categories",
        )})
        before = carddav.vcard_to_contact(SAMPLE_VCARD)
        after = carddav.vcard_to_contact(updated_text)
        self.assertEqual(before, after)

    def test_update_custom_property_preserves_unrequested_properties_and_params(self):
        text = SAMPLE_VCARD.replace(
            "TEL;TYPE=CELL:912345678", "TEL;TYPE=CELL;PREF=1:912345678"
        ).replace("X-EGROUPWARE-CUSTOM:custom-value", "X-EGROUPWARE-CUSTOM:old")
        updated = carddav.apply_updates(text, {"custom_fields": {"X-EGROUPWARE-CUSTOM": "new"}})
        contact = carddav.vcard_to_contact(updated)
        self.assertEqual(contact["custom_fields"]["X-EGROUPWARE-CUSTOM"], "new")
        self.assertEqual(contact["phones"][0]["params"]["pref"], ["1"])
        self.assertEqual(contact["emails"][0]["value"], "joao@example.com")
        self.assertEqual(contact["categories"], ["Family", "Friends"])

    def test_custom_property_can_be_added_and_removed(self):
        updated = carddav.apply_updates(SAMPLE_VCARD, {"custom_fields": {"X-NEW": "value"}})
        self.assertEqual(carddav.vcard_to_contact(updated)["custom_fields"]["X-NEW"], "value")
        cleared = carddav.apply_updates(updated, {"custom_fields": {"X-NEW": None}})
        self.assertNotIn("X-NEW", carddav.vcard_to_contact(cleared)["custom_fields"])

    def test_scalar_field_update_keeps_vcard_parameters(self):
        text = SAMPLE_VCARD.replace("BDAY:1990-10-04", "BDAY;VALUE=date:1990-10-04")
        updated = carddav.apply_updates(text, {"birthday": "1991-10-04"})
        parsed = __import__("vobject").readOne(updated)
        self.assertEqual(parsed.bday.params["VALUE"], ["date"])


class SearchMatchTests(unittest.TestCase):
    def setUp(self):
        self.contact = carddav.vcard_to_contact(SAMPLE_VCARD)

    def test_matches_name_substring_case_insensitive(self):
        self.assertTrue(carddav.matches_query(self.contact, "joao"))
        self.assertTrue(carddav.matches_query(self.contact, "SILVA"))

    def test_matches_email_and_org(self):
        self.assertTrue(carddav.matches_query(self.contact, "acme"))
        self.assertTrue(carddav.matches_query(self.contact, "joao.silva@acme"))

    def test_no_match(self):
        self.assertFalse(carddav.matches_query(self.contact, "does-not-exist"))

    def test_matches_categories(self):
        self.assertTrue(carddav.matches_query(self.contact, "Family"))


class BirthdayTests(unittest.TestCase):
    def test_parse_full_date(self):
        self.assertEqual(carddav.parse_birthday("1990-10-04"), (10, 4, 1990))

    def test_parse_partial_date(self):
        self.assertEqual(carddav.parse_birthday("--10-04"), (10, 4, None))
        self.assertEqual(carddav.parse_birthday("--1004"), (10, 4, None))

    def test_parse_placeholder_year(self):
        self.assertEqual(carddav.parse_birthday("1604-10-04"), (10, 4, None))
        self.assertEqual(carddav.parse_birthday("1000-10-04"), (10, 4, None))
        self.assertEqual(carddav.parse_birthday("0000-10-04"), (10, 4, None))

    def test_parse_invalid_raises(self):
        with self.assertRaises(ValueError):
            carddav.parse_birthday("not-a-date")

    def test_next_occurrence_this_year(self):
        self.assertEqual(carddav.next_occurrence(10, 4, date(2026, 9, 1)), date(2026, 10, 4))

    def test_next_occurrence_wraps_to_next_year(self):
        self.assertEqual(carddav.next_occurrence(1, 15, date(2026, 10, 1)), date(2027, 1, 15))

    def test_next_occurrence_leap_day_in_non_leap_year(self):
        self.assertEqual(carddav.next_occurrence(2, 29, date(2027, 1, 1)), date(2027, 2, 28))


if __name__ == "__main__":
    unittest.main()

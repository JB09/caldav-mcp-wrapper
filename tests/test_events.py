"""Unit tests for complete iCalendar event reads and read-modify-write tools."""

import importlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icalendar import Calendar, Event


class FakeEvent:
    def __init__(self, text):
        self.data = text.encode() if isinstance(text, str) else text
        self.saved = 0

    @property
    def icalendar_instance(self):
        return Calendar.from_ical(self.data)

    @icalendar_instance.setter
    def icalendar_instance(self, value):
        self.data = value.to_ical()

    def save(self):
        self.saved += 1


class FakeCalendar:
    def __init__(self):
        self.saved_data = None

    def save_event(self, text):
        self.saved_data = text


def _server(read_only=False):
    env = {
        "CALDAV_URL": "http://example.test/groupdav.php/",
        "CALDAV_USERNAME": "mardjor",
        "CALDAV_PASSWORD": "secret",
        "READ_ONLY": str(read_only).lower(),
    }
    with mock.patch.dict(os.environ, env, clear=False):
        if "server" in sys.modules:
            return importlib.reload(sys.modules["server"])
        return importlib.import_module("server")


def _event_text():
    return """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Tests//EN
BEGIN:VEVENT
UID:event-1
DTSTART:20261002T100000Z
DTEND:20261002T110000Z
SUMMARY:Original
RRULE:FREQ=WEEKLY;COUNT=5
CATEGORIES:Work,Planning
ATTENDEE;CN=Aida;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:aida@example.com
ATTENDEE;CN=Bob:mailto:bob@example.com
ORGANIZER;CN=Host;SENT-BY="mailto:assistant@example.com":mailto:host@example.com
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Reminder
TRIGGER:-PT15M
END:VALARM
X-EGROUPWARE-PRIVATE:keep-me
END:VEVENT
END:VCALENDAR
"""


class EventToolTests(unittest.TestCase):
    def setUp(self):
        self.server = _server()
        self.calendar = FakeCalendar()

    def test_create_event_writes_attendee_organizer_and_extensions(self):
        with mock.patch.object(self.server, "_resolve_writable", return_value=self.calendar), \
             mock.patch.object(self.server, "_resolve_target", return_value="Calendar"), \
             mock.patch.object(self.server, "_calendar_name", return_value="Calendar"):
            self.server.create_event(
                "Meeting",
                "2026-10-02T10:00:00+00:00",
                "2026-10-02T11:00:00+00:00",
                attendees=[{
                    "email": "aida@example.com",
                    "name": "Aida Maria Ramos Miranda",
                    "role": "REQ-PARTICIPANT",
                    "rsvp": True,
                }],
                organizer={"email": "host@example.com", "name": "Host"},
                categories=["Work", "VIP"],
                custom_fields={"X-EXAMPLE": "value"},
            )
        created = Calendar.from_ical(self.calendar.saved_data)
        event = created.walk("VEVENT")[0]
        attendee = event["attendee"]
        self.assertEqual(str(attendee), "mailto:aida@example.com")
        self.assertEqual(attendee.params["CN"], "Aida Maria Ramos Miranda")
        self.assertEqual(attendee.params["RSVP"], "TRUE")
        self.assertEqual(str(event["organizer"]), "mailto:host@example.com")
        self.assertEqual(event["organizer"].params["CN"], "Host")
        summary = self.server._summarize_component(event)
        self.assertEqual(summary["categories"], ["Work", "VIP"])
        self.assertEqual(summary["custom_fields"]["X-EXAMPLE"], "value")

    def test_update_one_property_preserves_recurrence_attendees_alarm_and_xprop(self):
        event = FakeEvent(_event_text())
        with mock.patch.object(self.server, "_resolve_writable", return_value=self.calendar), \
             mock.patch.object(self.server, "_resolve_target", return_value="Calendar"), \
             mock.patch.object(self.server, "_find_event", return_value=event), \
             mock.patch.object(self.server, "_calendar_name", return_value="Calendar"):
            self.server.update_event("event-1", summary="Updated")
        stored = Calendar.from_ical(event.data).walk("VEVENT")[0]
        self.assertEqual(str(stored["summary"]), "Updated")
        self.assertEqual(str(stored["rrule"]["FREQ"][0]), "WEEKLY")
        self.assertEqual(len(stored["attendee"]), 2)
        self.assertEqual(str(stored["X-EGROUPWARE-PRIVATE"]), "keep-me")
        self.assertEqual(len(stored.subcomponents), 1)
        self.assertEqual(
            stored["organizer"].params["SENT-BY"], "mailto:assistant@example.com"
        )
        self.assertEqual([str(c) for c in stored["categories"].cats], ["Work", "Planning"])

    def test_changing_organizer_keeps_existing_parameters(self):
        event = FakeEvent(_event_text())
        with mock.patch.object(self.server, "_resolve_writable", return_value=self.calendar), \
             mock.patch.object(self.server, "_resolve_target", return_value="Calendar"), \
             mock.patch.object(self.server, "_find_event", return_value=event), \
             mock.patch.object(self.server, "_calendar_name", return_value="Calendar"):
            self.server.update_event(
                "event-1", organizer={"email": "host@example.com", "name": "New Host"}
            )
        organizer = Calendar.from_ical(event.data).walk("VEVENT")[0]["organizer"]
        self.assertEqual(organizer.params["CN"], "New Host")
        self.assertEqual(
            organizer.params["SENT-BY"], "mailto:assistant@example.com"
        )

    def test_attendee_add_update_remove_keeps_other_attendees(self):
        event = FakeEvent(_event_text())
        patches = (
            mock.patch.object(self.server, "_resolve_writable", return_value=self.calendar),
            mock.patch.object(self.server, "_resolve_target", return_value="Calendar"),
            mock.patch.object(self.server, "_find_event", return_value=event),
            mock.patch.object(self.server, "_calendar_name", return_value="Calendar"),
        )
        for patcher in patches:
            patcher.start()
        try:
            self.server.add_event_attendee("event-1", email="new@example.com", name="New")
            self.server.update_event_attendee(
                "event-1", "aida@example.com", partstat="ACCEPTED", rsvp=False, name="Aida M."
            )
            self.server.remove_event_attendee("event-1", "bob@example.com")
        finally:
            for patcher in patches:
                patcher.stop()

        stored = Calendar.from_ical(event.data).walk("VEVENT")[0]
        attendees = {
            str(item).removeprefix("mailto:"): item
            for item in self.server._ical_values(stored, "attendee")
        }
        self.assertEqual(set(attendees), {"aida@example.com", "new@example.com"})
        self.assertEqual(attendees["aida@example.com"].params["PARTSTAT"], "ACCEPTED")
        self.assertEqual(attendees["aida@example.com"].params["RSVP"], "FALSE")
        self.assertEqual(attendees["aida@example.com"].params["CN"], "Aida M.")
        self.assertEqual(str(stored["X-EGROUPWARE-PRIVATE"]), "keep-me")

    def test_contact_name_ambiguity_is_not_resolved_arbitrarily(self):
        with mock.patch.object(self.server, "_get_carddav_client"), \
             mock.patch.object(self.server, "_resolve_addressbook_url", return_value="book"), \
             mock.patch.object(self.server.carddav, "search_vcards_server_side", return_value=[
                 {"text": "BEGIN:VCARD\nVERSION:3.0\nFN:Aida Ramos\nEMAIL:aida1@example.com\nEND:VCARD\n"},
                 {"text": "BEGIN:VCARD\nVERSION:3.0\nFN:Aida Ramos\nEMAIL:aida2@example.com\nEND:VCARD\n"},
             ]):
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                self.server._resolve_attendee_identity(None, "Aida Ramos")

    def test_contact_lookup_supplies_calendar_email_identity(self):
        entry = {
            "text": (
                "BEGIN:VCARD\nVERSION:3.0\nUID:contact-1\nFN:Aida Ramos\n"
                "EMAIL;TYPE=HOME:aida@example.com\nEND:VCARD\n"
            )
        }
        with mock.patch.object(self.server, "_get_carddav_client"), \
             mock.patch.object(self.server, "_resolve_addressbook_url", return_value="book"), \
             mock.patch.object(
                 self.server.carddav, "search_vcards_server_side", return_value=[entry]
             ):
            email, name = self.server._resolve_attendee_identity(None, "Aida Ramos")
        self.assertEqual(email, "aida@example.com")
        self.assertEqual(name, "Aida Ramos")

    def test_read_only_rejects_attendee_changes(self):
        server = _server(read_only=True)
        with self.assertRaisesRegex(RuntimeError, "READ_ONLY"):
            server.add_event_attendee("event-1", email="aida@example.com")


if __name__ == "__main__":
    unittest.main()

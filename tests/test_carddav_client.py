"""Tests for carddav.py's client-facing CardDAV operations.

Uses `caldav.response.DAVResponse.from_bytes` (the library's own test helper)
to build realistic multistatus replies and a minimal fake `DAVClient` stand-in,
so these exercise the PROPFIND/REPORT parsing without any network access.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from caldav.response import DAVResponse  # noqa: E402

import carddav  # noqa: E402

HOME_URL = "http://example.test/egroupware/groupdav.php/joao/"
BOOK_URL = "http://example.test/egroupware/groupdav.php/joao/addressbook/"

HOME_PROPFIND_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>{HOME_URL}</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>{BOOK_URL}</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype><D:collection/><C:addressbook/></D:resourcetype>
        <D:displayname>Contacts</D:displayname>
        <D:current-user-privilege-set>
          <D:privilege><D:write/></D:privilege>
          <D:privilege><D:read/></D:privilege>
        </D:current-user-privilege-set>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
""".encode()

MEMBERS_PROPFIND_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>{BOOK_URL}</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>{BOOK_URL}joao-silva.vcf</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
        <D:getcontenttype>text/vcard; charset=utf-8</D:getcontenttype>
        <D:getetag>"etag-1"</D:getetag>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>{BOOK_URL}maria-costa.vcf</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype/>
        <D:getcontenttype>text/vcard; charset=utf-8</D:getcontenttype>
        <D:getetag>"etag-2"</D:getetag>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
""".encode()


def _vcard(uid: str, fn: str) -> str:
    return f"BEGIN:VCARD\nVERSION:3.0\nUID:{uid}\nFN:{fn}\nEND:VCARD\n"


MULTIGET_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>{BOOK_URL}joao-silva.vcf</D:href>
    <D:propstat>
      <D:prop>
        <D:getetag>"etag-1"</D:getetag>
        <C:address-data>{_vcard("uid-joao", "Joao Silva")}</C:address-data>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>{BOOK_URL}maria-costa.vcf</D:href>
    <D:propstat>
      <D:prop>
        <D:getetag>"etag-2"</D:getetag>
        <C:address-data>{_vcard("uid-maria", "Maria Costa")}</C:address-data>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
""".encode()


def _response(body: bytes) -> DAVResponse:
    return DAVResponse.from_bytes(body, status_code=207)


class FakeClient:
    """Stand-in for caldav.DAVClient recording calls and returning canned bodies."""

    def __init__(self, propfind_bodies=None, report_bodies=None, request_responses=None):
        self._propfind_bodies = list(propfind_bodies or [])
        self._report_bodies = list(report_bodies or [])
        self._request_responses = list(request_responses or [])
        self.propfind_calls = []
        self.report_calls = []
        self.request_calls = []

    def propfind(self, url, props=None, depth=0):
        self.propfind_calls.append((url, depth))
        return _response(self._propfind_bodies.pop(0))

    def report(self, url, query="", depth=0):
        self.report_calls.append((url, query, depth))
        return _response(self._report_bodies.pop(0))

    def request(self, url, method="GET", body="", headers=None):
        self.request_calls.append((url, method, body, headers))
        return self._request_responses.pop(0)


class FakeHttpResponse:
    """Minimal stand-in for the subset of DAVResponse that put/delete_vcard use."""

    def __init__(self, status: int, raw: str = "", headers: dict | None = None):
        self.status = status
        self.raw = raw
        self.headers = headers or {}


class DiscoverAddressbooksTests(unittest.TestCase):
    def test_finds_addressbook_and_skips_home_itself(self):
        client = FakeClient(propfind_bodies=[HOME_PROPFIND_XML])
        books = carddav.discover_addressbooks(client, HOME_URL)
        self.assertEqual(len(books), 1)
        self.assertEqual(books[0]["name"], "Contacts")
        self.assertEqual(books[0]["url"], BOOK_URL)
        self.assertEqual(set(books[0]["permissions"]), {"read", "write"})
        self.assertEqual(books[0]["components"], ["VCARD"])

    def test_missing_privilege_set_defaults_to_read_only(self):
        no_privs_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:response>
    <D:href>{HOME_URL}</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>{BOOK_URL}</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype><D:collection/><C:addressbook/></D:resourcetype>
        <D:displayname>Contacts</D:displayname>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
""".encode()
        client = FakeClient(propfind_bodies=[no_privs_xml])
        books = carddav.discover_addressbooks(client, HOME_URL)
        self.assertEqual(books[0]["permissions"], ["read"])


class FetchAllVCardsTests(unittest.TestCase):
    def test_lists_members_then_multigets_them(self):
        client = FakeClient(
            propfind_bodies=[MEMBERS_PROPFIND_XML], report_bodies=[MULTIGET_XML]
        )
        entries = carddav.fetch_all_vcards(client, BOOK_URL)
        self.assertEqual(len(entries), 2)
        uids = {carddav.vcard_to_contact(e["text"])["uid"] for e in entries}
        self.assertEqual(uids, {"uid-joao", "uid-maria"})
        for e in entries:
            self.assertTrue(e["etag"])
            self.assertTrue(e["href"].startswith(BOOK_URL))
        # One PROPFIND (enumerate) + one REPORT (multiget) — no per-contact GET.
        self.assertEqual(len(client.propfind_calls), 1)
        self.assertEqual(len(client.report_calls), 1)

    def test_empty_addressbook_skips_multiget(self):
        empty_members = f"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>{BOOK_URL}</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>
""".encode()
        client = FakeClient(propfind_bodies=[empty_members])
        entries = carddav.fetch_all_vcards(client, BOOK_URL)
        self.assertEqual(entries, [])
        self.assertEqual(len(client.report_calls), 0)


class SearchAndUidLookupTests(unittest.TestCase):
    def test_search_server_side_parses_address_data(self):
        client = FakeClient(report_bodies=[MULTIGET_XML])
        entries = carddav.search_vcards_server_side(client, BOOK_URL, "joao")
        self.assertEqual(len(entries), 2)  # the fake server returns its canned body
        query_sent = client.report_calls[0][1]
        self.assertIn("joao", query_sent)
        self.assertIn("addressbook-query", query_sent)

    def test_find_by_uid_returns_none_when_no_match(self):
        empty = b"""<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav"></D:multistatus>
"""
        client = FakeClient(report_bodies=[empty])
        self.assertIsNone(carddav.find_vcard_by_uid_server_side(client, BOOK_URL, "no-such-uid"))

    def test_find_by_uid_returns_the_match(self):
        client = FakeClient(report_bodies=[MULTIGET_XML])
        found = carddav.find_vcard_by_uid_server_side(client, BOOK_URL, "uid-joao")
        self.assertIsNotNone(found)
        self.assertIn("joao-silva.vcf", found["href"])


class PutDeleteVcardTests(unittest.TestCase):
    def test_put_vcard_success_returns_new_etag(self):
        client = FakeClient(
            request_responses=[FakeHttpResponse(201, headers={"ETag": '"new-etag"'})]
        )
        href = f"{BOOK_URL}joao-silva.vcf"
        etag = carddav.put_vcard(client, href, _vcard("uid-joao", "Joao Silva"), etag='"old-etag"')
        self.assertEqual(etag, '"new-etag"')
        url, method, body, headers = client.request_calls[0]
        self.assertEqual(url, href)
        self.assertEqual(method, "PUT")
        self.assertEqual(headers["If-Match"], '"old-etag"')

    def test_put_vcard_error_raises_carddaverror(self):
        client = FakeClient(request_responses=[FakeHttpResponse(409, raw="conflict")])
        with self.assertRaises(carddav.CardDAVError):
            carddav.put_vcard(client, f"{BOOK_URL}joao-silva.vcf", _vcard("uid-joao", "Joao Silva"))

    def test_delete_vcard_success(self):
        client = FakeClient(request_responses=[FakeHttpResponse(204)])
        carddav.delete_vcard(client, f"{BOOK_URL}joao-silva.vcf", etag='"etag-1"')
        url, method, body, headers = client.request_calls[0]
        self.assertEqual(method, "DELETE")
        self.assertEqual(headers["If-Match"], '"etag-1"')

    def test_delete_vcard_error_raises_carddaverror(self):
        client = FakeClient(request_responses=[FakeHttpResponse(404, raw="not found")])
        with self.assertRaises(carddav.CardDAVError):
            carddav.delete_vcard(client, f"{BOOK_URL}missing.vcf")


if __name__ == "__main__":
    unittest.main()

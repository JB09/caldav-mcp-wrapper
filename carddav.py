"""CardDAV protocol glue and vCard <-> dict mapping for the contacts tools.

python-caldav (the library `server.py` already uses for calendars) has no
address-book support at all: no `AddressBook` class, no vCard REPORT/PROPFIND
helpers. Rather than add a second HTTP client/auth stack, this module builds on
the same `caldav.DAVClient` the calendar code already authenticates with,
issuing the raw WebDAV requests CardDAV needs (RFC 6352) directly:

- `PROPFIND` to discover address-book collections and their member resources.
- `REPORT addressbook-multiget` / `addressbook-query` to fetch or search vCards.
- `PUT` / `DELETE` to write and remove them.

vCard parsing/generation uses `vobject`, a small, well-established library that
(unlike `icalendar`, already a dependency here but iCalendar-only) understands
vCard's `TYPE=HOME`/`TYPE=WORK`/`TYPE=CELL` parameters and preserves properties
it does not know about — important for the update tool's read-modify-write
requirement: fields the caller did not ask to change must survive untouched.

Nothing here is EGroupware-specific beyond the URL layout `server.py` builds;
the REPORT/PROPFIND bodies are plain RFC 6352.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from urllib.parse import urljoin
from xml.sax.saxutils import escape as xml_escape

import vobject

logger = logging.getLogger("caldav-mcp.carddav")

DAV_NS = "DAV:"
CARD_NS = "urn:ietf:params:xml:ns:carddav"


class CardDAVError(RuntimeError):
    """Raised when the CardDAV server returns something tools cannot use."""


# --- Low-level XML request bodies (RFC 6352) ------------------------------


def propfind_addressbooks_body() -> str:
    """PROPFIND body to discover address-book collections under a home set."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<D:propfind xmlns:D="{DAV_NS}" xmlns:C="{CARD_NS}">'
        "<D:prop>"
        "<D:resourcetype/>"
        "<D:displayname/>"
        "<D:current-user-privilege-set/>"
        "</D:prop>"
        "</D:propfind>"
    )


def propfind_members_body() -> str:
    """PROPFIND body to enumerate the vCard resources in an address book."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<D:propfind xmlns:D="{DAV_NS}">'
        "<D:prop>"
        "<D:resourcetype/>"
        "<D:getcontenttype/>"
        "<D:getetag/>"
        "</D:prop>"
        "</D:propfind>"
    )


def multiget_body(hrefs: list[str]) -> str:
    """REPORT body fetching `address-data` for an explicit list of hrefs."""
    href_xml = "".join(f"<D:href>{xml_escape(h)}</D:href>" for h in hrefs)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<C:addressbook-multiget xmlns:D="{DAV_NS}" xmlns:C="{CARD_NS}">'
        "<D:prop>"
        "<D:getetag/>"
        "<C:address-data/>"
        "</D:prop>"
        f"{href_xml}"
        "</C:addressbook-multiget>"
    )


# Properties searched by both the UID lookup and the free-text search. Order
# does not matter: the filter below combines them with a logical OR.
_SEARCHABLE_PROPS = ("FN", "N", "ORG", "EMAIL", "TEL", "NICKNAME", "CATEGORIES")


def query_body_for_text(query: str, props: tuple[str, ...] = _SEARCHABLE_PROPS) -> str:
    """REPORT body for a server-side `addressbook-query` substring search.

    Combines a `text-match` per property with `test="anyof"` (logical OR), so a
    contact matches if *any* of name/organization/email/phone/category contains
    `query` (case-insensitive).
    """
    escaped = xml_escape(query)
    prop_filters = "".join(
        f'<C:prop-filter name="{name}">'
        f'<C:text-match collation="i;unicode-casemap" match-type="contains">{escaped}'
        "</C:text-match>"
        "</C:prop-filter>"
        for name in props
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<C:addressbook-query xmlns:D="{DAV_NS}" xmlns:C="{CARD_NS}">'
        "<D:prop>"
        "<D:getetag/>"
        "<C:address-data/>"
        "</D:prop>"
        f'<C:filter test="anyof">{prop_filters}</C:filter>'
        "</C:addressbook-query>"
    )


def query_body_for_uid(uid: str) -> str:
    """REPORT body for a server-side exact UID lookup."""
    escaped = xml_escape(uid)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<C:addressbook-query xmlns:D="{DAV_NS}" xmlns:C="{CARD_NS}">'
        "<D:prop>"
        "<D:getetag/>"
        "<C:address-data/>"
        "</D:prop>"
        '<C:filter test="anyof">'
        '<C:prop-filter name="UID">'
        f'<C:text-match collation="i;unicode-casemap" match-type="equals">{escaped}'
        "</C:text-match>"
        "</C:prop-filter>"
        "</C:filter>"
        "</C:addressbook-query>"
    )


# --- Response parsing (plain lxml; the caldav lib has no CardDAV support) --


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(elem) -> str | None:
    return elem.text if elem is not None and elem.text else None


def parse_multistatus_responses(tree) -> list[dict]:
    """Parse a WebDAV multistatus `<D:response>` list into plain dicts.

    Returns one entry per `<D:response>`: `{"href": str, "status": int,
    "props": {localname: element}}`. Deliberately returns raw lxml elements for
    `props` (not text) since callers need both simple text props
    (`getetag`, `displayname`) and structured ones (`resourcetype`,
    `current-user-privilege-set`, `address-data`).
    """
    if tree is None:
        return []
    results = []
    for response in tree.iter(f"{{{DAV_NS}}}response"):
        href_elem = response.find(f"{{{DAV_NS}}}href")
        href = _text(href_elem)
        if not href:
            continue
        status = 200
        props: dict = {}
        for propstat in response.iter(f"{{{DAV_NS}}}propstat"):
            status_elem = propstat.find(f"{{{DAV_NS}}}status")
            status_text = _text(status_elem) or ""
            try:
                status = int(status_text.split(" ")[1]) if status_text else 200
            except (IndexError, ValueError):
                status = 200
            if status >= 300:
                continue
            prop_elem = propstat.find(f"{{{DAV_NS}}}prop")
            if prop_elem is None:
                continue
            for child in prop_elem:
                props[_localname(child.tag)] = child
        results.append({"href": href, "status": status, "props": props})
    return results


def is_addressbook_collection(props: dict) -> bool:
    """True when a PROPFIND `resourcetype` prop marks this as an address book."""
    resourcetype = props.get("resourcetype")
    if resourcetype is None:
        return False
    return any(_localname(child.tag) == "addressbook" for child in resourcetype)


def privileges(props: dict) -> list[str]:
    """Best-effort list of privilege names from `current-user-privilege-set`."""
    privset = props.get("current-user-privilege-set")
    if privset is None:
        return []
    names = []
    for priv in privset:
        for child in priv:
            names.append(_localname(child.tag))
    return names


def extract_address_data(props: dict) -> str | None:
    elem = props.get("address-data")
    return _text(elem)


def extract_etag(props: dict) -> str | None:
    elem = props.get("getetag")
    return _text(elem)


# --- vCard <-> dict mapping -------------------------------------------------


def _single(vcard, name: str) -> str | None:
    child = getattr(vcard, name, None)
    return str(child.value) if child is not None else None


def _entries(vcard, name: str, value_attr: str = "value") -> list[dict]:
    """Return every `name` line as `{"type": ..., "value": ...}`.

    `type` is the first `TYPE` parameter when present (e.g. "HOME", "WORK",
    "CELL"), else `None`. Multiple `TYPE` params (`TYPE=HOME,VOICE`) are joined
    with a comma so none are silently dropped.
    """
    out = []
    for child in vcard.contents.get(name.lower(), []):
        type_param = child.params.get("TYPE")
        type_value = ",".join(type_param) if type_param else None
        value = getattr(child, value_attr, child.value)
        out.append({"type": type_value, "value": str(value)})
    return out


def _addresses(vcard) -> list[dict]:
    out = []
    for child in vcard.contents.get("adr", []):
        type_param = child.params.get("TYPE")
        type_value = ",".join(type_param) if type_param else None
        v = child.value
        out.append(
            {
                "type": type_value,
                "street": getattr(v, "street", "") or None,
                "city": getattr(v, "city", "") or None,
                "region": getattr(v, "region", "") or None,
                "code": getattr(v, "code", "") or None,
                "country": getattr(v, "country", "") or None,
            }
        )
    return out


def vcard_to_contact(vcard_text: str, href: str | None = None, etag: str | None = None) -> dict:
    """Parse a raw vCard into a structured, JSON-serializable dict.

    Only fields actually present in the vCard are populated; nothing is
    invented. `href`/`etag` (when given) identify the server resource so
    `update_contact`/`delete_contact` can target it without a further lookup.
    """
    vcard = vobject.readOne(vcard_text)
    given = family = None
    if hasattr(vcard, "n"):
        n = vcard.n.value
        given = getattr(n, "given", "") or None
        family = getattr(n, "family", "") or None

    return {
        "uid": _single(vcard, "uid"),
        "href": href,
        "etag": etag,
        "full_name": _single(vcard, "fn"),
        "given_name": given,
        "family_name": family,
        "organization": ", ".join(vcard.org.value) if hasattr(vcard, "org") else None,
        "title": _single(vcard, "title"),
        "emails": _entries(vcard, "email"),
        "phones": _entries(vcard, "tel"),
        "addresses": _addresses(vcard),
        "birthday": _single(vcard, "bday"),
        "notes": _single(vcard, "note"),
        "url": _single(vcard, "url"),
        "categories": list(vcard.categories.value) if hasattr(vcard, "categories") else [],
        "modified": _single(vcard, "rev"),
    }


def _apply_type(line, type_value: str | None) -> None:
    if type_value:
        line.type_param = type_value


def _set_org(vcard, organization: str) -> None:
    if hasattr(vcard, "org"):
        vcard.remove(vcard.org)
    vcard.add("org").value = [organization]


def _set_list_field(vcard, name: str, entries: list[dict], value_keys=("value",)) -> None:
    """Replace every line of `name` with the given entries (list semantics).

    Used for multi-valued fields (email/tel/adr/categories): the caller passes
    the *complete* desired list, since matching individual existing entries by
    type to merge them would be ambiguous (e.g. two HOME phone numbers).
    Scalar fields are updated in place instead, see `_set_scalar`.
    """
    for existing in list(vcard.contents.get(name.lower(), [])):
        vcard.remove(existing)
    for entry in entries:
        if name.lower() == "adr":
            line = vcard.add("adr")
            line.value = vobject.vcard.Address(
                street=entry.get("street") or "",
                city=entry.get("city") or "",
                region=entry.get("region") or "",
                code=entry.get("code") or "",
                country=entry.get("country") or "",
            )
        else:
            line = vcard.add(name.lower())
            line.value = entry.get("value", "")
        _apply_type(line, entry.get("type"))


def _set_scalar(vcard, name: str, value) -> None:
    if hasattr(vcard, name):
        vcard.remove(getattr(vcard, name))
    if value is not None:
        vcard.add(name).value = value


def build_vcard(fields: dict) -> str:
    """Build a brand-new vCard from structured `create_contact` fields.

    Generates a UID when the caller did not supply one (the EGroupware CardDAV
    endpoint does not allocate one on its own — unlike some servers' calendar
    PUT, which may rewrite a missing UID, a vCard with no UID is simply invalid
    and is better never sent).
    """
    full_name = fields.get("full_name") or " ".join(
        p for p in (fields.get("given_name"), fields.get("family_name")) if p
    )
    if not full_name:
        raise ValueError(
            "create_contact requires at least one of full_name, given_name, or "
            "family_name."
        )

    vcard = vobject.vCard()
    vcard.add("uid").value = fields.get("uid") or f"{uuid.uuid4()}"
    vcard.add("fn").value = full_name
    if fields.get("given_name") or fields.get("family_name"):
        n = vcard.add("n")
        n.value = vobject.vcard.Name(
            given=fields.get("given_name") or "", family=fields.get("family_name") or ""
        )
    if fields.get("organization"):
        _set_org(vcard, fields["organization"])
    if fields.get("title"):
        vcard.add("title").value = fields["title"]
    if fields.get("emails"):
        _set_list_field(vcard, "email", fields["emails"])
    if fields.get("phones"):
        _set_list_field(vcard, "tel", fields["phones"])
    if fields.get("addresses"):
        _set_list_field(vcard, "adr", fields["addresses"])
    if fields.get("birthday"):
        vcard.add("bday").value = fields["birthday"]
    if fields.get("notes"):
        vcard.add("note").value = fields["notes"]
    if fields.get("url"):
        vcard.add("url").value = fields["url"]
    if fields.get("categories"):
        vcard.add("categories").value = list(fields["categories"])
    return vcard.serialize()


def apply_updates(vcard_text: str, fields: dict) -> str:
    """Read-modify-write: apply only the fields the caller actually passed.

    `fields` values that are `None` mean "leave unchanged" — including list
    fields (emails/phones/addresses/categories), which are *replaced in full*
    when provided but left untouched when omitted. This is what keeps
    `update_contact("912345678")` from wiping the email/address/birthday/notes
    a caller did not mention.
    """
    vcard = vobject.readOne(vcard_text)

    if fields.get("full_name") is not None:
        _set_scalar(vcard, "fn", fields["full_name"])
    if fields.get("given_name") is not None or fields.get("family_name") is not None:
        existing = vcard.n.value if hasattr(vcard, "n") else vobject.vcard.Name()
        given = fields.get("given_name")
        family = fields.get("family_name")
        n = vcard.add("n") if not hasattr(vcard, "n") else vcard.n
        n.value = vobject.vcard.Name(
            given=given if given is not None else getattr(existing, "given", ""),
            family=family if family is not None else getattr(existing, "family", ""),
        )
    if fields.get("organization") is not None:
        _set_org(vcard, fields["organization"])
    if fields.get("title") is not None:
        _set_scalar(vcard, "title", fields["title"])
    if fields.get("emails") is not None:
        _set_list_field(vcard, "email", fields["emails"])
    if fields.get("phones") is not None:
        _set_list_field(vcard, "tel", fields["phones"])
    if fields.get("addresses") is not None:
        _set_list_field(vcard, "adr", fields["addresses"])
    if fields.get("birthday") is not None:
        _set_scalar(vcard, "bday", fields["birthday"])
    if fields.get("notes") is not None:
        _set_scalar(vcard, "note", fields["notes"])
    if fields.get("url") is not None:
        _set_scalar(vcard, "url", fields["url"])
    if fields.get("categories") is not None:
        _set_scalar(vcard, "categories", list(fields["categories"]) or None)
    return vcard.serialize()


# --- Search -----------------------------------------------------------------


def matches_query(contact: dict, query: str) -> bool:
    """Client-side fallback search, used when the server REPORT is refused.

    Substring, case-insensitive, over the same fields the server-side filter
    targets: name, full name, organization, emails, phones, categories.
    """
    needle = query.strip().lower()
    if not needle:
        return True
    haystacks = [
        contact.get("full_name"),
        contact.get("given_name"),
        contact.get("family_name"),
        contact.get("organization"),
        *[e.get("value") for e in contact.get("emails", [])],
        *[p.get("value") for p in contact.get("phones", [])],
        *(contact.get("categories") or []),
    ]
    return any(h and needle in h.lower() for h in haystacks)


# --- Birthdays ---------------------------------------------------------------


def parse_birthday(value: str) -> tuple[int, int, int | None]:
    """Parse a `BDAY` value into `(month, day, year_or_None)`.

    Handles the common vCard spellings: a full `YYYY-MM-DD`/`YYYYMMDD` date, and
    a year-less `--MM-DD`/`--MMDD` (vCard 4 "reduced accuracy", also emitted by
    some vCard 3 producers for a birthday with no known year). A handful of
    servers use an obviously-fake placeholder year (`0000`, `1604`) to mean "no
    year known" in an otherwise full date; those are treated the same as a
    year-less value rather than producing a nonsense age.

    Raises ValueError if the value cannot be parsed as a date at all.
    """
    raw = value.strip()
    if raw.startswith("--"):
        digits = raw[2:].replace("-", "")
        if len(digits) != 4:
            raise ValueError(f"Unrecognized partial BDAY value: {value!r}")
        return int(digits[:2]), int(digits[2:]), None

    digits = raw.replace("-", "")
    if len(digits) == 8 and digits.isdigit():
        year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        if year in (0, 1604, 1000):
            return month, day, None
        return month, day, year

    # Fall back to ISO parsing for anything else date-shaped (e.g. with a time
    # component some servers append).
    parsed = date.fromisoformat(raw[:10])
    year = None if parsed.year in (1604, 1000) else parsed.year
    return parsed.month, parsed.day, year


def next_occurrence(month: int, day: int, on_or_after: date) -> date:
    """Return the next anniversary of `month`/`day` on/after `on_or_after`.

    Falls back to Feb 28 in non-leap years for a Feb 29 birthday, matching how
    most calendar apps treat it.
    """
    for year in (on_or_after.year, on_or_after.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            candidate = date(year, 2, 28) if (month, day) == (2, 29) else None
            if candidate is None:
                raise
        if candidate >= on_or_after:
            return candidate
    raise AssertionError("unreachable")  # pragma: no cover


# --- Client-facing CardDAV operations ---------------------------------------
#
# These call a `caldav.DAVClient` directly (its low-level `propfind`/`report`/
# `request` methods, which are generic WebDAV and not CalDAV-specific) rather
# than python-caldav's `Calendar`/`Principal` objects, which have no CardDAV
# counterpart. Every raised exception here is either a `CardDAVError` this
# module produces, or an unmodified exception from the caldav library / HTTP
# stack — callers are expected to let it surface rather than swallow it.


def _absolute(base_url: str, href: str) -> str:
    """Resolve a (possibly relative) href against the request's base URL."""
    return urljoin(base_url, href)


def discover_addressbooks(client, home_url: str) -> list[dict]:
    """PROPFIND `home_url` and return its address-book child collections.

    Each entry: `{"name", "url", "permissions", "components": ["VCARD"]}`.
    `permissions` is `["read"]` or `["read", "write"]`, best-effort from
    `current-user-privilege-set` (empty when the server does not advertise it).
    """
    response = client.propfind(home_url, props=propfind_addressbooks_body(), depth=1)
    out = []
    for entry in parse_multistatus_responses(response.tree):
        if entry["status"] >= 300:
            continue
        props = entry["props"]
        if not is_addressbook_collection(props):
            continue
        url = _absolute(home_url, entry["href"])
        if url.rstrip("/") == home_url.rstrip("/"):
            continue  # the home collection itself, not an address book
        privs = privileges(props)
        permissions = ["read"]
        if not privs or "write" in privs or "all" in privs or "write-content" in privs:
            permissions.append("write")
        display_name = _text(props.get("displayname"))
        out.append(
            {
                "name": display_name or url.rstrip("/").rsplit("/", 1)[-1],
                "url": url,
                "permissions": permissions,
                "components": ["VCARD"],
            }
        )
    return out


def list_member_hrefs(client, addressbook_url: str) -> list[str]:
    """PROPFIND `addressbook_url` (depth 1) and return its vCard member hrefs."""
    response = client.propfind(addressbook_url, props=propfind_members_body(), depth=1)
    hrefs = []
    for entry in parse_multistatus_responses(response.tree):
        if entry["status"] >= 300:
            continue
        url = _absolute(addressbook_url, entry["href"])
        if url.rstrip("/") == addressbook_url.rstrip("/"):
            continue  # the collection itself
        props = entry["props"]
        if is_addressbook_collection(props):
            continue  # a sub-collection, not a vCard resource
        content_type = _text(props.get("getcontenttype")) or ""
        resourcetype = props.get("resourcetype")
        is_collection = resourcetype is not None and any(
            _localname(c.tag) == "collection" for c in resourcetype
        )
        if is_collection:
            continue
        if content_type and "vcard" not in content_type.lower():
            continue
        hrefs.append(entry["href"])
    return hrefs


def _reports_to_vcards(client, addressbook_url: str, response) -> list[dict]:
    out = []
    for entry in parse_multistatus_responses(response.tree):
        if entry["status"] >= 300:
            continue
        text = extract_address_data(entry["props"])
        if not text:
            continue
        out.append(
            {
                "href": _absolute(addressbook_url, entry["href"]),
                "etag": extract_etag(entry["props"]),
                "text": text,
            }
        )
    return out


def fetch_all_vcards(client, addressbook_url: str) -> list[dict]:
    """Return every vCard in the collection as `{"href", "etag", "text"}`.

    Two requests (PROPFIND to enumerate hrefs, then `addressbook-multiget` for
    their content) rather than one `addressbook-query` with an empty filter:
    RFC 6352 does not define what an empty `<C:filter>` matches, so servers are
    free to treat it as "match nothing" — multiget has no such ambiguity, it is
    handed the exact hrefs to fetch.
    """
    hrefs = list_member_hrefs(client, addressbook_url)
    if not hrefs:
        return []
    response = client.report(addressbook_url, multiget_body(hrefs), depth=1)
    return _reports_to_vcards(client, addressbook_url, response)


def search_vcards_server_side(client, addressbook_url: str, query: str) -> list[dict]:
    """Server-side `addressbook-query` substring search across name/org/email/etc.

    Raises on any failure (network, or the server rejecting/not supporting the
    REPORT); callers fall back to `fetch_all_vcards` + `matches_query`.
    """
    response = client.report(addressbook_url, query_body_for_text(query), depth=1)
    return _reports_to_vcards(client, addressbook_url, response)


def find_vcard_by_uid_server_side(client, addressbook_url: str, uid: str) -> dict | None:
    """Server-side exact UID lookup. Raises on failure; see `search_vcards_server_side`."""
    response = client.report(addressbook_url, query_body_for_uid(uid), depth=1)
    matches = _reports_to_vcards(client, addressbook_url, response)
    return matches[0] if matches else None


def put_vcard(client, href: str, vcard_text: str, etag: str | None = None) -> str | None:
    """PUT a vCard to `href`, creating or replacing it. Returns the new ETag, if any.

    Sends `If-Match` when `etag` is given, so a concurrent change on the server
    is reported as a clear conflict rather than being silently overwritten.
    """
    headers = {"Content-Type": "text/vcard; charset=utf-8"}
    if etag:
        headers["If-Match"] = etag
    response = client.request(href, "PUT", vcard_text, headers)
    if response.status >= 300:
        raise CardDAVError(f"PUT {href} failed with HTTP {response.status}: {response.raw}")
    return response.headers.get("ETag")


def delete_vcard(client, href: str, etag: str | None = None) -> None:
    """DELETE a vCard resource. Raises `CardDAVError` on a non-2xx status."""
    headers = {"If-Match": etag} if etag else None
    response = client.request(href, "DELETE", "", headers)
    if response.status >= 300:
        raise CardDAVError(f"DELETE {href} failed with HTTP {response.status}.")

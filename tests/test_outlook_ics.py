import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import patch

from sage import config, db, outlook


class OutlookIcsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = patch.multiple(config, DATA=root, DB_PATH=root / 'sage.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init()

    def test_connect_stores_multiple_links_locally_and_status_prefers_ics(self):
        url = 'https://calendar.example.net/private/calendar.ics'
        other_url = 'https://calendar.example.org/published.ics'
        with patch('sage.outlook._dpapi', side_effect=lambda data, protect: data):
            outlook.connect_ics(url)
            outlook.connect_ics(other_url)
            state = outlook.status()
            stored = outlook._load_ics_urls()

        self.assertEqual(stored, [url, other_url])
        self.assertTrue(state['ics_connected'])
        self.assertEqual(state['source'], 'ics')
        self.assertEqual([feed['host'] for feed in state['ics_feeds']],
                         ['calendar.example.net', 'calendar.example.org'])
        self.assertNotIn(url, str(state))
        self.assertNotIn(other_url, str(state))

    def test_rejects_unsafe_or_malformed_urls(self):
        for url in ('http://calendar.example.net/calendar.ics',
                    'https://user:password@calendar.example.net/calendar.ics',
                    'https://user:password@outlook.live.com/calendar.ics',
                    'https://calendar.example.net/calendar.ics#fragment',
                    'https://calendar.example.net:8443/calendar.ics',
                    'not a URL'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                outlook.connect_ics(url)

    def test_parses_and_filters_timed_and_all_day_events(self):
        feed = '\r\n'.join([
            'BEGIN:VCALENDAR',
            'BEGIN:VEVENT',
            'UID:one',
            'DTSTART:20261015T140000Z',
            'DTEND:20261015T150000Z',
            'SUMMARY:Team sync\\, review',
            'LOCATION:Room 3',
            'END:VEVENT',
            'BEGIN:VEVENT',
            'UID:two',
            'DTSTART;VALUE=DATE:20261016',
            'DTEND;VALUE=DATE:20261017',
            'SUMMARY:Focus day',
            'END:VEVENT',
            'BEGIN:VEVENT',
            'UID:outside',
            'DTSTART:20261020T140000Z',
            'DTEND:20261020T150000Z',
            'SUMMARY:Later',
            'END:VEVENT',
            'END:VCALENDAR',
            '',
        ])

        events = outlook._parse_ics_events(
            feed,
            datetime(2026, 10, 15, tzinfo=timezone.utc),
            datetime(2026, 10, 18, tzinfo=timezone.utc),
        )

        self.assertEqual([event['id'] for event in events], ['one', 'two'])
        self.assertEqual(events[0]['subject'], 'Team sync, review')
        self.assertEqual(events[0]['location'], 'Room 3')
        self.assertTrue(events[1]['is_all_day'])

    def test_expands_weekly_recurring_events_and_honors_exdates(self):
        feed = '\r\n'.join([
            'BEGIN:VCALENDAR',
            'BEGIN:VEVENT',
            'UID:weekly',
            'DTSTART:20261008T140000Z',
            'DTEND:20261008T150000Z',
            'RRULE:FREQ=WEEKLY;BYDAY=TH;COUNT=5',
            'EXDATE:20261015T140000Z',
            'SUMMARY:Weekly sync',
            'END:VEVENT',
            'END:VCALENDAR',
            '',
        ])

        events = outlook._parse_ics_events(
            feed,
            datetime(2026, 10, 15, tzinfo=timezone.utc),
            datetime(2026, 10, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(len(events), 2)
        self.assertEqual([event['id'] for event in events], ['weekly', 'weekly'])
        self.assertTrue(all(event['subject'] == 'Weekly sync' for event in events))

    def test_reads_outlook_windows_time_zone_ids(self):
        event_start, all_day = outlook._parse_ics_datetime(
            {'TZID': 'Pacific Standard Time'},
            '20261015T140000',
        )
        self.assertFalse(all_day)
        self.assertEqual(
            outlook._utc(event_start),
            datetime(2026, 10, 15, 21, tzinfo=timezone.utc),
        )

    def test_ics_feed_is_read_only_for_event_creation(self):
        with self.assertRaisesRegex(PermissionError, 'ICS feed is read-only'):
            outlook.create_event(
                'Planning',
                '2026-10-15T14:00:00+00:00',
                '2026-10-15T15:00:00+00:00',
            )

    def test_disconnect_removes_saved_ics_link(self):
        with patch('sage.outlook._dpapi', side_effect=lambda data, protect: data):
            first = outlook.connect_ics('https://calendar.example.net/private/calendar.ics')
            outlook.connect_ics('https://calendar.example.org/private/calendar.ics')
            outlook.disconnect_ics(first['feed_id'])
            self.assertEqual(len(outlook.status()['ics_feeds']), 1)
            outlook.disconnect_ics()
        self.assertFalse(outlook.status()['ics_connected'])

    def test_events_between_uses_ics_when_both_connections_are_available(self):
        start = datetime(2026, 10, 15, tzinfo=timezone.utc)
        end = datetime(2026, 10, 16, tzinfo=timezone.utc)
        events = [{'id': 'ics-event'}]
        with patch('sage.outlook._load_ics_urls', return_value=['published-url']), \
                patch('sage.outlook._ics_events_between', return_value=events) as read_ics, \
                patch('sage.outlook._graph_events_between') as read_graph:
            result = outlook.events_between(start, end)

        self.assertEqual(result, events)
        read_ics.assert_called_once_with(start, end)
        read_graph.assert_not_called()

    def test_ics_events_between_reads_all_connected_feeds(self):
        start = datetime(2026, 10, 15, tzinfo=timezone.utc)
        end = datetime(2026, 10, 16, tzinfo=timezone.utc)
        feeds = [
            'https://one.example.net/calendar.ics',
            'https://two.example.org/calendar.ics',
        ]
        parsed = [
            {'id': 'one', 'start': '2026-10-15T10:00:00+00:00'},
            {'id': 'two', 'start': '2026-10-15T11:00:00+00:00'},
        ]

        class Response:
            def __init__(self, content):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return self.content

        opener = type('Opener', (), {
            'open': lambda self, request, timeout: Response(request.full_url.encode())
        })()
        with patch('sage.outlook._load_ics_urls', return_value=feeds), \
                patch('sage.outlook.build_opener', return_value=opener), \
                patch('sage.outlook._parse_ics_events',
                      side_effect=lambda content, start, end: [parsed[0] if 'one.example.net' in content else parsed[1]]) as parse:
            events = outlook._ics_events_between(start, end)

        self.assertEqual([event['calendar'] for event in events],
                         ['one.example.net', 'two.example.org'])
        self.assertEqual([event['id'] for event in events], ['one', 'two'])
        self.assertEqual(parse.call_count, 2)

    def test_ics_feed_cannot_resolve_to_private_addresses(self):
        with self.assertRaisesRegex(ValueError, 'public IP'):
            outlook._public_ics_addresses('127.0.0.1', 443)
        with patch('sage.outlook.socket.getaddrinfo', return_value=[
            (None, None, None, None, ('8.8.8.8', 443)),
            (None, None, None, None, ('192.168.1.1', 443)),
        ]):
            with self.assertRaisesRegex(ValueError, 'public IP'):
                outlook._public_ics_addresses('calendar.example.net', 443)

    def test_feed_http_handler_blocks_private_hosts(self):
        opener = build_opener(
            ProxyHandler({}),
            outlook._PublicHTTPSHandler(),
            outlook._ICSRedirectHandler(),
        )
        with self.assertRaisesRegex(ValueError, 'public IP'):
            opener.open(Request('https://127.0.0.1/calendar.ics'), timeout=1)

    def test_loads_existing_single_url_storage(self):
        old_url = 'https://outlook.office365.com/owa/calendar/private/calendar.ics'
        path = outlook._ics_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(old_url, encoding='utf-8')
        with patch('sage.outlook._dpapi', side_effect=lambda data, protect: data):
            self.assertEqual(outlook._load_ics_urls(), [old_url])

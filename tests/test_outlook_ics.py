import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
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

    def test_connect_stores_link_locally_and_status_prefers_ics(self):
        url = 'https://outlook.office365.com/owa/calendar/private/calendar.ics'
        with patch('sage.outlook._dpapi', side_effect=lambda data, protect: data):
            outlook.connect_ics(url)
            state = outlook.status()
            stored = outlook._load_ics_url()

        self.assertEqual(stored, url)
        self.assertTrue(state['ics_connected'])
        self.assertEqual(state['source'], 'ics')
        self.assertNotIn(url, str(state))

    def test_rejects_non_outlook_or_non_https_urls(self):
        for url in ('http://outlook.office365.com/calendar.ics',
                    'https://example.com/calendar.ics',
                    'https://user:password@outlook.live.com/calendar.ics',
                    'https://outlook.live.com:8443/calendar.ics'):
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
            outlook.connect_ics('https://outlook.live.com/owa/calendar/private/calendar.ics')
            outlook.disconnect_ics()
        self.assertFalse(outlook.status()['ics_connected'])

    def test_events_between_uses_ics_when_both_connections_are_available(self):
        start = datetime(2026, 10, 15, tzinfo=timezone.utc)
        end = datetime(2026, 10, 16, tzinfo=timezone.utc)
        events = [{'id': 'ics-event'}]
        with patch('sage.outlook._load_ics_url', return_value='published-url'), \
                patch('sage.outlook._ics_events_between', return_value=events) as read_ics, \
                patch('sage.outlook._graph_events_between') as read_graph:
            result = outlook.events_between(start, end)

        self.assertEqual(result, events)
        read_ics.assert_called_once_with(start, end)
        read_graph.assert_not_called()

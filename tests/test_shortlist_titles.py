"""Readable coverage names retain kind, dates, timezone and split-list identity."""
import unittest
from job_search.job_reviews.old_method.titles import list_title

class TitleTests(unittest.TestCase):
    def test_same_day(self):
        self.assertEqual(list_title('targeted', '2026-10-10T06:00:00Z', '2026-10-10T17:00:00Z'),
                         'Targeted · Oct 10, 2026 · 1:00 AM–12:00 PM CDT')
    def test_multiday_and_split(self):
        self.assertEqual(list_title('broad', '2026-10-09T23:00:00Z', '2026-10-10T17:00:00Z', part=2, parts=3),
                         'Broad · Oct 9, 2026, 6:00 PM CDT → Oct 10, 2026, 12:00 PM CDT · Part 2/3')
    def test_dst_fallback_is_unambiguous(self):
        self.assertEqual(list_title('targeted', '2026-11-01T06:00:00Z', '2026-11-01T07:00:00Z'),
                         'Targeted · Nov 1, 2026, 1:00 AM CDT → Nov 1, 2026, 1:00 AM CST')
    def test_cross_year(self):
        title = list_title('broad', '2027-01-01T05:00:00Z', '2027-01-01T07:00:00Z')
        self.assertIn('Dec 31, 2026', title)
        self.assertIn('Jan 1, 2027', title)

if __name__ == '__main__':
    unittest.main()

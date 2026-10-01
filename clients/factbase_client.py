import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://rollcall.com/wp-json/factbase/v1/twitter"

# Roll Call's Factbase archive of Trump's social media posts (Truth Social,
# formerly Twitter). The web UI has a "Custom Date Range" filter, but its
# start_date/end_date wiring is dead code (confirmed by reading the plugin's
# JS -- the relevant lines are commented out), so passing those params to
# the API silently does nothing. The only thing that reliably works is
# pagination sorted by date descending (page 1 = most recent), so historical
# lookups are done via binary search over page number.


def fetch_page(page, page_size=50, max_retries=5):
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                BASE_URL,
                params={"sort": "date", "sort_order": "desc", "page": page, "page_size": page_size},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == max_retries - 1:
                raise
            time.sleep(2**attempt)


def _post_date(post):
    return datetime.fromisoformat(post["date"]).astimezone(timezone.utc)


def find_page_for_date(target_date, page_count):
    """Binary search: page N's last (oldest) post date, decreasing as N
    grows. Finds the smallest page whose oldest post is <= target_date."""
    lo, hi = 1, page_count
    while lo < hi:
        mid = (lo + hi) // 2
        page = fetch_page(mid, page_size=5)
        last_date = _post_date(page["data"][-1])
        if last_date > target_date:
            lo = mid + 1
        else:
            hi = mid
    return lo


def get_posts_for_window(start_date_str, end_date_str, max_pages=15):
    """All posts with date in [start_date, end_date] (inclusive, UTC days)."""
    start = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

    meta = fetch_page(1, page_size=1)["meta"]
    page_count = meta["page_count"]

    start_page = find_page_for_date(end, page_count)

    posts = []
    page = max(1, start_page - 1)  # back off one page in case the boundary search overshot
    for _ in range(max_pages):
        data = fetch_page(page, page_size=50)
        rows = data["data"]
        if not rows:
            break
        for post in rows:
            d = _post_date(post)
            if start <= d <= end:
                posts.append(post)
        oldest = _post_date(rows[-1])
        if oldest < start:
            break
        page += 1

    return posts

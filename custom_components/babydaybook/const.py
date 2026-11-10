"""Constants for the Baby Daybook integration."""

from datetime import timedelta

DOMAIN = "babydaybook"

CONF_REFRESH_TOKEN = "refresh_token"

# Every poll asks only for records changed since the last one, which normally
# returns nothing. A full reload of the recent records runs as a safety net.
POLL_INTERVAL = timedelta(seconds=30)
FULL_REFRESH_INTERVAL = timedelta(minutes=15)
RECENT_ACTIONS = 50
MAX_CACHED_ACTIONS = 200

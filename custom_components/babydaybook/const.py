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

# "Woke up at ..." may move the end of an already finished sleep, but only by
# this much, so a slip of the tongue cannot stretch yesterday's sleep to today.
MAX_END_CORRECTION = timedelta(hours=3)

OUTCOME_LOGGED = "logged"
OUTCOME_STARTED = "started"
OUTCOME_STOPPED = "stopped"
OUTCOME_SWITCHED = "switched"
OUTCOME_CORRECTED = "corrected"
OUTCOME_DELETED = "deleted"
OUTCOME_UNCHANGED = "unchanged"

MAX_BOTTLE_ML = 500

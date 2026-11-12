# Baby Daybook for Home Assistant

Log diapers, sleep and breastfeeding in the [Baby Daybook](https://play.google.com/store/apps/details?id=com.drillyapps.babydaybook)
app from Home Assistant: a button on the wall, a voice command, an automation. The app on your
phones stays exactly as it is; Home Assistant simply behaves like one more device signed in to
your account, and everything syncs both ways.

This is an unofficial project, not affiliated with Baby Daybook or DrillyApps.

## How it works

```
IKEA button / voice / automation
            │
   Home Assistant entities            custom_components/babydaybook  (Python, runs inside
            │                         Home Assistant itself: no container, no add-on, no phone)
            ▼
   HTTPS to Google Firebase  ◄────►  Baby Daybook's own cloud  ◄────►  your phones
```

Baby Daybook keeps its data in Google Firebase. The integration makes the same three kinds of
HTTPS calls the Android app makes, and no others:

| Host | Used for |
|---|---|
| `identitytoolkit.googleapis.com` | signing in once with e-mail and password |
| `securetoken.googleapis.com` | renewing the session about once an hour |
| `firestore.googleapis.com` | reading and writing activity records |

No third-party server is involved and the only dependency is `aiohttp`, which Home Assistant
already ships. Your password is used once and never stored; Home Assistant keeps only the session
token, in its own config-entry storage.

To notice what you log on the phones, the integration asks every 30 seconds for records changed
since the last check (normally an empty answer), and reloads the 50 newest records every
15 minutes as a safety net. Before starting or stopping a timer it checks again, so a sleep started
on a phone seconds earlier is stopped rather than duplicated.

## What you get

One device per baby, with entities that come with fitting icons:

| Entity | What it does |
|---|---|
| `button` Diaper wet / Diaper dirty / Diaper wet and dirty | Records a diaper change now |
| `switch` Sleep | On = sleep timer running. Turning it on or off starts or stops it |
| `switch` Breastfeeding left / right | On = feeding on that side. Turning the other side on switches side within the same session |
| `sensor` Last diaper change / Last sleep / Last breastfeeding | When it last happened, with details as attributes |

Because these are ordinary entities they work with voice assistants, dashboards and any
automation trigger without custom services. Example for a two-button IKEA switch:

```yaml
automation:
  - alias: "Diaper button"
    triggers:
      - trigger: state
        entity_id: event.diaper_button_action
    actions:
      - action: button.press
        target:
          entity_id: >-
            {{ 'button.robin_diaper_wet' if trigger.to_state.attributes.event_type == 'on'
               else 'button.robin_diaper_dirty' }}
```

## Logging in the past, corrections and undo

Six actions cover "it happened a while ago" and "that was wrong". Each exists as a service (for
automations) and as a voice intent (for AI conversation agents):

| Service | What it does |
|---|---|
| `babydaybook.log_diaper` | Diaper change, `kind`: `wet`, `dirty` or `mixed` |
| `babydaybook.fell_asleep` | Starts the sleep. If one is already running, a given time moves its start |
| `babydaybook.woke_up` | Ends the sleep. If it was already ended, a given time moves its end |
| `babydaybook.start_breastfeeding` | Starts on `side` `left` or `right`, or switches to it |
| `babydaybook.stop_breastfeeding` | Ends the feeding. If it was already ended, a given time moves its end |
| `babydaybook.delete_last` | Deletes the newest `diaper`, `sleep` or `breastfeeding` entry |

All but `delete_last` accept `minutes_ago` or `time` (a time of day means its most recent
occurrence); leave both out for "now". `baby` is the baby's name, in any case and without emoji,
and is only needed when the account has more than one.

```yaml
action: babydaybook.woke_up
data:
  baby: Robin
  minutes_ago: 10
```

Impossible requests are refused with an explanation rather than written: a time in the future, a
wake-up before the sleep started, or a "woke up" that would move the end of a finished sleep by
more than three hours.

### Voice

The same actions are registered as intents, which Home Assistant offers to AI conversation agents
as tools. With such an agent, natural phrases work in any language it speaks: "Robin woke up ten
minutes ago", "wet diaper for Robin at half past two", "delete Robin's last diaper". Nothing has to
be exposed or configured per action. The built-in sentence-matching agent does not understand
these phrases; with it, use the entities ("turn on Robin sleep").

## Install

1. In HACS, add this repository as a custom repository of type *Integration* and install
   **Baby Daybook** (or copy `custom_components/babydaybook` into your Home Assistant `config`
   folder), then restart Home Assistant.
2. Go to *Settings → Devices & services → Add integration → Baby Daybook*.
3. Sign in:
   - **Account with e-mail and password:** choose *Sign in*.
   - **Account that uses Google sign-in:** choose *e-mail me a link to set a password*. Baby
     Daybook sends a message to your Google address; open the link, choose a password, then
     continue in Home Assistant. This adds a password to the same account. You keep using Google
     sign-in in the app, though the phones may ask you to sign in once more afterwards.

## Status and limits

- Sign-in, reading and writing have been confirmed against a real account. The automated tests
  run inside Home Assistant against an in-memory imitation of Firebase.
- It can break. It identifies itself to Google as the Android app (version 7.0.8) and relies on
  the app's current data layout. An app update or a change on the developer's side can stop it
  working, and it is outside the app's terms of service.
- Updates from the phones arrive within about 30 seconds, not instantly.
- Only diapers, sleep and breastfeeding are covered. Bottles, medicine and the rest are a small
  addition each.

## Where the protocol knowledge comes from

`custom_components/babydaybook/api.py` is an independent Python port of the small part needed
here of [kamilio/baby-daybook-sdk](https://github.com/kamilio/baby-daybook-sdk) (MIT). The relevant
upstream sources are copied, unmodified and unexecuted, into `vendor/baby-daybook-sdk/` at a
pinned commit, so this project keeps working as a reference even if that repository disappears.
See `vendor/baby-daybook-sdk/NOTICE.md`.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

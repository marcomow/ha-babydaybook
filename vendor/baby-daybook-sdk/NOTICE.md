# Vendored reference: kamilio/baby-daybook-sdk

A partial, unmodified snapshot of the unofficial Baby Daybook SDK, kept here so
this project does not depend on that repository staying online.

- Source: https://github.com/kamilio/baby-daybook-sdk
- Commit: `ffcce56fbb6da62116c35c89e23efb9febe1b86d` (2026-08-31)
- Author: Kamil Jopek
- License: MIT, as declared in the upstream `package.json` (upstream ships no
  separate license file)

Only the files that describe the wire protocol used by this integration are
copied: authentication, the Firestore REST layer, document paths and the
activity-record logic. Nothing in this folder is executed. The Home Assistant
integration in `custom_components/babydaybook/api.py` is an independent Python
port of the small subset it needs.

Left out on purpose: the CLI, the MCP servers, the hosted OAuth relay, PDF
reports, fonts, statistics, backups and the Garmin relay.

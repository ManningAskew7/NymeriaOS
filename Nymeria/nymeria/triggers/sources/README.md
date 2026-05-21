# sources/

Event trigger source implementations. Each source watches an external system and fires agent actions.

## Start here

`base.py`  -  `TriggerSource` interface that all sources implement.

## Contents

- `base.py`  -  abstract base class
- `webhook_source.py`  -  inbound webhook triggers
- `rss_source.py`  -  RSS feed polling
- `http_poll_source.py`  -  generic HTTP endpoint polling
- `outlook_email_source.py`  -  Outlook email triggers
- `slack_source.py`  -  Slack event triggers
- `teams_source.py`  -  Microsoft Teams triggers

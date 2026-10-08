# zendesk-cxas-integration

A website chat answered first by a Google Customer Experience Agent Studio (CXAS) agent, with escalation to human agents in Zendesk.

- `docs/architecture/` explains why this design was chosen and what the alternatives were.
- `docs/website-widget.md` explains how to add the chat to a website. `web/index.html` is a test page.
- `bridge/` is the Cloud Run service that connects Zendesk messaging (Sunshine Conversations) to CXAS. See [bridge/README.md](bridge/README.md).

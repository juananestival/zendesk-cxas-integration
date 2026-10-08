# Adding the chat to a website

The chat button on the website is the Zendesk Web Widget (messaging). The
customer talks to the CXAS agent through the bridge in `bridge/`, and moves to
a human agent in the same widget when CXAS escalates. Nothing on the website
talks to Google Cloud directly.

## 1. Add the snippet

Paste this just before `</body>` on every page that should show the chat
button. This is the snippet for the **d3v-juanan** developer account:

```html
<!-- Start of d3v-juanan Zendesk Widget script -->
<script id="ze-snippet" src="https://static.zdassets.com/ekr/snippet.js?key=3e1667ba-8ac0-4d35-903f-77c088989f4b"> </script>
<!-- End of d3v-juanan Zendesk Widget script -->
```

The key is the Web Widget channel key. It is public by design, because every
visitor's browser loads it. For another Zendesk account or brand, copy that
account's snippet from Admin Center › Channels › Messaging and social ›
Messaging › your Web Widget › Installation.

Messaging only shows the widget on domains you allow. In the Web Widget
settings, add the site's domain under the allowed domains (and `localhost` for
local tests), if your account restricts them.

## 2. Optional: control it from the page

The snippet exposes the `zE` messenger API. Common calls:

```html
<script>
  // Open the chat from your own button instead of the launcher.
  document.getElementById("chat-button").addEventListener("click", () => {
    zE("messenger", "open");
  });

  // Signed-in customers: link the chat to their Zendesk user, so web and app
  // share one history and the ticket has the right requester. The JWT is
  // signed by your backend with the messaging JWT signing key.
  zE("messenger", "loginUser", (callback) => {
    fetch("/api/zendesk-jwt").then((r) => r.text()).then(callback);
  });

  // Page context for the ticket, e.g. a custom conversation field.
  zE("messenger:set", "conversationFields", [{ id: "TICKET_FIELD_ID", value: "booking-page" }]);
</script>
```

## 3. Try it locally

`web/index.html` is a test page with the snippet and an "Open chat" button:

```bash
cd web && python3 -m http.server 8000
# open http://localhost:8000
```

What a working setup looks like:
1. The launcher appears, and the first message is answered by the CXAS agent
   (through the bridge on Cloud Run).
2. Asking for a human makes CXAS call `end_session(session_escalated=True)`.
   The bridge then passes control to agents, and a ticket with the bot
   transcript appears in the Agent Workspace.
3. The agent's replies arrive in the same widget.

If the widget answers with Zendesk's own bot instead, the switchboard default
responder isn't the CXAS bot yet. Run `bridge/script/setup_switchboard.sh`.

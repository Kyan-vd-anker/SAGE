# Connect calendars to Sage

Sage supports two connection methods. For simply viewing your agenda, use the published ICS link: it needs no Azure subscription, app registration, or Microsoft sign-in in Sage. Microsoft Graph remains available if you also want Sage to create events after your approval.

## Option 1: Published ICS links (read-only)

1. In your calendar provider, find its publish, share, or export option for an **ICS/iCalendar** feed. For Outlook on the web, open **Settings → Calendar → Shared calendars → Publish a calendar**, choose a calendar and permission level, then select **Publish**.
2. Copy the calendar's HTTPS ICS link or iPhone `webcal://` subscription link and paste it into **Daily briefing → Add an ICS calendar** in Sage. Sage securely converts `webcal://` links to HTTPS before fetching.
3. Repeat for each calendar you want to include. Sage supports up to 20 feeds, and each can be removed separately.

Sage fetches every connected feed when it displays or uses the agenda. ICS feeds are read-only and take precedence over Microsoft Graph for reading events. Sage accepts HTTPS and iPhone `webcal://` feeds from any provider, but only fetches hosts that resolve exclusively to public IP addresses. Each feed is limited to 2 MB. Published links are bearer secrets: anyone who obtains one may view the calendar details allowed by its publishing permissions. Sage stores links locally (protected with Windows DPAPI on Windows, or local file permissions elsewhere), never displays them again, and lets you remove feeds individually. You can also unpublish a feed with its provider to revoke the link.

To create Outlook events, connect Microsoft Graph as well. Calendar publishing may be disabled by your organization, and availability can depend on the account type.

## Option 2: Microsoft Graph (optional, supports event creation)

### Register an app

1. Open [Microsoft Entra app registrations](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) and create a registration.
2. Choose the account type that matches your Microsoft account. For an Outlook.com account, include personal Microsoft accounts. For a work or school account, choose the applicable organization option. A multi-tenant registration can use `common`; personal accounts can use `consumers`.
3. Copy the **Application (client) ID**.
4. Open **Authentication** and enable **Allow public client flows** (sometimes shown as “Treat application as a public client”). Save.
5. Open **API permissions** and add Microsoft Graph **delegated** permissions `User.Read` and `Calendars.ReadWrite`. Your organization may require an administrator to approve consent.

This desktop device-code flow does not need a client secret. See Microsoft's [device-code flow documentation](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-acquire-token-device-code-flow).

### Connect in Sage

1. In Sage's **Daily briefing** page, find **Optional: connect Microsoft Graph**.
2. Paste the client ID. Leave tenant as `common` for a multi-account app, or use `consumers`, `organizations`, or your tenant ID/domain as appropriate.
3. Select **Connect Graph**. Sage displays a one-time code and opens Microsoft's device sign-in page.
4. Enter the code on Microsoft's page, sign in, and accept the requested permissions.

Graph can read upcoming events and create new ones after you approve the request in chat. Before creating an event, Sage checks for conflicts using Graph. Sage currently creates new events and does not edit or delete existing events. Microsoft Graph's delegated `Calendars.ReadWrite` permission grants wider calendar access than Sage's implemented actions; review the consent screen and your organization's policy before granting it. See Microsoft's [calendar view](https://learn.microsoft.com/en-us/graph/api/calendar-list-calendarview?view=graph-rest-1.0) and [create event](https://learn.microsoft.com/en-us/graph/api/calendar-post-events?view=graph-rest-1.0) API documentation.

Use **Disconnect Graph** to delete Sage's saved token. You can also revoke the app's consent in your Microsoft account or organization. Sage does not receive or store your Microsoft password.

## Troubleshooting

- **ICS link rejected:** use an HTTPS or `webcal://` ICS link without embedded credentials or a fragment.
- **ICS feed cannot be fetched:** confirm the provider still publishes the feed, that it resolves to public IP addresses, and that your network can reach it.
- **Invalid client / app not found:** verify the client ID and selected tenant/account type.
- **Public client flow disabled:** turn on the public-client option under Authentication and save the registration.
- **Need admin approval:** ask your Microsoft 365 administrator to approve the delegated Graph permissions.
- **Calendar access denied:** confirm `Calendars.ReadWrite` is a delegated Graph permission and that consent completed.
- **Sign-in code expired:** return to Daily briefing and start the Graph connection again.

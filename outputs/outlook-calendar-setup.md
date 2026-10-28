# Connect an Outlook calendar to Sage

Sage supports two connection methods. For simply viewing your agenda, use the published ICS link: it needs no Azure subscription, app registration, or Microsoft sign-in in Sage. Microsoft Graph remains available if you also want Sage to create events after your approval.

## Option 1: Published ICS link (read-only)

1. In Outlook on the web, open **Settings → Calendar → Shared calendars → Publish a calendar**.
2. Choose the calendar and the permission level you want to publish, then select **Publish**.
3. Copy the **ICS** link and paste it into **Daily briefing → Connect with an Outlook export link** in Sage.

Sage fetches the feed when it displays or uses the agenda. When an ICS feed is connected, it is the preferred source for reading events. The published link is a bearer secret: anyone who obtains it may view the calendar details allowed by the publishing permission. Sage stores the link locally (protected with Windows DPAPI on Windows, or local file permissions elsewhere) and only fetches links hosted by Outlook publishing domains. Use **Disconnect ICS feed** to remove it. You can also unpublish the feed in Outlook to revoke the link.

ICS is read-only. To create events in Outlook, connect Microsoft Graph as well. Outlook calendar publishing may be disabled by your organization, and availability can depend on the account type.

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

- **ICS link rejected:** use the HTTPS ICS publishing link copied from Outlook; Sage accepts `outlook.live.com`, `outlook.office365.com`, and `outlook.office.com` hosts.
- **ICS feed unavailable:** confirm the feed is still published and that your account or organization allows calendar publishing.
- **Invalid client / app not found:** verify the client ID and selected tenant/account type.
- **Public client flow disabled:** turn on the public-client option under Authentication and save the registration.
- **Need admin approval:** ask your Microsoft 365 administrator to approve the delegated Graph permissions.
- **Calendar access denied:** confirm `Calendars.ReadWrite` is a delegated Graph permission and that consent completed.
- **Sign-in code expired:** return to Daily briefing and start the Graph connection again.

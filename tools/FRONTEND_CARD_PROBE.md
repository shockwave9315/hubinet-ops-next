# Android card delivery probe

For the outstanding 2026.9.1.19 card-loading failure on the app's external
mTLS URL. This is a standalone test artifact, not an integration runtime fix.

Copy `frontend-card-probe.html` to the Home Assistant configuration directory
as `www/hubinet-card-probe.html`. HA then serves it at
`/local/hubinet-card-probe.html`. If `www` did not exist when HA started,
restart HA after creating it so the native `/local` static route is available.

Add a built-in **Webpage / Strona internetowa** card to any dashboard:

```yaml
type: iframe
title: Hubinet — test ładowania
url: /local/hubinet-card-probe.html
aspect_ratio: "180%"
```

The relative URL follows the HA origin selected by the app. Open the dashboard
in the Android app with VPN disconnected, choose **Powtórz test** after the
missing VM card appears, then **Skopiuj wynik**. Repeat with VPN connected.
These two reports show the actual external/local request and registration
states in the same WebView. Using the other, login-protected browser domain
does not establish the app domain's behavior.
If automatic copying is unavailable, the page selects a read-only text field;
copy that complete JSON using the phone's normal text-selection menu.

The page reads the parent frontend's registration/picker state and HA version,
then fetches four same-origin static resources. It reports HTTP status, MIME,
redirects, Cloudflare Ray ID when present, and SHA-256 against this repository's
2026.9.1.19 source. A changed digest alone does not establish the cause.
The main module's readiness wait/timeout markers are also reported.

The probe never imports or executes the downloaded card code, reads auth
storage, or calls HA actions. A successful diagnostic fetch cannot repair
the parent page's missing registrations. Running standalone outside an HA
iframe still checks HTTP delivery, but cannot read the parent app registry.

Delete the temporary dashboard card and `www/hubinet-card-probe.html` when
the investigation is finished. The report contains domain, HA/WebView version,
and resource diagnostics, without auth tokens or private certificates.

Runtime validation used HA Container 2026.9.4 with its native Webpage card,
an HTTPS proxy requiring a test client certificate, and Chromium mobile
emulation of HA's V2 external-app bridge. Four cases passed: normal loading,
denied root module, denied guest module, and readable source with registration
suppressed. The probe preserved the parent registry and copied the complete
report in each case. This validates the diagnostic, not the owner's actual
Cloudflare or Android WebView behavior.

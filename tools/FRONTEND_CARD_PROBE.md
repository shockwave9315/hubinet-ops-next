# Android card delivery probe

For the outstanding 2026.9.1.19 card-loading failure on the app's external
mTLS URL. This is a standalone test artifact, not an integration runtime fix.

Copy `frontend-card-probe.html` to the Home Assistant configuration directory
as `www/hubinet-card-probe-v2.html`. HA then serves it at
`/local/hubinet-card-probe-v2.html`. If `www` did not exist when HA started,
restart HA after creating it so the native `/local` static route is available.

Add a built-in **Webpage / Strona internetowa** card to any dashboard:

```yaml
type: iframe
title: Hubinet — test ładowania
url: /local/hubinet-card-probe-v2.html
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

Probe version 2 also describes the script entries in the parent document and
in a separately fetched HTML response for the current HA panel path. It reports
Hubinet import directives, script types, frontend entrypoint paths, Rocket
Loader markers, the modern/legacy selection, and service-worker control.
The HTML request uses a unique query parameter and `cache: no-store`; service
workers and edge rules can still affect delivery, so it is a separate response,
not guaranteed origin-server evidence. CSP is summarized as flags. Raw HTML,
script bodies, and nonce values are never included in the report or executed.
`documentContainsHubinetModule` explicitly checks whether the literal root
module path appears in both the active document and the separate HTML response.

The owner's first reports showed four HTTP 200 JavaScript responses externally
with exact release-matching hashes, but no registered Hubinet elements, picker
types, or recorded parent requests. Locally, the same app/WebView registered all
five types and recorded all four module requests. Version 2 inspects the earlier
HTML import stage to distinguish missing directives from present, inactive ones.

The initial test never imports or executes downloaded code. A successful
diagnostic fetch cannot repair missing registrations. Resource timing has a
bounded buffer: an empty list alone does not prove that HA never requested a
module. A path appearing in HTML alone does not prove that its script ran;
the launcher type and import flags provide that context.

After collecting the initial result, choose **Uruchom moduł w HA**, then
**Skopiuj wynik**. The separate, owner-requested force test appends a module
script to the **parent HA document**, using the existing root module with
`?v=2026.9.1.19&probe=<time>`. It never imports it into the diagnostic iframe.
It monitors the parent's definitions/picker, the current registry's
`whenDefined("home-assistant")`, script load/error events, and relevant parent
JS errors/rejections for up to 20 seconds. A script `load` event alone does not
prove top-level-await completion. Errors have URL queries/nonces redacted.

The original result remains in the JSON, with an added `forceLoad` section.
The diagnostic keeps this one report in the parent window's memory so HA
rearranging cards and recreating the iframe cannot erase the before/after
comparison. No auth storage is read and no HA actions are called. The force
test changes registrations in this open frontend session; a full HA page
reload clears this diagnostic report and starts a new session. It does not
change HA configuration or create a permanent delivery fallback.

Recovery to five registered cards shows the current parent can execute the
module successfully; the original failure can still be missing/blocked HTML
startup or an earlier stalled/failed module. No recovery requires inspecting
delivery errors, exceptions, and timeout state. It does not, by itself, prove
the module code is defective. The current registry's readiness result cannot
establish whether a previous promise captured a different registry.

Running standalone outside an HA iframe still checks HTTP delivery, but cannot
read the parent app registry or enable the parent force test.

Delete the temporary dashboard card and `www/hubinet-card-probe-v2.html` when
the investigation is finished. The report contains domain, HA/WebView version,
and resource diagnostics, without auth tokens or private certificates.

Runtime validation used HA Container 2026.9.4 with its native Webpage card,
an HTTPS proxy requiring a test client certificate, and Chromium mobile
emulation of HA's V2 external-app bridge. Nine cases passed: normal loading,
denied root module, denied guest module, and readable source with registration
suppressed, plus missing HTML launchers and launchers marked with an inert
script type. The last two produce the owner's empty registration and parent
request history while the diagnostic fetches still see all four release files.
The force tests also cover a deliberately stalled initial top-level await
recovering to five definitions, a captured forced-module exception, and a
forced top-level await reaching its timeout. They verify registration in the
parent and not the iframe, no duplicate picker types, preserved initial JSON,
complete copying, and report recovery when the iframe is recreated. This
validates the diagnostic, not the owner's actual
Cloudflare or Android WebView behavior.

"""Atomic's web-rendered pages - Home and Discover.

Served over http:// to a WebView2 hosted inside the Qt window (see
helpers/webview2_host.py). The owner's instruction, 31 August 2026: these
pages scroll without Qt, and the rest of the app stays exactly as it is.

Measured before it was built - QtWebEngine, which this app already
bundles, draws Chromium into a texture Qt then composites and reached
151fps on his 240Hz panel. WebView2 presents through Edge's own
compositor. His verdict on the same pages was "better even than stremio".
"""

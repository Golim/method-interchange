# Source: Scrapling techniques at
#   https://github.com/D4Vinci/Scrapling/blob/main/scrapling/engines/constants.py
#   https://github.com/D4Vinci/Scrapling/blob/main/scrapling/engines/_browsers/_base.py
# Adapted for our Chromium + playwright + playwright-stealth stack (Option B).
#
#   IGNORE_DEFAULT_ARGS: Playwright defaults that reveal automation.
#   STEALTH_BROWSER_ARGS: --disable-blink-features=AutomationControlled.
#   STEALTH_BROWSER_ARGS: Scrapling STEALTH_ARGS bundle (~48 flags).
#   STEALTH_BROWSER_ARGS: --fingerprinting-canvas-image-data-noise.
#   STEALTH_BROWSER_ARGS: WebRTC local-IP leak prevention.
#   STEALTH_CONTEXT_OPTIONS: color_scheme, device_scale_factor (creepjs).
#   STEALTH_CONTEXT_OPTIONS: service_workers="block" — needed so POST captures aren't suppressed.
#   STEALTH_CONTEXT_OPTIONS: viewport, screen, is_mobile, has_touch.
#   STEALTH_CONTEXT_OPTIONS: permissions (geolocation, notifications).
#   STEALTH_CONTEXT_OPTIONS: ignore_https_errors.
#
# Out of scope (per RESEARCH.md §"Key insight"):
#   S11 (header generation), S12/S13 (referer spoofing), S14 (persistent context),
#   S15/S16 (patchright patches — not portable). No JS-injection patches here —
#   those duplicate playwright-stealth.

from typing import Any, Dict, Tuple


# Remove Playwright default args that reveal automation.
IGNORE_DEFAULT_ARGS: Tuple[str, ...] = (
    "--enable-automation",
    "--disable-popup-blocking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
)

# Additional launch args for stealth hardening.
STEALTH_BROWSER_ARGS: Tuple[str, ...] = (
    # navigator.webdriver Blink feature
    "--disable-blink-features=AutomationControlled",
    # Scrapling STEALTH_ARGS bundle
    "--test-type",
    "--lang=en-US",
    "--accept-lang=en-US",
    "--mute-audio",
    "--disable-sync",
    "--hide-scrollbars",
    "--disable-logging",
    "--start-maximized",
    "--enable-async-dns",
    "--use-mock-keychain",
    "--disable-translate",
    "--disable-voice-input",
    "--window-position=0,0",
    "--disable-wake-on-wifi",
    "--ignore-gpu-blocklist",
    "--enable-tcp-fast-open",
    "--enable-web-bluetooth",
    "--disable-cloud-import",
    "--disable-print-preview",
    "--metrics-recording-only",
    "--disable-crash-reporter",
    "--disable-partial-raster",
    "--disable-gesture-typing",
    "--disable-checker-imaging",
    "--disable-prompt-on-repost",
    "--force-color-profile=srgb",
    "--font-render-hinting=none",
    "--aggressive-cache-discard",
    "--disable-cookie-encryption",
    "--disable-domain-reliability",
    "--disable-threaded-animation",
    "--disable-threaded-scrolling",
    "--enable-simple-cache-backend",
    "--disable-background-networking",
    "--enable-surface-synchronization",
    "--disable-image-animation-resync",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--prerender-from-omnibox=disabled",
    "--safebrowsing-disable-auto-update",
    "--disable-offer-upload-credit-cards",
    "--disable-background-timer-throttling",
    "--disable-new-content-rendering-timeout",
    "--run-all-compositor-stages-before-draw",
    "--disable-client-side-phishing-detection",
    "--disable-backgrounding-occluded-windows",
    "--disable-layer-tree-host-memory-pressure",
    "--autoplay-policy=user-gesture-required",
    "--disable-offer-store-unmasked-wallet-cards",
    "--disable-component-extensions-with-background-pages",
    "--enable-features=NetworkService,NetworkServiceInProcess,TrustTokens,TrustTokensAlwaysAllowIssuance",
    "--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4",
    "--disable-features=AudioServiceOutOfProcess,TranslateUI,BlinkGenPropertyTrees",
    # Canvas fingerprint noise
    "--fingerprinting-canvas-image-data-noise",
    # WebRTC local-IP leak prevention
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--force-webrtc-ip-handling-policy",
)

# Context options for new_context() call.
# service_workers="block"
# (color_scheme="dark", device_scale_factor=2) is the creepjs bypass.
STEALTH_CONTEXT_OPTIONS: Dict[str, Any] = {
    "color_scheme": "dark",
    "device_scale_factor": 2,
    "service_workers": "block",
    "viewport": {"width": 1920, "height": 1080},
    "screen": {"width": 1920, "height": 1080},
    "is_mobile": False,
    "has_touch": False,
    "ignore_https_errors": True,
    "permissions": ["geolocation", "notifications"],
}


def merged_browser_args(base_args: list[str]) -> list[str]:
    """Return launch args = caller's base args + stealth additions (deduped, order preserved).

    :param base_args: Existing ``config.browser_args`` list from YAML (e.g.,
        ``["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]``).
    :return: List suitable to pass as ``chromium.launch(args=...)``.
    """
    seen = set()
    merged: list[str] = []
    for arg in list(base_args) + list(STEALTH_BROWSER_ARGS):
        if arg not in seen:
            merged.append(arg)
            seen.add(arg)
    return merged

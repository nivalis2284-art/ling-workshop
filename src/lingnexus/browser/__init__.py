"""Parent integration: one BrowserService per async lifespan, router via state.browser.

Only ActionInput/submit and read-only state belong in the model tool surface.
configure_origins, decide, open_login and finish_login are trusted owner operations.
Chrome is lazy-launched headless; no model-controlled profile path or headed option.
"""

from lingnexus.browser.policy import ActionInput, OriginGrant
from lingnexus.browser.service import BrowserService

__all__ = ["ActionInput", "BrowserService", "OriginGrant"]

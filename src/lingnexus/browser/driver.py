"""Async Playwright only: isolated persistent profiles and guarded DOM operations.

Official references consulted before implementation:
https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context
https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route
https://playwright.dev/python/docs/actionability
https://playwright.dev/python/docs/api/class-cdpsession
https://chromedevtools.github.io/devtools-protocol/tot/Fetch/
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, ElementHandle, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from lingnexus.browser.policy import authorize
from lingnexus.browser.store import digest
from lingnexus.common.errors import Conflict, PermissionDenied
from lingnexus.common.types import new_id

# Installed before site scripts. No keyboard/submit/evaluate method is exposed to models.
# Network writes are additionally gated in Python, independently of this DOM guard.
INIT_SCRIPT = r"""(() => {
    const nonce = [...crypto.getRandomValues(new Uint32Array(4))].join('-');
    let revision = 0, manual = false, armed = null, delivered = false;
    const observer = new MutationObserver(records => { if (records.length) revision++; });
    observer.observe(document, {subtree:true, childList:true, attributes:true, characterData:true});
    document.addEventListener('input', () => revision++, true);
    const fields = root => {
        const selector = 'input,textarea,select,button,[contenteditable=true]';
        const nodes = root instanceof HTMLFormElement
            ? [...root.elements].filter(e => e.matches(selector))
            : [...root.querySelectorAll(selector)];
        if (nodes.length > 200) throw new Error('Form exceeds supported control budget');
        return nodes.map(e => {
            const value = e.isContentEditable ? e.innerText : e.value;
            if (value.length > 20000) throw new Error('Control exceeds supported value budget');
            return {tag:e.tagName, id:e.id, name:e.name || '', type:e.type || '', value,
                selected:e.tagName === 'SELECT' ? [...e.selectedOptions].map(o => o.value) : null,
                checked:!!e.checked, disabled:!!e.disabled,
                secret:e.type === 'password' || e.type === 'hidden'};
        });
    };
    const base = () => {
        if (observer.takeRecords().length) revision++;
        return {nonce, revision, url:location.href, controls:fields(document)};
    };
    const snapshot = e => {
        const form = e.form || e.closest('form');
        const raw = {page:base(), target:e.outerHTML.slice(0,16384),
            form:form ? {
                action:e.hasAttribute('formaction') ? e.formAction : form.action,
                method:e.hasAttribute('formmethod') ? e.formMethod : form.method,
                target:e.hasAttribute('formtarget') ? e.formTarget : form.target,
                fields:fields(form)} : null};
        const preview = {target:{tag:e.tagName, type:e.type || '', name:e.name || '',
                text:(e.innerText || e.getAttribute('aria-label') || '').slice(0,500),
                href:e.href || null},
            form:raw.form ? {...raw.form, fields:raw.form.fields.map(f =>
                ({...f, value:f.secret ? '[redacted]' : f.value}))} : null};
        return {raw, preview};
    };
    const block = event => { event.preventDefault(); event.stopImmediatePropagation(); };
    document.addEventListener('click', event => {
        if (manual) return;
        if (!armed || delivered || !armed.element.contains(event.target) ||
                JSON.stringify(snapshot(armed.element).raw) !== armed.expected) {
            block(event); return;
        }
        delivered = true;
    }, true);
    document.addEventListener('submit', event => {
        if (!manual && !(armed && delivered)) block(event);
    }, true);
    for (const name of ['submit','requestSubmit']) {
        const original = HTMLFormElement.prototype[name];
        HTMLFormElement.prototype[name] = function(...args) {
            if (manual || (armed && delivered)) return original.apply(this,args);
        };
    }
    Object.defineProperty(window, '__lingBrowser', {value:{
        base, snapshot,
        manual: value => { manual = value; },
        arm: (element, expected) => { armed = {element,expected}; delivered = false; },
        disarm: () => { const result = delivered; armed = null; delivered = false; return result; }
    }});
})();"""


@dataclass
class Session:
    context: BrowserContext
    page: Page
    session_id: str
    manual: bool = False
    navigating: bool = False
    effect: bool = False
    blocked: bool = False


class BrowserDriver:
    def __init__(self, store, data_dir: Path, executable_path: Path | None = None):
        self.store = store
        self.data_dir = Path(data_dir).resolve()
        self.profile_root = self.data_dir / "browser" / "automation-profiles"
        self.executable_path = executable_path
        self.sessions: dict[str, Session] = {}
        self.playwright: Playwright | None = None
        # Reject accidentally passing a normal Chrome profile as app data, including links.
        local = os.environ.get("LOCALAPPDATA")
        defaults = [
            Path.home() / ".config/google-chrome",
            Path.home() / "Library/Application Support/Google/Chrome",
        ]
        if local:
            defaults.append(Path(local) / "Google/Chrome/User Data")
        if any(
            self.data_dir.is_relative_to(p.resolve()) or p.resolve().is_relative_to(self.data_dir)
            for p in defaults
        ):
            raise ValueError("Browser data must use a dedicated application directory")

    def profile(self, agent_id: str) -> Path:
        key = hashlib.sha256(agent_id.encode()).hexdigest()
        path = self.profile_root / key
        if not path.resolve().is_relative_to(self.data_dir):
            raise PermissionDenied("Automation profile must remain inside app data")
        path.mkdir(parents=True, exist_ok=True)
        # A reparse point below app data must not redirect to any normal browser profile.
        resolved = path.resolve()
        if resolved != path:
            raise PermissionDenied("Automation profile links are not supported")
        return path

    async def session(self, agent_id: str, *, manual: bool = False) -> Session:
        if agent_id in self.sessions:
            session = self.sessions[agent_id]
            if session.manual != manual:
                raise PermissionDenied("Owner manual login is active; automation is paused")
            return session
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        context = await self.playwright.chromium.launch_persistent_context(
            self.profile(agent_id),
            headless=not manual,
            accept_downloads=False,
            service_workers="block",
            executable_path=self.executable_path,
            channel=None if self.executable_path else "chrome",
            args=[
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-extensions",
                "--no-default-browser-check",
            ],
        )
        try:
            context.set_default_timeout(5000)
            context.set_default_navigation_timeout(10000)
            await context.add_init_script(
                script=INIT_SCRIPT.replace(
                    "manual = false", "manual = true" if manual else "manual = false"
                )
            )
            page = context.pages[0] if context.pages else await context.new_page()
            session = Session(context, page, new_id(), manual=manual)

            async def route_request(route):
                try:
                    await authorize(self.store.origins(agent_id), route.request.url)
                    # This adapter owns one main-page form tab. Block other targets rather
                    # than leave popup/worker/OOP-frame redirects outside the response guard.
                    if route.request.frame != session.page.main_frame:
                        raise PermissionDenied("Only the owned main browser tab is supported")
                    if route.request.method not in {"GET", "HEAD"} and not (
                        session.manual or session.effect
                    ):
                        raise PermissionDenied("Browser writes require an approved click")
                    if route.request.is_navigation_request() and not (
                        session.manual or session.effect or session.navigating
                    ):
                        raise PermissionDenied("Automatic navigation is not authorized")
                except (ValueError, PermissionDenied, PlaywrightError):
                    session.blocked = True
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()

            await context.route("**/*", route_request)
            # Chromium can follow redirects without invoking Playwright's request route
            # again. Pause every response BEFORE following its Location, including every
            # intermediate response in a redirect chain, not just the final page URL.
            cdp = await context.new_cdp_session(page)

            async def response_guard(event):
                request_id = event["requestId"]
                try:
                    url = event["request"]["url"]
                    state = self.store.origins(agent_id)
                    await authorize(state, url)
                    if 300 <= event.get("responseStatusCode", 0) < 400:
                        location = next(
                            (
                                h["value"]
                                for h in event.get("responseHeaders", [])
                                if h["name"].lower() == "location"
                            ),
                            None,
                        )
                        if location is not None:
                            await authorize(state, urljoin(url, location))
                    await cdp.send("Fetch.continueResponse", {"requestId": request_id})
                except (ValueError, PermissionDenied):
                    session.blocked = True
                    try:
                        await cdp.send(
                            "Fetch.failRequest",
                            {"requestId": request_id, "errorReason": "BlockedByClient"},
                        )
                    except PlaywrightError:
                        pass  # Context shutdown also aborts paused requests.
                except PlaywrightError:
                    session.blocked = True
                    if not page.is_closed():
                        await page.close()  # A broken response guard must fail closed.

            cdp.on("Fetch.requestPaused", response_guard)
            await cdp.send(
                "Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]}
            )
            # Websocket sends have no per-message approval contract; block them entirely.
            await context.route_web_socket("**/*", lambda ws: ws.close())
            page.on("dialog", lambda dialog: dialog.dismiss())
            self.sessions[agent_id] = session
            return session
        except BaseException:
            await context.close()
            raise

    def existing(self, agent_id: str) -> Session:
        session = self.sessions.get(agent_id)
        if session is None or session.page.is_closed():
            raise Conflict("Browser page is no longer available")
        if session.manual:
            raise PermissionDenied("Owner manual login is active; automation is paused")
        return session

    async def page_state(self, agent_id: str) -> dict | None:
        session = self.sessions.get(agent_id)
        if session is None or session.page.is_closed():
            return None
        if session.manual:
            return {"manual_login": True, "session_id": session.session_id}
        if session.page.url == "about:blank":
            return None
        await authorize(self.store.origins(agent_id), session.page.url)
        base = await session.page.evaluate("() => window.__lingBrowser.base()")
        return {
            "url": base["url"],
            "page_version": digest(base),
            "session_id": session.session_id,
            "manual_login": False,
        }

    async def require_page(self, agent_id: str, version: str, revision: int) -> Session:
        session = self.existing(agent_id)
        await authorize(self.store.origins(agent_id), session.page.url, revision)
        state = await self.page_state(agent_id)
        if state is None or state["page_version"] != version:
            raise Conflict("Browser page changed; inspect it again")
        return session

    async def goto(self, agent_id: str, url: str, revision: int) -> dict:
        await authorize(self.store.origins(agent_id), url, revision)
        session = await self.session(agent_id)
        session.navigating, session.blocked = True, False
        try:
            await session.page.goto(url, wait_until="domcontentloaded")
        finally:
            session.navigating = False
        await authorize(self.store.origins(agent_id), session.page.url, revision)
        return await self.page_state(agent_id) or {}

    async def target(self, session: Session, selector: str) -> ElementHandle:
        locator = session.page.locator(selector)
        if await locator.count() != 1:
            raise ValueError("Browser selector must identify exactly one element")
        handle = await locator.element_handle()
        if handle is None:
            raise Conflict("Browser target disappeared")
        if await handle.owner_frame() != session.page.main_frame:
            raise PermissionDenied("Browser controls must belong to the main page")
        return handle

    async def read(self, agent_id: str, version: str, revision: int, max_chars: int) -> dict:
        session = await self.require_page(agent_id, version, revision)
        text = await session.page.evaluate(
            '(limit) => (document.body?.innerText || "").slice(0,limit)', max_chars + 1
        )
        # A navigation during read must not return text from an ungranted document.
        await self.require_page(agent_id, version, revision)
        return {
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
            **(await self.page_state(agent_id) or {}),
        }

    async def fill(
        self, agent_id: str, version: str, revision: int, selector: str, value: str
    ) -> dict:
        session = await self.require_page(agent_id, version, revision)
        target = await self.target(session, selector)
        supported = await target.evaluate("""e => e.tagName === 'TEXTAREA' || e.isContentEditable ||
            (e.tagName === 'INPUT' && ['text','search','email','tel','url','number'].includes(e.type))""")
        if not supported:
            raise PermissionDenied(
                "Only text controls can be prepared; use owner login for passwords"
            )
        await self.require_page(agent_id, version, revision)
        await target.fill(value)
        await authorize(self.store.origins(agent_id), session.page.url, revision)
        return {"filled": True, "submitted": False, **(await self.page_state(agent_id) or {})}

    async def snapshot(self, agent_id: str, version: str, revision: int, selector: str) -> dict:
        session = await self.require_page(agent_id, version, revision)
        target = await self.target(session, selector)
        captured = await target.evaluate("e => window.__lingBrowser.snapshot(e)")
        if digest(captured["raw"]["page"]) != version:
            raise Conflict("Browser page changed while preparing the intent")
        form = captured["preview"]["form"]
        href = captured["preview"]["target"]["href"]
        if form:
            await authorize(self.store.origins(agent_id), form["action"], revision)
        if href:
            await authorize(self.store.origins(agent_id), href, revision)
        return {
            "session_id": session.session_id,
            "page_version": version,
            "origin_revision": revision,
            "url": session.page.url,
            "selector": selector,
            "snapshot_hash": digest(captured["raw"]),
            **captured["preview"],
        }

    async def click(self, agent_id: str, intent: dict) -> dict:
        session = await self.require_page(
            agent_id, intent["page_version"], intent["origin_revision"]
        )
        target = await self.target(session, intent["selector"])
        captured = await target.evaluate("e => window.__lingBrowser.snapshot(e)")
        if digest(captured["raw"]) != intent["snapshot_hash"]:
            raise Conflict("Browser target or form changed")
        session.blocked = False
        # Guard again at event dispatch, after Playwright's visibility/actionability waits.
        await target.evaluate(
            "(e, raw) => window.__lingBrowser.arm(e, JSON.stringify(raw))", captured["raw"]
        )
        session.effect = True
        dispatched = False
        try:
            await target.click()
            # Navigation creates a new document; an actual navigation also proves dispatch.
            if session.page.url != intent["url"]:
                dispatched = True
            else:
                dispatched = await session.page.evaluate("() => window.__lingBrowser.disarm()")
        finally:
            session.effect = False
            if not session.page.is_closed():
                await session.page.evaluate("() => window.__lingBrowser?.disarm()")
        if not dispatched or session.blocked:
            raise Conflict("Click outcome requires review")
        # completed means browser dispatch succeeded, not business/server confirmation.
        return {"clicked": True, "url": session.page.url, "business_outcome": "not_verified"}

    async def open_login(self, agent_id: str, url: str, revision: int) -> dict:
        await authorize(self.store.origins(agent_id), url, revision)
        await self.close_agent(agent_id)
        session = await self.session(agent_id, manual=True)
        await session.page.goto(url, wait_until="domcontentloaded")
        return {"manual_login": True, "url": session.page.url, "session_id": session.session_id}

    async def close_agent(self, agent_id: str) -> None:
        session = self.sessions.pop(agent_id, None)
        if session:
            await session.context.close()

    async def aclose(self) -> None:
        try:
            for agent_id in list(self.sessions):
                await self.close_agent(agent_id)
        finally:
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None

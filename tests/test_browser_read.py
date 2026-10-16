"""Real DOM navigation evidence remains bounded and cannot grant another origin."""

import asyncio
import sys
from pathlib import Path

import pytest

from lingnexus.browser.policy import ActionInput, OriginGrant
from lingnexus.browser.service import BrowserService
from lingnexus.common.errors import PermissionDenied

sys.path.insert(0, str(Path(__file__).absolute().parents[1] / "scripts"))
from evaluate_acceptance import CHROME, fixture_site


def test_browser_read_exposes_bounded_links_without_granting_access(app, agent_id, tmp_path):
    if not CHROME.is_file():
        pytest.skip("Real Chrome executable unavailable")

    async def check():
        with fixture_site() as (origin, _site):
            browser = BrowserService(app.database, tmp_path, executable_path=CHROME)
            await browser.start()
            try:
                await browser.configure_origins(agent_id, [OriginGrant(origin=origin, allow_local=True)],
                                                expected_revision=0)
                await browser.submit(agent_id, ActionInput(request_id="goto", action="goto",
                    origin_revision=1, url=origin + "/"))

                async def read(request):
                    state = await browser.state(agent_id)
                    return await browser.submit(agent_id, ActionInput(request_id=request, action="read",
                        origin_revision=1, page_version=state["page"]["page_version"]))

                first = await read("read-links")
                assert first["result"]["links"] == [{"text": "Workshop details", "url": origin + "/details"}]
                assert first["result"]["links_truncated"] is False
                page = browser.driver.sessions[agent_id].page
                await page.evaluate("""() => {
                    document.body.innerHTML = '<a href="https://ungranted.invalid/">Another origin</a>' +
                        '<a href="javascript:void(0)">Script</a><a href="http://user:secret@localhost/">Secret</a>' +
                        '<a href="http://%">Invalid</a><a style="display:none" href="/hidden">Hidden</a>' +
                        '<a style="visibility:hidden" href="/invisible">Invisible</a>';
                }""")
                safe = await read("read-filtered")
                assert safe["result"]["links"] == [{"text": "Another origin", "url": "https://ungranted.invalid/"}]
                with pytest.raises(PermissionDenied):
                    await browser.submit(agent_id, ActionInput(request_id="no-grant", action="goto",
                        origin_revision=1, url=safe["result"]["links"][0]["url"]))
                await page.evaluate("""() => {
                    document.body.innerHTML = Array.from({length:130}, (_,i) =>
                        '<a href="/item'+i+'">Item '+i+'</a>').join(' ');
                }""")
                bounded = await read("read-bounded")
                assert len(bounded["result"]["links"]) == 100
                assert bounded["result"]["links_truncated"] is True
            finally:
                await browser.aclose()

    asyncio.run(check())

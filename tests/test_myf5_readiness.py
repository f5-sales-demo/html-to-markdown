"""Exercise real Lightning DOM readiness and complete extraction in Chromium."""

import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from html_to_markdown.adapters.my_f5 import MyF5Adapter
from html_to_markdown.models import FetchResult

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser():
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        try:
            yield chromium
        finally:
            await chromium.close()


@pytest.mark.parametrize(
    "body,expected",
    [
        ("<h1>Cloud services</h1><h2>Answer</h2><p>Use port 8443.</p>", True),
        ("<h1>Cloud services</h1><h2>Answer</h2>", False),
        ("<h1>Cloud services</h1><h2>Answer</h2><p>Use port 8443.</p><h2>Procedure</h2>", False),
        (
            '<h1>Cloud services</h1><div class="article-text-block slds-rich-text-editor__output"><p>Use port 8443.</p></div>',
            True,
        ),
        (
            '<h1>Cloud services</h1><h2>Overview</h2><div class="article-text-block slds-rich-text-editor__output"><p>Use port 8443.</p></div>',
            True,
        ),
        (
            '<h1>Cloud services</h1><div class="article-text-block slds-rich-text-editor__output"></div><footer><p>Related content</p></footer>',
            False,
        ),
        ("<h1>Sign in</h1><p>Enter your account.</p>", False),
        (
            '<h1>Cloud services</h1><div aria-busy="true"><div class="article-text-block slds-rich-text-editor__output"><p>Use port 8443.</p></div></div>',
            False,
        ),
        (
            '<h1>Cloud services</h1><lightning-spinner></lightning-spinner><div class="article-text-block slds-rich-text-editor__output"><p>Use port 8443.</p></div>',
            False,
        ),
        (
            '<h1>Cloud services</h1><h2>Description</h2><div class="article-text-block slds-rich-text-editor__output"><h2>Configure the service</h2><p>Use port 8443.</p></div><h2>Cause</h2>',
            True,
        ),
        (
            '<h1>Cloud services</h1><h2>Description</h2><h2>Cause</h2><h2>Recommended Actions</h2><div class="article-text-block"><div class="slds-rich-text-editor__output"><p>Restart the service. Expect healthy origins within 30 seconds.</p></div></div>',
            True,
        ),
        (
            '<h1>Cloud services</h1><h2>Description</h2><div class="article-text-block slds-rich-text-editor__output"><nav><a href="/related">Related content</a></nav></div><footer><p>Promotions</p></footer>',
            False,
        ),
    ],
)
async def test_article_structure_readiness(browser, body, expected):
    page = await browser.new_page()
    try:
        await page.set_content("<article>" + body + "</article>")
        assert await page.evaluate(MyF5Adapter().readiness_script()) is expected
    finally:
        await page.close()


async def test_delayed_shadow_fields_wait_for_content_and_preserve_all_steps(browser):
    page = await browser.new_page()
    adapter = MyF5Adapter()
    try:
        await page.set_content(
            "<c-site-article-detail-container></c-site-article-detail-container>"
        )
        await page.evaluate("""() => {
          const root=document.querySelector('c-site-article-detail-container').attachShadow({mode:'open'});
          root.innerHTML='<article><h1>K000123456: Cloud services</h1><div class="article-text-block"><div aria-busy="true" class="slds-rich-text-editor__output"></div></div></article>';
        }""")
        assert await page.evaluate(adapter.readiness_script()) is False
        await page.evaluate("""() => {
          const field=document.querySelector('c-site-article-detail-container').shadowRoot.querySelector('.slds-rich-text-editor__output');
          setTimeout(() => { field.innerHTML='<p>Configure the origin pool before the load balancer.</p><p>Use port 8443. Expect healthy origins within 30 seconds.</p>'; field.removeAttribute('aria-busy'); }, 50);
        }""")
        await page.wait_for_function(adapter.readiness_script(), timeout=2000)
        html = await adapter.rendered_html(page)
        extracted = adapter.extract(
            FetchResult(
                url="https://my.f5.com/manage/s/article/K000123456",
                final_url="https://my.f5.com/manage/s/article/K000123456",
                status_code=200,
                html=html,
                headers={},
            )
        )
        assert "before the load balancer" in extracted.html
        assert "port 8443" in extracted.html and "within 30 seconds" in extracted.html
    finally:
        await page.close()


async def test_public_rich_text_login_instruction_is_not_an_authentication_wall():
    url = "https://my.f5.com/manage/s/article/K000123456"
    page = FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        html='<article><h1>Cloud services</h1><section><div class="article-text-block"><div class="slds-rich-text-editor__output"><p>Log in to configure the origin pool before the load balancer.</p></div></div></section></article>',
    )
    MyF5Adapter().classify_page(page)


async def test_empty_shadow_field_labels_preserve_useful_procedure_and_source_limits(browser):
    page = await browser.new_page()
    adapter = MyF5Adapter()
    try:
        await page.set_content(
            "<c-site-article-detail-container></c-site-article-detail-container>"
        )
        await page.evaluate("""() => {
          const root=document.querySelector('c-site-article-detail-container').attachShadow({mode:'open'});
          root.innerHTML='<article><h1>K000123456: Cloud services</h1><h2>Description</h2><h2>Cause</h2><h2>Recommended Actions</h2><div class="article-text-block"><div class="slds-rich-text-editor__output"><p>Configure the origin pool before the load balancer.</p><p>Use port 8443. Expect healthy origins within 30 seconds.</p></div></div></article>';
        }""")
        assert await page.evaluate(adapter.readiness_script()) is True
        html = await adapter.rendered_html(page)
        result = adapter.extract(
            FetchResult(
                url="https://my.f5.com/manage/s/article/K000123456",
                final_url="https://my.f5.com/manage/s/article/K000123456",
                status_code=200,
                html=html,
                headers={},
            )
        )
        assert "<h2>Description</h2>" in result.html and "<h2>Cause</h2>" in result.html
        assert "before the load balancer" in result.html
        assert "port 8443" in result.html and "within 30 seconds" in result.html
    finally:
        await page.close()

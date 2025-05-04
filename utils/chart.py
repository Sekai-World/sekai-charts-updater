import logging
from os import getcwd, path
from typing import List

import aiohttp
import sekaiworld.scores as scores
from playwright.async_api import async_playwright

logger = logging.getLogger("charts")


async def svg_to_png(svg_path: str, png_path: str):
    async with async_playwright() as p:
        # spawn a new browser instance
        browser = await p.firefox.launch(headless=True)
        
        # open svg file
        page = await browser.new_page()
        await page.goto(f"file:///{path.join(getcwd(), svg_path)}")
        await page.wait_for_selector("svg")
        
        # get real svg size
        svg_element = await page.query_selector("svg")
        bounding_box = await svg_element.bounding_box()
        
        # take screenshot
        await page.set_viewport_size({"width": int(bounding_box["width"]), "height": int(bounding_box["height"])})
        await page.screenshot(path=png_path, clip=bounding_box)
        
        # close the browser
        await browser.close()


async def render_chart(
    score_path: str, chart_path: str, music: dict, jacket: str
):
    # open the score from score_path and render it to a chart in chart_path
    score = scores.Score.open(score_path, encoding="utf-8")
    score.meta.title = music["title"]
    score.meta.jacket = jacket
    drawing = scores.Drawing(score)
    svg = drawing.svg()
    svg.saveas(chart_path)

    png_path = chart_path.replace(".svg", ".png")
    await svg_to_png(chart_path, png_path)


async def get_list(url: str) -> List[dict]:
    # use aiohttp to get the list from url
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()


def get_json_url(server: str, json_name: str) -> str:
    if server == "jp":
        return f"https://sekai-world.github.io/sekai-master-db-diff/{json_name}.json"
    else:
        return f"https://sekai-world.github.io/sekai-master-db-{server}-diff/{json_name}.json"

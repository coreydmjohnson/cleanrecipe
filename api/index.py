#!/usr/bin/env python3
"""
CleanRecipe API Server
Extracts recipe data from any URL via JSON-LD/schema.org → readability fallback
"""
import json
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from flask_cors import CORS

import os
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), '..', 'public')
app = Flask(__name__, static_folder=PUBLIC_DIR, static_url_path='')
CORS(app)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def clean_text(s):
    if not s:
        return ""
    return re.sub(r'\s+', ' ', str(s)).strip()


def parse_duration(iso_dur):
    """Convert ISO 8601 duration (PT1H30M) to readable string."""
    if not iso_dur:
        return ""
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', str(iso_dur))
    if not m:
        return str(iso_dur)
    h, mins, s = m.group(1), m.group(2), m.group(3)
    parts = []
    if h:   parts.append(f"{h}h")
    if mins: parts.append(f"{mins}m")
    if s:   parts.append(f"{s}s")
    return " ".join(parts) or str(iso_dur)


def extract_jsonld(soup):
    """Try to extract recipe from JSON-LD schema.org markup."""
    scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        # Handle @graph array
        if isinstance(data, dict) and "@graph" in data:
            data = data["@graph"]

        items = data if isinstance(data, list) else [data]

        for item in items:
            if not isinstance(item, dict):
                continue
            typ = item.get("@type", "")
            if isinstance(typ, list):
                typ = " ".join(typ)
            if "Recipe" not in typ:
                continue

            # Parse ingredients
            ingredients = item.get("recipeIngredient", [])
            if isinstance(ingredients, str):
                ingredients = [ingredients]

            # Parse steps
            steps = []
            raw_steps = item.get("recipeInstructions", [])
            if isinstance(raw_steps, str):
                steps = [clean_text(raw_steps)]
            elif isinstance(raw_steps, list):
                for s in raw_steps:
                    if isinstance(s, str):
                        steps.append(clean_text(s))
                    elif isinstance(s, dict):
                        text = s.get("text", s.get("name", ""))
                        if text:
                            steps.append(clean_text(text))

            # Yield/servings
            yield_raw = item.get("recipeYield", "") or item.get("yield", "")
            if isinstance(yield_raw, list):
                yield_raw = yield_raw[0] if yield_raw else ""

            # Image
            image = item.get("image", "")
            if isinstance(image, list):
                image = image[0] if image else ""
            if isinstance(image, dict):
                image = image.get("url", "")

            # Nutrition
            nutrition = item.get("nutrition", {})
            cal = ""
            if isinstance(nutrition, dict):
                cal = nutrition.get("calories", "")

            return {
                "title": clean_text(item.get("name", "")),
                "description": clean_text(item.get("description", "")),
                "image": str(image),
                "prep_time": parse_duration(item.get("prepTime", "")),
                "cook_time": parse_duration(item.get("cookTime", "")),
                "total_time": parse_duration(item.get("totalTime", "")),
                "servings": clean_text(str(yield_raw)),
                "ingredients": [clean_text(i) for i in ingredients if i],
                "steps": steps,
                "calories": clean_text(str(cal)) if cal else "",
                "source": "json-ld",
            }
    return None


def extract_heuristic(soup, url):
    """Fallback: try to scrape recipe data from HTML heuristically."""
    title = ""
    for sel in ["h1.recipe-title", "h1.entry-title", "h1.wprm-recipe-name",
                "h1", ".recipe-name", "[class*='recipe-title']"]:
        el = soup.select_one(sel)
        if el:
            title = clean_text(el.get_text())
            break

    if not title:
        og_title = soup.find("meta", property="og:title")
        if og_title:
            title = clean_text(og_title.get("content", ""))

    # Image
    image = ""
    og_img = soup.find("meta", property="og:image")
    if og_img:
        image = og_img.get("content", "")

    # Ingredients — look for list items in ingredient containers
    ingredients = []
    ing_containers = soup.select(
        "[class*='ingredient'], [id*='ingredient'], .wprm-recipe-ingredient, "
        ".tasty-recipe-ingredients li, .recipe-ingredients li"
    )
    if ing_containers:
        for el in ing_containers:
            t = clean_text(el.get_text())
            if t and len(t) > 2:
                ingredients.append(t)
    else:
        # Try any ul with 3+ short list items in the middle of the page
        for ul in soup.find_all("ul"):
            items = [clean_text(li.get_text()) for li in ul.find_all("li")]
            items = [i for i in items if 3 < len(i) < 200]
            if 3 <= len(items) <= 30:
                ingredients = items
                break

    # Steps
    steps = []
    step_containers = soup.select(
        "[class*='instruction'], [id*='instruction'], [class*='direction'], "
        ".wprm-recipe-instruction-text, .tasty-recipe-instructions li, .recipe-instructions li"
    )
    if step_containers:
        for el in step_containers:
            t = clean_text(el.get_text())
            if t and len(t) > 10:
                steps.append(t)
    else:
        for ol in soup.find_all("ol"):
            items = [clean_text(li.get_text()) for li in ol.find_all("li")]
            items = [i for i in items if len(i) > 10]
            if len(items) >= 2:
                steps = items
                break

    return {
        "title": title or urlparse(url).netloc,
        "description": "",
        "image": image,
        "prep_time": "",
        "cook_time": "",
        "total_time": "",
        "servings": "",
        "ingredients": ingredients,
        "steps": steps,
        "calories": "",
        "source": "heuristic",
    }


@app.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "url is required"}), 400

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"Could not fetch URL: {e}"}), 400

    soup = BeautifulSoup(resp.text, "html.parser")

    recipe = extract_jsonld(soup)
    if not recipe:
        recipe = extract_heuristic(soup, url)

    recipe["url"] = url
    recipe["domain"] = urlparse(url).netloc

    # Sanity check — must have at least a title and either ingredients or steps
    if not recipe.get("title"):
        return jsonify({"error": "Could not detect a recipe on this page"}), 422
    if not recipe.get("ingredients") and not recipe.get("steps"):
        return jsonify({"error": "Page found but no recipe content detected"}), 422

    return jsonify(recipe)


@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/")
def index():
    return app.send_static_file("index.html")


if __name__ == "__main__":
    app.run(port=5050, debug=False)

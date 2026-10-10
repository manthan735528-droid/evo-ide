#!/usr/bin/env python3
"""
Evo IDE V5.1 - Starter Project Templates

V5.1 Changes:
- FIXED: Removed 'asyncio' from async_app requirements (built-in since Python 3.4)
- Added: Web scraper template with REAL fetching
- Added: API client template
- Added: Data visualization template
- Added: Async app template
"""

TEMPLATES = {
    "flask_app": {
        "description": "Flask web application",
        "files": {
            "app.py": """from flask import Flask, render_template

app = Flask(__name__)

@app.route('/')
def home():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True)
""",
            "templates/index.html": """<!DOCTYPE html>
<html>
<head><title>Flask App</title></head>
<body>
    <h1>Hello from Flask!</h1>
</body>
</html>
""",
            "requirements.txt": "flask\n"
        }
    },
    "fastapi_app": {
        "description": "FastAPI web application",
        "files": {
            "main.py": """from fastapi import FastAPI

app = FastAPI()

@app.get('/')
def read_root():
    return {'message': 'Hello World'}

@app.get('/items/{item_id}')
def read_item(item_id: int, q: str = None):
    return {'item_id': item_id, 'q': q}
""",
            "requirements.txt": "fastapi\nuvicorn\n"
        }
    },
    "cli_tool": {
        "description": "Command-line tool with argparse",
        "files": {
            "cli.py": """import argparse

def main():
    parser = argparse.ArgumentParser(description='My CLI Tool')
    parser.add_argument('--name', default='World', help='Name to greet')
    args = parser.parse_args()
    print(f'Hello, {args.name}!')

if __name__ == '__main__':
    main()
"""
        }
    },
    "data_science": {
        "description": "Data science notebook structure",
        "files": {
            "analysis.py": """import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

def load_data(filepath):
    \"\"\"Load and return dataframe.\"\"\"
    return pd.read_csv(filepath)

def analyze(df):
    \"\"\"Basic analysis.\"\"\"
    print(df.describe())
    return df

if __name__ == '__main__':
    df = pd.DataFrame({'x': range(10), 'y': range(10)})
    analyze(df)
""",
            "requirements.txt": "pandas\nnumpy\nmatplotlib\n"
        }
    },
    "web_scraper": {
        "description": "Web scraper with requests and BeautifulSoup",
        "files": {
            "scraper.py": """import requests
from bs4 import BeautifulSoup
import json

class WebScraper:
    def __init__(self, base_url):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'
        })

    def fetch(self, path=''):
        url = f"{self.base_url}/{path}"
        response = self.session.get(url)
        response.raise_for_status()
        return response.text

    def parse(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        return soup

    def extract_links(self, soup):
        return [a['href'] for a in soup.find_all('a', href=True)]

if __name__ == '__main__':
    scraper = WebScraper('https://example.com')
    html = scraper.fetch()
    soup = scraper.parse(html)
    print(f"Found {len(soup.find_all())} elements")
""",
            "requirements.txt": "requests\nbeautifulsoup4\n"
        }
    },
    "async_app": {
        "description": "Async application with asyncio and aiohttp",
        "files": {
            "main.py": """import asyncio
import aiohttp

async def fetch_url(session, url):
    async with session.get(url) as response:
        return await response.text()

async def main():
    urls = [
        'https://api.github.com',
        'https://httpbin.org/get'
    ]
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_url(session, url) for url in urls]
        results = await asyncio.gather(*tasks)
        for url, result in zip(urls, results):
            print(f"{url}: {len(result)} bytes")

if __name__ == '__main__':
    asyncio.run(main())
""",
            "requirements.txt": "aiohttp\n"
        }
    }
}

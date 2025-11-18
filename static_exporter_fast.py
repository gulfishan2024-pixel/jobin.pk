import os
import asyncio
import aiohttp
import aiofiles
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urldefrag

START_URL = "http://localhost/wordpress1/"
OUTPUT_DIR = "static-build-fast"
DOMAIN = urlparse(START_URL).netloc

visited = set()
semaphore = asyncio.Semaphore(20)   # 20 requests in parallel
queue = asyncio.Queue()


def normalize(url):
    url, _ = urldefrag(url)
    return url.rstrip("/")


async def save_file(url, content):
    parsed = urlparse(url)
    path = parsed.path

    if path == "" or path.endswith("/"):
        path = path + "index.html"
    elif not os.path.splitext(path)[1]:
        path = path + "/index.html"

    save_path = OUTPUT_DIR + path

    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    async with aiofiles.open(save_path, "wb") as f:
        await f.write(content)


async def fetch(session, url):
    async with semaphore:
        try:
            async with session.get(url, timeout=15) as response:
                if response.status != 200:
                    return None
                return await response.read(), await response.text()
        except:
            return None


async def worker(session):
    while True:
        url = await queue.get()
        url = normalize(url)

        if url in visited:
            queue.task_done()
            continue

        visited.add(url)

        result = await fetch(session, url)
        if result is None:
            queue.task_done()
            continue

        binary_content, text_content = result
        await save_file(url, binary_content)

        soup = BeautifulSoup(text_content, "html.parser")

        tags = soup.find_all(["a", "link", "img", "script"])
        for tag in tags:
            attr = "href" if tag.name in ["a", "link"] else "src"
            link = tag.get(attr)

            if not link:
                continue

            absolute = normalize(urljoin(url, link))
            parsed = urlparse(absolute)

            if parsed.netloc == DOMAIN and absolute not in visited:
                await queue.put(absolute)

        queue.task_done()


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    await queue.put(START_URL)

    async with aiohttp.ClientSession(headers={"User-Agent": "FastCrawler/3.0"}) as session:
        tasks = []
        for _ in range(50):  # 50 async workers
            task = asyncio.create_task(worker(session))
            tasks.append(task)

        await queue.join()

        for task in tasks:
            task.cancel()


if __name__ == "__main__":
    print("🚀 FAST Export Started...")
    asyncio.run(main())
    print("✅ FAST Export Complete!")

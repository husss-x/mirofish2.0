"""
SeedAgent — autonomous research pipeline for MiroFish 2.0

Three modes:
  - web_only: query → research → markdown
  - upload_only: file text → markdown (passthrough)
  - hybrid: file text + query → research → merged markdown
"""

import os
import time
import httpx
import feedparser
import trafilatura
from dataclasses import dataclass
from typing import Optional
from openai import OpenAI
from app.utils.llm_client import LLMClient
from app.utils.logger import get_logger

logger = get_logger(__name__)

# RSS feeds for news scraping
NEWS_FEEDS = [
    "https://feeds.reuters.com/reuters/worldNews",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://feeds.ap.org/ap/world",
]

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"


@dataclass
class SeedResult:
    markdown: str
    sources: list[str]
    token_count: int
    elapsed_seconds: float


class SeedAgent:
    """
    Orchestrates multi-source research and produces a structured
    markdown seed document for MiroFish ingestion.
    """

    def __init__(self):
        self.llm = LLMClient()
        self.tavily_key = os.getenv("TAVILY_API_KEY")
        self.grok_key = os.getenv("GROK_API_KEY")
        self.max_tokens = int(os.getenv("SEED_MAX_TOKENS", "8000"))
        self.max_seconds = int(os.getenv("SEED_MAX_SECONDS", "90"))
        self.max_sources = int(os.getenv("SEED_MAX_SOURCES", "15"))

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run_web_only(self, query: str, simulation_requirement: str) -> SeedResult:
        """Mode 1: fully autonomous research from a natural language query."""
        start = time.time()
        logger.info(f"[SeedAgent] web_only — query: {query}")

        sub_queries = self._decompose_query(query)
        raw_results = self._gather_all_sources(query, sub_queries)
        markdown = self._synthesize(query, simulation_requirement, raw_results)

        return SeedResult(
            markdown=markdown,
            sources=[r["url"] for r in raw_results if r.get("url")],
            token_count=len(markdown.split()),
            elapsed_seconds=round(time.time() - start, 1),
        )

    def run_upload_only(self, file_text: str) -> SeedResult:
        """Mode 2: passthrough — file text becomes the seed document as-is."""
        logger.info("[SeedAgent] upload_only — passthrough")
        return SeedResult(
            markdown=file_text,
            sources=[],
            token_count=len(file_text.split()),
            elapsed_seconds=0.0,
        )

    def run_hybrid(
        self, file_text: str, query: str, simulation_requirement: str
    ) -> SeedResult:
        """Mode 3: enrich uploaded document with fresh web research."""
        start = time.time()
        logger.info(f"[SeedAgent] hybrid — query: {query}")

        sub_queries = self._decompose_query(query)
        raw_results = self._gather_all_sources(query, sub_queries)
        markdown = self._synthesize_hybrid(
            query, simulation_requirement, file_text, raw_results
        )

        return SeedResult(
            markdown=markdown,
            sources=[r["url"] for r in raw_results if r.get("url")],
            token_count=len(markdown.split()),
            elapsed_seconds=round(time.time() - start, 1),
        )

    # ------------------------------------------------------------------
    # Step 1 — Query decomposition
    # ------------------------------------------------------------------

    def _decompose_query(self, query: str) -> list[str]:
        """Use LLM to break the query into 5-8 focused sub-queries."""
        result = self.llm.chat_json([
            {
                "role": "system",
                "content": (
                    "You are a research analyst. Given a prediction query, "
                    "decompose it into 5-8 specific sub-queries that together "
                    "cover all angles needed for a comprehensive simulation seed. "
                    "Focus on: key actors, recent events, geopolitical context, "
                    "public sentiment, historical background, and future triggers. "
                    "Return JSON: {\"sub_queries\": [\"...\", ...]}"
                ),
            },
            {"role": "user", "content": f"Query: {query}"},
        ])
        sub_queries = result.get("sub_queries", [query])
        logger.info(f"[SeedAgent] decomposed into {len(sub_queries)} sub-queries")
        return sub_queries

    # ------------------------------------------------------------------
    # Step 2 — Multi-source gathering
    # ------------------------------------------------------------------

    def _gather_all_sources(
        self, query: str, sub_queries: list[str]
    ) -> list[dict]:
        """Gather results from all available sources in parallel."""
        results = []
        start = time.time()

        # Tavily web search
        if self.tavily_key:
            results += self._search_tavily(sub_queries[:6])

        # Grok/X real-time tweets
        if self.grok_key:
            results += self._search_grok(query)

        # RSS news feeds
        results += self._scrape_rss(query)

        # Wikipedia background
        results += self._search_wikipedia(sub_queries[:3])

        # GDELT geopolitical events
        results += self._search_gdelt(query)

        # Stop criteria: cap sources and tokens
        results = results[: self.max_sources]
        logger.info(
            f"[SeedAgent] gathered {len(results)} sources "
            f"in {round(time.time()-start,1)}s"
        )
        return results

    def _search_tavily(self, sub_queries: list[str]) -> list[dict]:
        """Search web via Tavily API — returns clean LLM-ready snippets."""
        from tavily import TavilyClient
        client = TavilyClient(api_key=self.tavily_key)
        results = []
        for q in sub_queries:
            try:
                resp = client.search(
                    q,
                    search_depth="basic",
                    max_results=3,
                    include_answer=False,
                )
                for r in resp.get("results", []):
                    results.append({
                        "source": "tavily",
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "content": r.get("content", ""),
                    })
            except Exception as e:
                logger.warning(f"[SeedAgent] Tavily error: {e}")
        return results

    def _search_grok(self, query: str) -> list[dict]:
        """
        Fetch recent X/Twitter content via Grok API.
        xAI uses OpenAI-compatible SDK — just swap base_url.
        """
        try:
            client = OpenAI(
                api_key=self.grok_key,
                base_url="https://api.x.ai/v1",
            )
            resp = client.chat.completions.create(
                model="grok-3-mini",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a social media analyst with access to "
                            "real-time X/Twitter data. Return the most relevant "
                            "recent posts, reactions, and sentiment about the "
                            "given topic. Format as bullet points with key quotes "
                            "and account types (journalist, official, analyst). "
                            "Focus on the last 7 days."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"What is the current X/Twitter sentiment and key posts about: {query}",
                    },
                ],
                max_tokens=1500,
            )
            content = resp.choices[0].message.content
            return [{
                "source": "grok_x",
                "url": "https://x.com",
                "title": f"X/Twitter real-time sentiment: {query}",
                "content": content,
            }]
        except Exception as e:
            logger.warning(f"[SeedAgent] Grok error: {e}")
            return []

    def _scrape_rss(self, query: str) -> list[dict]:
        """Scrape RSS feeds and filter articles relevant to the query."""
        keywords = [w.lower() for w in query.split() if len(w) > 3]
        results = []
        for feed_url in NEWS_FEEDS:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:20]:
                    title = entry.get("title", "").lower()
                    summary = entry.get("summary", "").lower()
                    if any(kw in title or kw in summary for kw in keywords):
                        full_text = ""
                        link = entry.get("link", "")
                        if link:
                            try:
                                downloaded = trafilatura.fetch_url(link)
                                if downloaded:
                                    full_text = trafilatura.extract(downloaded) or ""
                            except Exception:
                                full_text = entry.get("summary", "")
                        results.append({
                            "source": "rss",
                            "url": link,
                            "title": entry.get("title", ""),
                            "content": full_text or entry.get("summary", ""),
                        })
                        if len(results) >= 5:
                            break
            except Exception as e:
                logger.warning(f"[SeedAgent] RSS error {feed_url}: {e}")
        return results

    def _search_wikipedia(self, sub_queries: list[str]) -> list[dict]:
        """Fetch Wikipedia summaries for key entities."""
        import requests
        from urllib.parse import quote
        headers = {"User-Agent": "MiroFish/2.0"}
        results = []
        for q in sub_queries[:3]:
            try:
                search_resp = requests.get(
                    WIKIPEDIA_API,
                    params={
                        "action": "query",
                        "list": "search",
                        "srsearch": q,
                        "format": "json",
                        "srlimit": 1,
                    },
                    headers=headers,
                    timeout=10,
                )
                pages = search_resp.json().get("query", {}).get("search", [])
                if pages:
                    title = pages[0]["title"]
                    summary_resp = requests.get(
                        f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title)}",
                        headers=headers,
                        timeout=10,
                    )
                    extract = summary_resp.json().get("extract", "")[:2000]
                    results.append({
                        "source": "wikipedia",
                        "url": f"https://en.wikipedia.org/wiki/{quote(title)}",
                        "title": title,
                        "content": extract,
                    })
            except Exception as e:
                logger.warning(f"[SeedAgent] Wikipedia error: {e}")
        return results

    def _search_gdelt(self, query: str) -> list[dict]:
        """Query GDELT for recent geopolitical event coverage."""
        try:
            params = {
                "query": query,
                "mode": "artlist",
                "maxrecords": 5,
                "format": "json",
                "timespan": "7days",
            }
            resp = httpx.get(
                GDELT_API,
                params=params,
                headers={"Accept": "application/json"},
                timeout=20,
            )
            try:
                data = resp.json()
            except Exception:
                data = {}
            articles = data.get("articles", [])
            results = []
            for art in articles:
                results.append({
                    "source": "gdelt",
                    "url": art.get("url", ""),
                    "title": art.get("title", ""),
                    "content": art.get("seendate", "") + " — " + art.get("title", ""),
                })
            return results
        except Exception as e:
            logger.warning(f"[SeedAgent] GDELT error: {e}")
            return []

    # ------------------------------------------------------------------
    # Step 3 — Synthesis
    # ------------------------------------------------------------------

    def _synthesize(
        self,
        query: str,
        simulation_requirement: str,
        raw_results: list[dict],
    ) -> str:
        """Synthesize all gathered sources into a structured markdown document."""
        sources_text = self._format_sources(raw_results)
        return self.llm.chat([
            {
                "role": "system",
                "content": (
                    "You are a research analyst preparing a seed document for "
                    "a multi-agent social simulation. Your document must be "
                    "rich in named entities (people, organizations, places, "
                    "events) and factual claims that agents can embody and argue "
                    "about. Structure it with clear sections: ## Key Actors, "
                    "## Recent Events, ## Geopolitical Context, "
                    "## Public Sentiment, ## Key Tensions & Triggers. "
                    "Write in English. Be specific — names, dates, positions matter."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Prediction query: {query}\n"
                    f"Simulation requirement: {simulation_requirement}\n\n"
                    f"Sources gathered:\n{sources_text}"
                ),
            },
        ])

    def _synthesize_hybrid(
        self,
        query: str,
        simulation_requirement: str,
        file_text: str,
        raw_results: list[dict],
    ) -> str:
        """Merge uploaded document with fresh web research."""
        sources_text = self._format_sources(raw_results)
        return self.llm.chat([
            {
                "role": "system",
                "content": (
                    "You are a research analyst merging an internal document "
                    "with fresh web intelligence. Produce a unified seed document "
                    "with two clearly labeled sections: "
                    "'## Background (from uploaded document)' which summarizes "
                    "the key entities and claims from the user's file, and "
                    "'## Recent Developments (from web research)' which adds "
                    "what has happened since. Then add a final section "
                    "'## Synthesis' that connects both. "
                    "Write in English. Preserve all named entities from both sources."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Prediction query: {query}\n"
                    f"Simulation requirement: {simulation_requirement}\n\n"
                    f"--- UPLOADED DOCUMENT ---\n{file_text[:4000]}\n\n"
                    f"--- WEB SOURCES ---\n{sources_text}"
                ),
            },
        ])

    def _format_sources(self, results: list[dict]) -> str:
        parts = []
        for i, r in enumerate(results, 1):
            parts.append(
                f"[{i}] {r.get('title','')} ({r.get('source','')}) "
                f"— {r.get('url','')}\n{r.get('content','')[:800]}"
            )
        return "\n\n".join(parts)

#!/usr/bin/env python3
"""MCP server (stdio). Add to Claude Code:  claude mcp add agentcut -- python /path/to/mcp_server.py"""
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from core.tools import project_from_env

mcp = FastMCP("agentcut")


def _p():
    return project_from_env()


@mcp.tool()
def get_guide() -> str:
    """Editing guide and timeline format. Read once before editing."""
    return (Path(__file__).parent / "AGENTS.md").read_text()


@mcp.tool()
def init_project(zip_path: str) -> dict:
    """Extract the user's zip (numbered images/videos + voiceover) and list what is inside."""
    _p().init(zip_path)
    return _p().summary()


@mcp.tool()
def get_manifest() -> dict:
    """Files in order, voiceover and its length."""
    return _p().summary()


@mcp.tool()
def transcribe(language: str | None = None) -> dict:
    """Transcribe the voiceover with Whisper (word timestamps)."""
    return _p().transcribe(language=language)


@mcp.tool()
def get_transcript(detail: str = "segments", start: float | None = None, end: float | None = None) -> dict:
    """Sentences with times. detail='words' gives word times for a window of at most 60s."""
    return _p().get_transcript(detail, start, end)


@mcp.tool()
def find_in_transcript(phrase: str) -> dict:
    """When is this word or phrase spoken?"""
    return _p().find(phrase)


@mcp.tool()
def list_sfx() -> dict:
    """Sound effects: file|length|when to use."""
    return _p().list_sfx()


@mcp.tool()
def draft_timeline(format: str | None = None, words_per_chunk: int = 3, transition_sfx: str | None = None,
                   captions: bool = True) -> dict:
    """Build and save an automatic first cut (images in order, cuts on sentence boundaries)."""
    return _p().draft(format, words_per_chunk, transition_sfx, captions)


@mcp.tool()
def show_timeline() -> dict:
    """Compact view of the timeline: index|file|time|motion|transition|spoken words."""
    return _p().show()


@mcp.tool()
def patch_timeline(ops: list[dict]) -> dict:
    """Edit ops: set_clip, swap, reorder, insert_clip, remove_clip, add_sfx, remove_sfx, set. See the guide."""
    return _p().patch(ops)


@mcp.tool()
def write_timeline(timeline: dict) -> dict:
    """Replace the whole timeline JSON (rarely needed)."""
    return _p().write(timeline)


@mcp.tool()
def render(quality: str = "preview") -> dict:
    """Render the timeline. quality: 'preview' (fast, half-res) or 'final'."""
    return _p().render(quality)


if __name__ == "__main__":
    mcp.run()

#!/usr/bin/env python3
"""
OpenRouter Free Models Generator

Generate markdown lists of free models from OpenRouter with various sorting options.

Usage: python3 fetch_free_models.py [order]
Help: python3 fetch_free_models.py help (or -h, --help)

Default: top (top-weekly - most popular models)
Short aliases: top=top-weekly, new=newest, ctx=context-high-to-low, speed=throughput-high-to-low, fast=latency-low-to-high
Full names: context-high-to-low, newest, top-weekly, throughput-high-to-low, latency-low-to-high

Output: scripts/free-models-list.md
"""

import requests
import json
import re
import sys
from datetime import datetime


def fetch_free_models(order='context-high-to-low'):
    """Fetch free models from OpenRouter API."""
    url = "https://openrouter.ai/api/frontend/models/find"
    params = {
        'order': order,
        'q': 'free'
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }

    try:
        print("Fetching free models from OpenRouter API...")
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()

        data = response.json()

        if 'data' in data and 'models' in data['data']:
            models = data['data']['models']
            print(f"Found {len(models)} free models")

            return models
        else:
            print("Unexpected API response structure")
            return []

    except Exception as e:
        print(f"Error fetching data: {e}")
        return []


def extract_model_info(model):
    """Extract available information from model data."""
    info = {}

    # Basic info
    full_name = model.get('name', '').replace(' (free)', '')
    if ': ' in full_name:
        developer_part, name = full_name.split(': ', 1)
        info['developer'] = developer_part.lower()
        info['name'] = name
        info['developer_capitalized'] = developer_part
    else:
        info['developer'] = model.get('author', '').lower()
        info['name'] = full_name
        info['developer_capitalized'] = model.get('author', '')

    # Use model_variant_slug which already includes :free
    info['model_id'] = model.get('endpoint', {}).get('model_variant_slug', '')

    # Context window
    context_length = model.get('context_length', 0)
    if context_length >= 1000000:
        info['context_window'] = f"{context_length/1000000:.2f}M tokens".replace('.00', '')
    elif context_length >= 1000:
        info['context_window'] = f"{context_length/1000:.0f}K tokens"
    else:
        info['context_window'] = f"{context_length} tokens"

    # Description
    description = model.get('description', '')
    info['description'] = description

    # Pricing
    endpoint = model.get('endpoint', {})
    pricing = endpoint.get('pricing', {})
    if pricing.get('prompt') == "0" and pricing.get('completion') == "0":
        info['pricing'] = "$0/M Input | $0/M Output (Free)"
    else:
        info['pricing'] = None

    # Provider
    info['provider_slug'] = endpoint.get('provider_slug', 'unknown')

    # Reasoning and tool support
    info['supports_reasoning'] = model.get('supports_reasoning', False)
    info['supports_tools'] = endpoint.get('supports_tool_parameters', False)

    return info


def generate_correct_format_markdown(models, order='top-weekly'):
    """Generate markdown in the exact format as free-models-correct.md."""

    # Map order parameter to readable title
    order_titles = {
        'context-high-to-low': 'Context Window Size: High to Low',
        'newest': 'Newest First',
        'top-weekly': 'Top Weekly',
        'throughput-high-to-low': 'Throughput: High to Low',
        'latency-low-to-high': 'Latency: Low to High'
    }

    title = order_titles.get(order, f'Sorted by {order}')

    lines = [
        f"# OPENROUTER.AI FREE MODELS (Sorted by {title})",
        ""
    ]

    for i, model in enumerate(models, 1):
        info = extract_model_info(model)

        # Header
        lines.append(f"## {i}. {info['developer_capitalized']}: {info['name']}")

        # Model ID
        lines.append(info['model_id'])

        # Context Window
        lines.append(f"* Context Window: {info['context_window']}")

        # Description (full, with markdown links cleaned)
        desc = info['description']
        # Clean up markdown links
        desc = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', desc)
        lines.append(f"* Description: \r\n```\r\n{desc}\r\n```")

        # Usage
        if info['pricing']:
            lines.append(f"* Usage: {info['pricing']}")
        else:
            lines.append("* Usage: (Not Specified)")

        # Developer
        lines.append(f"* Developer: {info['developer']}")

        # Provider
        lines.append(f"* Provider: {info['provider_slug']}")

        # Advanced capabilities
        reasoning_text = "Yes" if info['supports_reasoning'] else "No"
        tools_text = "Yes" if info['supports_tools'] else "No"
        lines.append(f"* Supports Reasoning: {reasoning_text}")
        lines.append(f"* Supports Tools: {tools_text}")

        lines.append("")

    with open('scripts/free-models-list.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    print("Saved updated markdown to scripts/free-models-list.md")


def map_order_alias(order_input):
    """Map short aliases to full API parameter names."""
    order_map = {
        # Short aliases
        'top': 'top-weekly',
        'new': 'newest',
        'ctx': 'context-high-to-low',
        'speed': 'throughput-high-to-low',
        'fast': 'latency-low-to-high',
        # Full names (pass through)
        'context-high-to-low': 'context-high-to-low',
        'newest': 'newest',
        'top-weekly': 'top-weekly',
        'throughput-high-to-low': 'throughput-high-to-low',
        'latency-low-to-high': 'latency-low-to-high'
    }
    return order_map.get(order_input, order_input)


def show_help():
    """Display help information."""
    print("OpenRouter Free Models Generator")
    print("================================")
    print()
    print("Usage: python3 fetch_free_models.py [order]")
    print()
    print("Sorting Options:")
    print("  top        - Top weekly (most popular) [DEFAULT]")
    print("  new        - Newest models")
    print("  ctx        - Context window (high to low)")
    print("  speed      - Throughput (high to low)")
    print("  fast       - Latency (low to high)")
    print()
    print("Full Names (also accepted):")
    print("  top-weekly, newest, context-high-to-low, throughput-high-to-low, latency-low-to-high")
    print()
    print("Examples:")
    print("  python3 fetch_free_models.py        # Default: top weekly")
    print("  python3 fetch_free_models.py top    # Most popular models")
    print("  python3 fetch_free_models.py new    # Latest additions")
    print("  python3 fetch_free_models.py ctx    # Highest context windows")
    print("  python3 fetch_free_models.py help   # Show this help")
    print()
    print("Output: scripts/free-models-list.md")


def main():
    """Main function."""
    # Parse command line arguments
    order_input = 'top'  # default to 'top' (top-weekly)
    if len(sys.argv) > 1:
        order_input = sys.argv[1]

    # Show help if requested
    if order_input in ['-h', '--help', 'help', 'h']:
        show_help()
        return

    # Map alias to full API parameter
    order = map_order_alias(order_input)

    # Validate order parameter
    valid_inputs = ['top', 'new', 'ctx', 'speed', 'fast', 'context-high-to-low', 'newest', 'top-weekly', 'throughput-high-to-low', 'latency-low-to-high']
    if order_input not in valid_inputs:
        print(f"Invalid order: {order_input}")
        print()
        show_help()
        return

    print(f"Fetching models with order: {order} (from input: {order_input})")
    models = fetch_free_models(order)
    if models:
        generate_correct_format_markdown(models, order)
        print(f"\nDone! Check scripts/free-models-list.md for the formatted list (sorted by {order}).")
    else:
        print("No models found.")


if __name__ == "__main__":
    main()
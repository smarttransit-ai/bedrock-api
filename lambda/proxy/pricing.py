# ruff: noqa: E501
import json
import logging
import os

# Prices sourced from https://aws.amazon.com/bedrock/pricing/
# Refreshed via scripts/gen_pricing.py (public AWS pricing APIs).
# All values: integer USD-micros (µUSD) per 1,000 tokens.
# $15.00/1M tokens = 15_000 µUSD/1k tokens.
#
# Cross-region inference profiles (us.*, global.*) are listed alongside direct
# model IDs so the correct price is found regardless of how the caller
# addresses the model.
DEFAULT_PRICING: dict[str, dict[str, dict[str, int]]] = {
    "ai21.jamba-1-5-large-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 2000,
            "output_usd_micros_per_1k": 8000,
            "cache_read_input_usd_micros_per_1k": 8000,
            "cache_write_input_usd_micros_per_1k": 8000,
        },
        "batch": {
            "input_usd_micros_per_1k": 2000,
            "output_usd_micros_per_1k": 8000,
            "cache_read_input_usd_micros_per_1k": 8000,
            "cache_write_input_usd_micros_per_1k": 8000,
        },
    },  # $2/$8 per 1M
    "ai21.jamba-1-5-mini-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 200,
            "output_usd_micros_per_1k": 400,
            "cache_read_input_usd_micros_per_1k": 400,
            "cache_write_input_usd_micros_per_1k": 400,
        },
        "batch": {
            "input_usd_micros_per_1k": 200,
            "output_usd_micros_per_1k": 400,
            "cache_read_input_usd_micros_per_1k": 400,
            "cache_write_input_usd_micros_per_1k": 400,
        },
    },  # $0.2/$0.4 per 1M
    "amazon.nova-2-lite-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 1375,
            "cache_read_input_usd_micros_per_1k": 1375,
            "cache_write_input_usd_micros_per_1k": 1375,
        },
        "batch": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 1375,
            "cache_read_input_usd_micros_per_1k": 1375,
            "cache_write_input_usd_micros_per_1k": 1375,
        },
    },  # $0.3/$1.375 per 1M
    "amazon.nova-lite-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 60,
            "output_usd_micros_per_1k": 240,
            "cache_read_input_usd_micros_per_1k": 240,
            "cache_write_input_usd_micros_per_1k": 240,
        },
        "batch": {
            "input_usd_micros_per_1k": 60,
            "output_usd_micros_per_1k": 240,
            "cache_read_input_usd_micros_per_1k": 240,
            "cache_write_input_usd_micros_per_1k": 240,
        },
    },  # $0.06/$0.24 per 1M
    "amazon.nova-micro-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 35,
            "output_usd_micros_per_1k": 70,
            "cache_read_input_usd_micros_per_1k": 70,
            "cache_write_input_usd_micros_per_1k": 70,
        },
        "batch": {
            "input_usd_micros_per_1k": 35,
            "output_usd_micros_per_1k": 70,
            "cache_read_input_usd_micros_per_1k": 70,
            "cache_write_input_usd_micros_per_1k": 70,
        },
    },  # $0.035/$0.07 per 1M
    "amazon.nova-premier-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 4375,
            "output_usd_micros_per_1k": 12500,
            "cache_read_input_usd_micros_per_1k": 12500,
            "cache_write_input_usd_micros_per_1k": 12500,
        },
        "batch": {
            "input_usd_micros_per_1k": 4375,
            "output_usd_micros_per_1k": 12500,
            "cache_read_input_usd_micros_per_1k": 12500,
            "cache_write_input_usd_micros_per_1k": 12500,
        },
    },  # $4.375/$12.5 per 1M
    "amazon.nova-pro-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 1400,
            "output_usd_micros_per_1k": 3200,
            "cache_read_input_usd_micros_per_1k": 3200,
            "cache_write_input_usd_micros_per_1k": 3200,
        },
        "batch": {
            "input_usd_micros_per_1k": 1400,
            "output_usd_micros_per_1k": 3200,
            "cache_read_input_usd_micros_per_1k": 3200,
            "cache_write_input_usd_micros_per_1k": 3200,
        },
    },  # $1.4/$3.2 per 1M
    "anthropic.claude-3-5-haiku-20241022-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 800,
            "output_usd_micros_per_1k": 4000,
            "cache_read_input_usd_micros_per_1k": 80,
            "cache_write_input_usd_micros_per_1k": 1000,
        },
        "batch": {
            "input_usd_micros_per_1k": 400,
            "output_usd_micros_per_1k": 2000,
            "cache_read_input_usd_micros_per_1k": 2000,
            "cache_write_input_usd_micros_per_1k": 2000,
        },
    },  # $0.8/$4 per 1M
    "anthropic.claude-3-5-sonnet-20240620-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 15000,
            "cache_write_input_usd_micros_per_1k": 15000,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $3/$15 per 1M
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $3/$15 per 1M
    "anthropic.claude-3-7-sonnet-20250219-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $3/$15 per 1M
    "anthropic.claude-3-haiku-20240307-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 250,
            "output_usd_micros_per_1k": 1250,
            "cache_read_input_usd_micros_per_1k": 1250,
            "cache_write_input_usd_micros_per_1k": 1250,
        },
        "batch": {
            "input_usd_micros_per_1k": 125,
            "output_usd_micros_per_1k": 625,
            "cache_read_input_usd_micros_per_1k": 625,
            "cache_write_input_usd_micros_per_1k": 625,
        },
    },  # $0.25/$1.25 per 1M
    "anthropic.claude-3-opus-20240229-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 15000,
            "output_usd_micros_per_1k": 75000,
            "cache_read_input_usd_micros_per_1k": 75000,
            "cache_write_input_usd_micros_per_1k": 75000,
        },
        "batch": {
            "input_usd_micros_per_1k": 7500,
            "output_usd_micros_per_1k": 37500,
            "cache_read_input_usd_micros_per_1k": 37500,
            "cache_write_input_usd_micros_per_1k": 37500,
        },
    },  # $15/$75 per 1M
    "anthropic.claude-3-sonnet-20240229-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 15000,
            "cache_write_input_usd_micros_per_1k": 15000,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $3/$15 per 1M
    "anthropic.claude-haiku-4-5-20251001-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 2000,
            "output_usd_micros_per_1k": 5000,
            "cache_read_input_usd_micros_per_1k": 100,
            "cache_write_input_usd_micros_per_1k": 1250,
        },
        "batch": {
            "input_usd_micros_per_1k": 500,
            "output_usd_micros_per_1k": 2500,
            "cache_read_input_usd_micros_per_1k": 2500,
            "cache_write_input_usd_micros_per_1k": 2500,
        },
    },  # $2/$5 per 1M
    "anthropic.claude-instant-v1": {
        "on_demand": {
            "input_usd_micros_per_1k": 800,
            "output_usd_micros_per_1k": 2400,
            "cache_read_input_usd_micros_per_1k": 2400,
            "cache_write_input_usd_micros_per_1k": 2400,
        },
        "batch": {
            "input_usd_micros_per_1k": 800,
            "output_usd_micros_per_1k": 2400,
            "cache_read_input_usd_micros_per_1k": 2400,
            "cache_write_input_usd_micros_per_1k": 2400,
        },
    },  # $0.8/$2.4 per 1M
    "anthropic.claude-opus-4-1-20250805-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 15000,
            "output_usd_micros_per_1k": 75000,
            "cache_read_input_usd_micros_per_1k": 1500,
            "cache_write_input_usd_micros_per_1k": 18750,
        },
        "batch": {
            "input_usd_micros_per_1k": 15000,
            "output_usd_micros_per_1k": 75000,
            "cache_read_input_usd_micros_per_1k": 1500,
            "cache_write_input_usd_micros_per_1k": 18750,
        },
    },  # $15/$75 per 1M
    "anthropic.claude-opus-4-20250514-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 15000,
            "output_usd_micros_per_1k": 75000,
            "cache_read_input_usd_micros_per_1k": 1500,
            "cache_write_input_usd_micros_per_1k": 18750,
        },
        "batch": {
            "input_usd_micros_per_1k": 7500,
            "output_usd_micros_per_1k": 37500,
            "cache_read_input_usd_micros_per_1k": 37500,
            "cache_write_input_usd_micros_per_1k": 37500,
        },
    },  # $15/$75 per 1M
    "anthropic.claude-opus-4-5-20251101-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 10000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
        "batch": {
            "input_usd_micros_per_1k": 2500,
            "output_usd_micros_per_1k": 12500,
            "cache_read_input_usd_micros_per_1k": 12500,
            "cache_write_input_usd_micros_per_1k": 12500,
        },
    },  # $10/$25 per 1M
    "anthropic.claude-opus-4-6-v1": {
        "on_demand": {
            "input_usd_micros_per_1k": 5000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
        "batch": {
            "input_usd_micros_per_1k": 2500,
            "output_usd_micros_per_1k": 12500,
            "cache_read_input_usd_micros_per_1k": 12500,
            "cache_write_input_usd_micros_per_1k": 12500,
        },
    },  # $5/$25 per 1M
    "anthropic.claude-opus-4-7": {
        "on_demand": {
            "input_usd_micros_per_1k": 5000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
        "batch": {
            "input_usd_micros_per_1k": 5000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
    },  # $5/$25 per 1M
    "anthropic.claude-sonnet-4-20250514-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $3/$15 per 1M
    "anthropic.claude-sonnet-4-5-20250929-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 6000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $6/$15 per 1M
    "anthropic.claude-sonnet-4-6": {
        "on_demand": {
            "input_usd_micros_per_1k": 6000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $6/$15 per 1M
    "cohere.command-light-text-v14": {
        "on_demand": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 600,
            "cache_read_input_usd_micros_per_1k": 600,
            "cache_write_input_usd_micros_per_1k": 600,
        },
        "batch": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 600,
            "cache_read_input_usd_micros_per_1k": 600,
            "cache_write_input_usd_micros_per_1k": 600,
        },
    },  # $0.3/$0.6 per 1M
    "cohere.command-r-plus-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 15000,
            "cache_write_input_usd_micros_per_1k": 15000,
        },
        "batch": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 15000,
            "cache_write_input_usd_micros_per_1k": 15000,
        },
    },  # $3/$15 per 1M
    "cohere.command-r-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 500,
            "output_usd_micros_per_1k": 1500,
            "cache_read_input_usd_micros_per_1k": 1500,
            "cache_write_input_usd_micros_per_1k": 1500,
        },
        "batch": {
            "input_usd_micros_per_1k": 500,
            "output_usd_micros_per_1k": 1500,
            "cache_read_input_usd_micros_per_1k": 1500,
            "cache_write_input_usd_micros_per_1k": 1500,
        },
    },  # $0.5/$1.5 per 1M
    "cohere.command-text-v14": {
        "on_demand": {
            "input_usd_micros_per_1k": 1000,
            "output_usd_micros_per_1k": 2000,
            "cache_read_input_usd_micros_per_1k": 2000,
            "cache_write_input_usd_micros_per_1k": 2000,
        },
        "batch": {
            "input_usd_micros_per_1k": 1000,
            "output_usd_micros_per_1k": 2000,
            "cache_read_input_usd_micros_per_1k": 2000,
            "cache_write_input_usd_micros_per_1k": 2000,
        },
    },  # $1/$2 per 1M
    "deepseek.r1-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 1350,
            "output_usd_micros_per_1k": 5400,
            "cache_read_input_usd_micros_per_1k": 5400,
            "cache_write_input_usd_micros_per_1k": 5400,
        },
        "batch": {
            "input_usd_micros_per_1k": 1350,
            "output_usd_micros_per_1k": 5400,
            "cache_read_input_usd_micros_per_1k": 5400,
            "cache_write_input_usd_micros_per_1k": 5400,
        },
    },  # $1.35/$5.4 per 1M
    "deepseek.v3.2": {
        "on_demand": {
            "input_usd_micros_per_1k": 310,
            "output_usd_micros_per_1k": 3238,
            "cache_read_input_usd_micros_per_1k": 3238,
            "cache_write_input_usd_micros_per_1k": 3238,
        },
        "batch": {
            "input_usd_micros_per_1k": 310,
            "output_usd_micros_per_1k": 3238,
            "cache_read_input_usd_micros_per_1k": 3238,
            "cache_write_input_usd_micros_per_1k": 3238,
        },
    },  # $0.31/$3.238 per 1M
    "global.amazon.nova-2-lite-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 1375,
            "cache_read_input_usd_micros_per_1k": 1375,
            "cache_write_input_usd_micros_per_1k": 1375,
        },
        "batch": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 1375,
            "cache_read_input_usd_micros_per_1k": 1375,
            "cache_write_input_usd_micros_per_1k": 1375,
        },
    },  # $0.3/$1.375 per 1M
    "global.anthropic.claude-haiku-4-5-20251001-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 2000,
            "output_usd_micros_per_1k": 5000,
            "cache_read_input_usd_micros_per_1k": 100,
            "cache_write_input_usd_micros_per_1k": 1250,
        },
        "batch": {
            "input_usd_micros_per_1k": 500,
            "output_usd_micros_per_1k": 2500,
            "cache_read_input_usd_micros_per_1k": 2500,
            "cache_write_input_usd_micros_per_1k": 2500,
        },
    },  # $2/$5 per 1M
    "global.anthropic.claude-opus-4-5-20251101-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 10000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
        "batch": {
            "input_usd_micros_per_1k": 2500,
            "output_usd_micros_per_1k": 12500,
            "cache_read_input_usd_micros_per_1k": 12500,
            "cache_write_input_usd_micros_per_1k": 12500,
        },
    },  # $10/$25 per 1M
    "global.anthropic.claude-opus-4-6-v1": {
        "on_demand": {
            "input_usd_micros_per_1k": 5000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
        "batch": {
            "input_usd_micros_per_1k": 2500,
            "output_usd_micros_per_1k": 12500,
            "cache_read_input_usd_micros_per_1k": 12500,
            "cache_write_input_usd_micros_per_1k": 12500,
        },
    },  # $5/$25 per 1M
    "global.anthropic.claude-opus-4-7": {
        "on_demand": {
            "input_usd_micros_per_1k": 5000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
        "batch": {
            "input_usd_micros_per_1k": 5000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
    },  # $5/$25 per 1M
    "global.anthropic.claude-sonnet-4-20250514-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $3/$15 per 1M
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 6000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $6/$15 per 1M
    "global.anthropic.claude-sonnet-4-6": {
        "on_demand": {
            "input_usd_micros_per_1k": 6000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $6/$15 per 1M
    "google.gemma-3-12b-it": {
        "on_demand": {
            "input_usd_micros_per_1k": 160,
            "output_usd_micros_per_1k": 150,
            "cache_read_input_usd_micros_per_1k": 150,
            "cache_write_input_usd_micros_per_1k": 150,
        },
        "batch": {
            "input_usd_micros_per_1k": 160,
            "output_usd_micros_per_1k": 150,
            "cache_read_input_usd_micros_per_1k": 150,
            "cache_write_input_usd_micros_per_1k": 150,
        },
    },  # $0.16/$0.15 per 1M
    "google.gemma-3-27b-it": {
        "on_demand": {
            "input_usd_micros_per_1k": 230,
            "output_usd_micros_per_1k": 670,
            "cache_read_input_usd_micros_per_1k": 670,
            "cache_write_input_usd_micros_per_1k": 670,
        },
        "batch": {
            "input_usd_micros_per_1k": 230,
            "output_usd_micros_per_1k": 670,
            "cache_read_input_usd_micros_per_1k": 670,
            "cache_write_input_usd_micros_per_1k": 670,
        },
    },  # $0.23/$0.67 per 1M
    "google.gemma-3-4b-it": {
        "on_demand": {
            "input_usd_micros_per_1k": 70,
            "output_usd_micros_per_1k": 40,
            "cache_read_input_usd_micros_per_1k": 40,
            "cache_write_input_usd_micros_per_1k": 40,
        },
        "batch": {
            "input_usd_micros_per_1k": 70,
            "output_usd_micros_per_1k": 40,
            "cache_read_input_usd_micros_per_1k": 40,
            "cache_write_input_usd_micros_per_1k": 40,
        },
    },  # $0.07/$0.04 per 1M
    "meta.llama3-1-70b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 360,
            "output_usd_micros_per_1k": 720,
            "cache_read_input_usd_micros_per_1k": 720,
            "cache_write_input_usd_micros_per_1k": 720,
        },
        "batch": {
            "input_usd_micros_per_1k": 360,
            "output_usd_micros_per_1k": 720,
            "cache_read_input_usd_micros_per_1k": 720,
            "cache_write_input_usd_micros_per_1k": 720,
        },
    },  # $0.36/$0.72 per 1M
    "meta.llama3-1-8b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 220,
            "output_usd_micros_per_1k": 220,
            "cache_read_input_usd_micros_per_1k": 220,
            "cache_write_input_usd_micros_per_1k": 220,
        },
        "batch": {
            "input_usd_micros_per_1k": 220,
            "output_usd_micros_per_1k": 220,
            "cache_read_input_usd_micros_per_1k": 220,
            "cache_write_input_usd_micros_per_1k": 220,
        },
    },  # $0.22/$0.22 per 1M
    "meta.llama3-2-11b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 160,
            "output_usd_micros_per_1k": 160,
            "cache_read_input_usd_micros_per_1k": 160,
            "cache_write_input_usd_micros_per_1k": 160,
        },
        "batch": {
            "input_usd_micros_per_1k": 160,
            "output_usd_micros_per_1k": 160,
            "cache_read_input_usd_micros_per_1k": 160,
            "cache_write_input_usd_micros_per_1k": 160,
        },
    },  # $0.16/$0.16 per 1M
    "meta.llama3-2-1b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 100,
            "output_usd_micros_per_1k": 100,
            "cache_read_input_usd_micros_per_1k": 100,
            "cache_write_input_usd_micros_per_1k": 100,
        },
        "batch": {
            "input_usd_micros_per_1k": 100,
            "output_usd_micros_per_1k": 100,
            "cache_read_input_usd_micros_per_1k": 100,
            "cache_write_input_usd_micros_per_1k": 100,
        },
    },  # $0.1/$0.1 per 1M
    "meta.llama3-2-3b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 150,
            "output_usd_micros_per_1k": 150,
            "cache_read_input_usd_micros_per_1k": 150,
            "cache_write_input_usd_micros_per_1k": 150,
        },
        "batch": {
            "input_usd_micros_per_1k": 150,
            "output_usd_micros_per_1k": 150,
            "cache_read_input_usd_micros_per_1k": 150,
            "cache_write_input_usd_micros_per_1k": 150,
        },
    },  # $0.15/$0.15 per 1M
    "meta.llama3-2-90b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 720,
            "output_usd_micros_per_1k": 720,
            "cache_read_input_usd_micros_per_1k": 720,
            "cache_write_input_usd_micros_per_1k": 720,
        },
        "batch": {
            "input_usd_micros_per_1k": 720,
            "output_usd_micros_per_1k": 720,
            "cache_read_input_usd_micros_per_1k": 720,
            "cache_write_input_usd_micros_per_1k": 720,
        },
    },  # $0.72/$0.72 per 1M
    "meta.llama3-3-70b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 360,
            "output_usd_micros_per_1k": 360,
            "cache_read_input_usd_micros_per_1k": 360,
            "cache_write_input_usd_micros_per_1k": 360,
        },
        "batch": {
            "input_usd_micros_per_1k": 360,
            "output_usd_micros_per_1k": 360,
            "cache_read_input_usd_micros_per_1k": 360,
            "cache_write_input_usd_micros_per_1k": 360,
        },
    },  # $0.36/$0.36 per 1M
    "meta.llama3-70b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 2650,
            "output_usd_micros_per_1k": 3500,
            "cache_read_input_usd_micros_per_1k": 3500,
            "cache_write_input_usd_micros_per_1k": 3500,
        },
        "batch": {
            "input_usd_micros_per_1k": 2650,
            "output_usd_micros_per_1k": 3500,
            "cache_read_input_usd_micros_per_1k": 3500,
            "cache_write_input_usd_micros_per_1k": 3500,
        },
    },  # $2.65/$3.5 per 1M
    "meta.llama3-8b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 600,
            "cache_read_input_usd_micros_per_1k": 600,
            "cache_write_input_usd_micros_per_1k": 600,
        },
        "batch": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 600,
            "cache_read_input_usd_micros_per_1k": 600,
            "cache_write_input_usd_micros_per_1k": 600,
        },
    },  # $0.3/$0.6 per 1M
    "meta.llama4-maverick-17b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 240,
            "output_usd_micros_per_1k": 485,
            "cache_read_input_usd_micros_per_1k": 485,
            "cache_write_input_usd_micros_per_1k": 485,
        },
        "batch": {
            "input_usd_micros_per_1k": 240,
            "output_usd_micros_per_1k": 485,
            "cache_read_input_usd_micros_per_1k": 485,
            "cache_write_input_usd_micros_per_1k": 485,
        },
    },  # $0.24/$0.485 per 1M
    "meta.llama4-scout-17b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 85,
            "output_usd_micros_per_1k": 330,
            "cache_read_input_usd_micros_per_1k": 330,
            "cache_write_input_usd_micros_per_1k": 330,
        },
        "batch": {
            "input_usd_micros_per_1k": 85,
            "output_usd_micros_per_1k": 330,
            "cache_read_input_usd_micros_per_1k": 330,
            "cache_write_input_usd_micros_per_1k": 330,
        },
    },  # $0.085/$0.33 per 1M
    "minimax.minimax-m2": {
        "on_demand": {
            "input_usd_micros_per_1k": 150,
            "output_usd_micros_per_1k": 2100,
            "cache_read_input_usd_micros_per_1k": 2100,
            "cache_write_input_usd_micros_per_1k": 2100,
        },
        "batch": {
            "input_usd_micros_per_1k": 150,
            "output_usd_micros_per_1k": 2100,
            "cache_read_input_usd_micros_per_1k": 2100,
            "cache_write_input_usd_micros_per_1k": 2100,
        },
    },  # $0.15/$2.1 per 1M
    "minimax.minimax-m2.1": {
        "on_demand": {
            "input_usd_micros_per_1k": 525,
            "output_usd_micros_per_1k": 1200,
            "cache_read_input_usd_micros_per_1k": 1200,
            "cache_write_input_usd_micros_per_1k": 1200,
        },
        "batch": {
            "input_usd_micros_per_1k": 525,
            "output_usd_micros_per_1k": 1200,
            "cache_read_input_usd_micros_per_1k": 1200,
            "cache_write_input_usd_micros_per_1k": 1200,
        },
    },  # $0.525/$1.2 per 1M
    "minimax.minimax-m2.5": {
        "on_demand": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 2100,
            "cache_read_input_usd_micros_per_1k": 2100,
            "cache_write_input_usd_micros_per_1k": 2100,
        },
        "batch": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 2100,
            "cache_read_input_usd_micros_per_1k": 2100,
            "cache_write_input_usd_micros_per_1k": 2100,
        },
    },  # $0.3/$2.1 per 1M
    "mistral.devstral-2-123b": {
        "on_demand": {
            "input_usd_micros_per_1k": 400,
            "output_usd_micros_per_1k": 1000,
            "cache_read_input_usd_micros_per_1k": 1000,
            "cache_write_input_usd_micros_per_1k": 1000,
        },
        "batch": {
            "input_usd_micros_per_1k": 400,
            "output_usd_micros_per_1k": 1000,
            "cache_read_input_usd_micros_per_1k": 1000,
            "cache_write_input_usd_micros_per_1k": 1000,
        },
    },  # $0.4/$1 per 1M
    "mistral.magistral-small-2509": {
        "on_demand": {
            "input_usd_micros_per_1k": 880,
            "output_usd_micros_per_1k": 750,
            "cache_read_input_usd_micros_per_1k": 750,
            "cache_write_input_usd_micros_per_1k": 750,
        },
        "batch": {
            "input_usd_micros_per_1k": 880,
            "output_usd_micros_per_1k": 750,
            "cache_read_input_usd_micros_per_1k": 750,
            "cache_write_input_usd_micros_per_1k": 750,
        },
    },  # $0.88/$0.75 per 1M
    "mistral.ministral-3-14b-instruct": {
        "on_demand": {
            "input_usd_micros_per_1k": 350,
            "output_usd_micros_per_1k": 100,
            "cache_read_input_usd_micros_per_1k": 100,
            "cache_write_input_usd_micros_per_1k": 100,
        },
        "batch": {
            "input_usd_micros_per_1k": 350,
            "output_usd_micros_per_1k": 100,
            "cache_read_input_usd_micros_per_1k": 100,
            "cache_write_input_usd_micros_per_1k": 100,
        },
    },  # $0.35/$0.1 per 1M
    "mistral.ministral-3-3b-instruct": {
        "on_demand": {
            "input_usd_micros_per_1k": 100,
            "output_usd_micros_per_1k": 170,
            "cache_read_input_usd_micros_per_1k": 170,
            "cache_write_input_usd_micros_per_1k": 170,
        },
        "batch": {
            "input_usd_micros_per_1k": 100,
            "output_usd_micros_per_1k": 170,
            "cache_read_input_usd_micros_per_1k": 170,
            "cache_write_input_usd_micros_per_1k": 170,
        },
    },  # $0.1/$0.17 per 1M
    "mistral.ministral-3-8b-instruct": {
        "on_demand": {
            "input_usd_micros_per_1k": 70,
            "output_usd_micros_per_1k": 260,
            "cache_read_input_usd_micros_per_1k": 260,
            "cache_write_input_usd_micros_per_1k": 260,
        },
        "batch": {
            "input_usd_micros_per_1k": 70,
            "output_usd_micros_per_1k": 260,
            "cache_read_input_usd_micros_per_1k": 260,
            "cache_write_input_usd_micros_per_1k": 260,
        },
    },  # $0.07/$0.26 per 1M
    "mistral.mistral-7b-instruct-v0:2": {
        "on_demand": {
            "input_usd_micros_per_1k": 150,
            "output_usd_micros_per_1k": 200,
            "cache_read_input_usd_micros_per_1k": 200,
            "cache_write_input_usd_micros_per_1k": 200,
        },
        "batch": {
            "input_usd_micros_per_1k": 150,
            "output_usd_micros_per_1k": 200,
            "cache_read_input_usd_micros_per_1k": 200,
            "cache_write_input_usd_micros_per_1k": 200,
        },
    },  # $0.15/$0.2 per 1M
    "mistral.mistral-large-2402-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 4000,
            "output_usd_micros_per_1k": 12000,
            "cache_read_input_usd_micros_per_1k": 12000,
            "cache_write_input_usd_micros_per_1k": 12000,
        },
        "batch": {
            "input_usd_micros_per_1k": 4000,
            "output_usd_micros_per_1k": 12000,
            "cache_read_input_usd_micros_per_1k": 12000,
            "cache_write_input_usd_micros_per_1k": 12000,
        },
    },  # $4/$12 per 1M
    "mistral.mistral-large-3-675b-instruct": {
        "on_demand": {
            "input_usd_micros_per_1k": 500,
            "output_usd_micros_per_1k": 750,
            "cache_read_input_usd_micros_per_1k": 750,
            "cache_write_input_usd_micros_per_1k": 750,
        },
        "batch": {
            "input_usd_micros_per_1k": 500,
            "output_usd_micros_per_1k": 750,
            "cache_read_input_usd_micros_per_1k": 750,
            "cache_write_input_usd_micros_per_1k": 750,
        },
    },  # $0.5/$0.75 per 1M
    "mistral.mistral-small-2402-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 1000,
            "output_usd_micros_per_1k": 1500,
            "cache_read_input_usd_micros_per_1k": 1500,
            "cache_write_input_usd_micros_per_1k": 1500,
        },
        "batch": {
            "input_usd_micros_per_1k": 1000,
            "output_usd_micros_per_1k": 1500,
            "cache_read_input_usd_micros_per_1k": 1500,
            "cache_write_input_usd_micros_per_1k": 1500,
        },
    },  # $1/$1.5 per 1M
    "mistral.mixtral-8x7b-instruct-v0:1": {
        "on_demand": {
            "input_usd_micros_per_1k": 450,
            "output_usd_micros_per_1k": 700,
            "cache_read_input_usd_micros_per_1k": 700,
            "cache_write_input_usd_micros_per_1k": 700,
        },
        "batch": {
            "input_usd_micros_per_1k": 450,
            "output_usd_micros_per_1k": 700,
            "cache_read_input_usd_micros_per_1k": 700,
            "cache_write_input_usd_micros_per_1k": 700,
        },
    },  # $0.45/$0.7 per 1M
    "mistral.pixtral-large-2502-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 2000,
            "output_usd_micros_per_1k": 6000,
            "cache_read_input_usd_micros_per_1k": 6000,
            "cache_write_input_usd_micros_per_1k": 6000,
        },
        "batch": {
            "input_usd_micros_per_1k": 2000,
            "output_usd_micros_per_1k": 6000,
            "cache_read_input_usd_micros_per_1k": 6000,
            "cache_write_input_usd_micros_per_1k": 6000,
        },
    },  # $2/$6 per 1M
    "mistral.voxtral-mini-3b-2507": {
        "on_demand": {
            "input_usd_micros_per_1k": 20,
            "output_usd_micros_per_1k": 20,
            "cache_read_input_usd_micros_per_1k": 20,
            "cache_write_input_usd_micros_per_1k": 20,
        },
        "batch": {
            "input_usd_micros_per_1k": 20,
            "output_usd_micros_per_1k": 20,
            "cache_read_input_usd_micros_per_1k": 20,
            "cache_write_input_usd_micros_per_1k": 20,
        },
    },  # $0.02/$0.02 per 1M
    "mistral.voxtral-small-24b-2507": {
        "on_demand": {
            "input_usd_micros_per_1k": 50,
            "output_usd_micros_per_1k": 150,
            "cache_read_input_usd_micros_per_1k": 150,
            "cache_write_input_usd_micros_per_1k": 150,
        },
        "batch": {
            "input_usd_micros_per_1k": 50,
            "output_usd_micros_per_1k": 150,
            "cache_read_input_usd_micros_per_1k": 150,
            "cache_write_input_usd_micros_per_1k": 150,
        },
    },  # $0.05/$0.15 per 1M
    "moonshot.kimi-k2-thinking": {
        "on_demand": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 1250,
            "cache_read_input_usd_micros_per_1k": 1250,
            "cache_write_input_usd_micros_per_1k": 1250,
        },
        "batch": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 1250,
            "cache_read_input_usd_micros_per_1k": 1250,
            "cache_write_input_usd_micros_per_1k": 1250,
        },
    },  # $0.3/$1.25 per 1M
    "moonshotai.kimi-k2.5": {
        "on_demand": {
            "input_usd_micros_per_1k": 600,
            "output_usd_micros_per_1k": 5250,
            "cache_read_input_usd_micros_per_1k": 5250,
            "cache_write_input_usd_micros_per_1k": 5250,
        },
        "batch": {
            "input_usd_micros_per_1k": 600,
            "output_usd_micros_per_1k": 5250,
            "cache_read_input_usd_micros_per_1k": 5250,
            "cache_write_input_usd_micros_per_1k": 5250,
        },
    },  # $0.6/$5.25 per 1M
    "nvidia.nemotron-nano-12b-v2": {
        "on_demand": {
            "input_usd_micros_per_1k": 200,
            "output_usd_micros_per_1k": 300,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 300,
        },
        "batch": {
            "input_usd_micros_per_1k": 200,
            "output_usd_micros_per_1k": 300,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 300,
        },
    },  # $0.2/$0.3 per 1M
    "nvidia.nemotron-nano-3-30b": {
        "on_demand": {
            "input_usd_micros_per_1k": 60,
            "output_usd_micros_per_1k": 120,
            "cache_read_input_usd_micros_per_1k": 120,
            "cache_write_input_usd_micros_per_1k": 120,
        },
        "batch": {
            "input_usd_micros_per_1k": 60,
            "output_usd_micros_per_1k": 120,
            "cache_read_input_usd_micros_per_1k": 120,
            "cache_write_input_usd_micros_per_1k": 120,
        },
    },  # $0.06/$0.12 per 1M
    "nvidia.nemotron-nano-9b-v2": {
        "on_demand": {
            "input_usd_micros_per_1k": 30,
            "output_usd_micros_per_1k": 230,
            "cache_read_input_usd_micros_per_1k": 230,
            "cache_write_input_usd_micros_per_1k": 230,
        },
        "batch": {
            "input_usd_micros_per_1k": 30,
            "output_usd_micros_per_1k": 230,
            "cache_read_input_usd_micros_per_1k": 230,
            "cache_write_input_usd_micros_per_1k": 230,
        },
    },  # $0.03/$0.23 per 1M
    "nvidia.nemotron-super-3-120b": {
        "on_demand": {
            "input_usd_micros_per_1k": 75,
            "output_usd_micros_per_1k": 325,
            "cache_read_input_usd_micros_per_1k": 325,
            "cache_write_input_usd_micros_per_1k": 325,
        },
        "batch": {
            "input_usd_micros_per_1k": 75,
            "output_usd_micros_per_1k": 325,
            "cache_read_input_usd_micros_per_1k": 325,
            "cache_write_input_usd_micros_per_1k": 325,
        },
    },  # $0.075/$0.325 per 1M
    "openai.gpt-oss-120b-1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 150,
            "output_usd_micros_per_1k": 300,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 300,
        },
        "batch": {
            "input_usd_micros_per_1k": 150,
            "output_usd_micros_per_1k": 300,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 300,
        },
    },  # $0.15/$0.3 per 1M
    "openai.gpt-oss-20b-1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 70,
            "output_usd_micros_per_1k": 300,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 300,
        },
        "batch": {
            "input_usd_micros_per_1k": 70,
            "output_usd_micros_per_1k": 300,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 300,
        },
    },  # $0.07/$0.3 per 1M
    "openai.gpt-oss-safeguard-120b": {
        "on_demand": {
            "input_usd_micros_per_1k": 260,
            "output_usd_micros_per_1k": 1050,
            "cache_read_input_usd_micros_per_1k": 1050,
            "cache_write_input_usd_micros_per_1k": 1050,
        },
        "batch": {
            "input_usd_micros_per_1k": 260,
            "output_usd_micros_per_1k": 1050,
            "cache_read_input_usd_micros_per_1k": 1050,
            "cache_write_input_usd_micros_per_1k": 1050,
        },
    },  # $0.26/$1.05 per 1M
    "openai.gpt-oss-safeguard-20b": {
        "on_demand": {
            "input_usd_micros_per_1k": 70,
            "output_usd_micros_per_1k": 350,
            "cache_read_input_usd_micros_per_1k": 350,
            "cache_write_input_usd_micros_per_1k": 350,
        },
        "batch": {
            "input_usd_micros_per_1k": 70,
            "output_usd_micros_per_1k": 350,
            "cache_read_input_usd_micros_per_1k": 350,
            "cache_write_input_usd_micros_per_1k": 350,
        },
    },  # $0.07/$0.35 per 1M
    "qwen.qwen3-32b-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 262,
            "output_usd_micros_per_1k": 600,
            "cache_read_input_usd_micros_per_1k": 600,
            "cache_write_input_usd_micros_per_1k": 600,
        },
        "batch": {
            "input_usd_micros_per_1k": 262,
            "output_usd_micros_per_1k": 600,
            "cache_read_input_usd_micros_per_1k": 600,
            "cache_write_input_usd_micros_per_1k": 600,
        },
    },  # $0.262/$0.6 per 1M
    "qwen.qwen3-coder-30b-a3b-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 75,
            "output_usd_micros_per_1k": 300,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 300,
        },
        "batch": {
            "input_usd_micros_per_1k": 75,
            "output_usd_micros_per_1k": 300,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 300,
        },
    },  # $0.075/$0.3 per 1M
    "qwen.qwen3-coder-next": {
        "on_demand": {
            "input_usd_micros_per_1k": 875,
            "output_usd_micros_per_1k": 1200,
            "cache_read_input_usd_micros_per_1k": 1200,
            "cache_write_input_usd_micros_per_1k": 1200,
        },
        "batch": {
            "input_usd_micros_per_1k": 875,
            "output_usd_micros_per_1k": 1200,
            "cache_read_input_usd_micros_per_1k": 1200,
            "cache_write_input_usd_micros_per_1k": 1200,
        },
    },  # $0.875/$1.2 per 1M
    "qwen.qwen3-vl-235b-a22b": {
        "on_demand": {
            "input_usd_micros_per_1k": 530,
            "output_usd_micros_per_1k": 4660,
            "cache_read_input_usd_micros_per_1k": 4660,
            "cache_write_input_usd_micros_per_1k": 4660,
        },
        "batch": {
            "input_usd_micros_per_1k": 530,
            "output_usd_micros_per_1k": 4660,
            "cache_read_input_usd_micros_per_1k": 4660,
            "cache_write_input_usd_micros_per_1k": 4660,
        },
    },  # $0.53/$4.66 per 1M
    "us.amazon.nova-2-lite-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 1375,
            "cache_read_input_usd_micros_per_1k": 1375,
            "cache_write_input_usd_micros_per_1k": 1375,
        },
        "batch": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 1375,
            "cache_read_input_usd_micros_per_1k": 1375,
            "cache_write_input_usd_micros_per_1k": 1375,
        },
    },  # $0.3/$1.375 per 1M
    "us.amazon.nova-lite-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 60,
            "output_usd_micros_per_1k": 240,
            "cache_read_input_usd_micros_per_1k": 240,
            "cache_write_input_usd_micros_per_1k": 240,
        },
        "batch": {
            "input_usd_micros_per_1k": 60,
            "output_usd_micros_per_1k": 240,
            "cache_read_input_usd_micros_per_1k": 240,
            "cache_write_input_usd_micros_per_1k": 240,
        },
    },  # $0.06/$0.24 per 1M
    "us.amazon.nova-micro-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 35,
            "output_usd_micros_per_1k": 70,
            "cache_read_input_usd_micros_per_1k": 70,
            "cache_write_input_usd_micros_per_1k": 70,
        },
        "batch": {
            "input_usd_micros_per_1k": 35,
            "output_usd_micros_per_1k": 70,
            "cache_read_input_usd_micros_per_1k": 70,
            "cache_write_input_usd_micros_per_1k": 70,
        },
    },  # $0.035/$0.07 per 1M
    "us.amazon.nova-premier-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 4375,
            "output_usd_micros_per_1k": 12500,
            "cache_read_input_usd_micros_per_1k": 12500,
            "cache_write_input_usd_micros_per_1k": 12500,
        },
        "batch": {
            "input_usd_micros_per_1k": 4375,
            "output_usd_micros_per_1k": 12500,
            "cache_read_input_usd_micros_per_1k": 12500,
            "cache_write_input_usd_micros_per_1k": 12500,
        },
    },  # $4.375/$12.5 per 1M
    "us.amazon.nova-pro-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 1400,
            "output_usd_micros_per_1k": 3200,
            "cache_read_input_usd_micros_per_1k": 3200,
            "cache_write_input_usd_micros_per_1k": 3200,
        },
        "batch": {
            "input_usd_micros_per_1k": 1400,
            "output_usd_micros_per_1k": 3200,
            "cache_read_input_usd_micros_per_1k": 3200,
            "cache_write_input_usd_micros_per_1k": 3200,
        },
    },  # $1.4/$3.2 per 1M
    "us.anthropic.claude-3-5-haiku-20241022-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 800,
            "output_usd_micros_per_1k": 4000,
            "cache_read_input_usd_micros_per_1k": 80,
            "cache_write_input_usd_micros_per_1k": 1000,
        },
        "batch": {
            "input_usd_micros_per_1k": 400,
            "output_usd_micros_per_1k": 2000,
            "cache_read_input_usd_micros_per_1k": 2000,
            "cache_write_input_usd_micros_per_1k": 2000,
        },
    },  # $0.8/$4 per 1M
    "us.anthropic.claude-3-5-sonnet-20241022-v2:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $3/$15 per 1M
    "us.anthropic.claude-3-7-sonnet-20250219-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $3/$15 per 1M
    "us.anthropic.claude-3-haiku-20240307-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 250,
            "output_usd_micros_per_1k": 1250,
            "cache_read_input_usd_micros_per_1k": 1250,
            "cache_write_input_usd_micros_per_1k": 1250,
        },
        "batch": {
            "input_usd_micros_per_1k": 125,
            "output_usd_micros_per_1k": 625,
            "cache_read_input_usd_micros_per_1k": 625,
            "cache_write_input_usd_micros_per_1k": 625,
        },
    },  # $0.25/$1.25 per 1M
    "us.anthropic.claude-3-opus-20240229-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 15000,
            "output_usd_micros_per_1k": 75000,
            "cache_read_input_usd_micros_per_1k": 75000,
            "cache_write_input_usd_micros_per_1k": 75000,
        },
        "batch": {
            "input_usd_micros_per_1k": 7500,
            "output_usd_micros_per_1k": 37500,
            "cache_read_input_usd_micros_per_1k": 37500,
            "cache_write_input_usd_micros_per_1k": 37500,
        },
    },  # $15/$75 per 1M
    "us.anthropic.claude-3-sonnet-20240229-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 15000,
            "cache_write_input_usd_micros_per_1k": 15000,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $3/$15 per 1M
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 2000,
            "output_usd_micros_per_1k": 5000,
            "cache_read_input_usd_micros_per_1k": 100,
            "cache_write_input_usd_micros_per_1k": 1250,
        },
        "batch": {
            "input_usd_micros_per_1k": 500,
            "output_usd_micros_per_1k": 2500,
            "cache_read_input_usd_micros_per_1k": 2500,
            "cache_write_input_usd_micros_per_1k": 2500,
        },
    },  # $2/$5 per 1M
    "us.anthropic.claude-opus-4-1-20250805-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 15000,
            "output_usd_micros_per_1k": 75000,
            "cache_read_input_usd_micros_per_1k": 1500,
            "cache_write_input_usd_micros_per_1k": 18750,
        },
        "batch": {
            "input_usd_micros_per_1k": 15000,
            "output_usd_micros_per_1k": 75000,
            "cache_read_input_usd_micros_per_1k": 1500,
            "cache_write_input_usd_micros_per_1k": 18750,
        },
    },  # $15/$75 per 1M
    "us.anthropic.claude-opus-4-20250514-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 15000,
            "output_usd_micros_per_1k": 75000,
            "cache_read_input_usd_micros_per_1k": 1500,
            "cache_write_input_usd_micros_per_1k": 18750,
        },
        "batch": {
            "input_usd_micros_per_1k": 7500,
            "output_usd_micros_per_1k": 37500,
            "cache_read_input_usd_micros_per_1k": 37500,
            "cache_write_input_usd_micros_per_1k": 37500,
        },
    },  # $15/$75 per 1M
    "us.anthropic.claude-opus-4-5-20251101-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 10000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
        "batch": {
            "input_usd_micros_per_1k": 2500,
            "output_usd_micros_per_1k": 12500,
            "cache_read_input_usd_micros_per_1k": 12500,
            "cache_write_input_usd_micros_per_1k": 12500,
        },
    },  # $10/$25 per 1M
    "us.anthropic.claude-opus-4-6-v1": {
        "on_demand": {
            "input_usd_micros_per_1k": 5000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
        "batch": {
            "input_usd_micros_per_1k": 2500,
            "output_usd_micros_per_1k": 12500,
            "cache_read_input_usd_micros_per_1k": 12500,
            "cache_write_input_usd_micros_per_1k": 12500,
        },
    },  # $5/$25 per 1M
    "us.anthropic.claude-opus-4-7": {
        "on_demand": {
            "input_usd_micros_per_1k": 5000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
        "batch": {
            "input_usd_micros_per_1k": 5000,
            "output_usd_micros_per_1k": 25000,
            "cache_read_input_usd_micros_per_1k": 500,
            "cache_write_input_usd_micros_per_1k": 6250,
        },
    },  # $5/$25 per 1M
    "us.anthropic.claude-sonnet-4-20250514-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 3000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $3/$15 per 1M
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 6000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $6/$15 per 1M
    "us.anthropic.claude-sonnet-4-6": {
        "on_demand": {
            "input_usd_micros_per_1k": 6000,
            "output_usd_micros_per_1k": 15000,
            "cache_read_input_usd_micros_per_1k": 300,
            "cache_write_input_usd_micros_per_1k": 3750,
        },
        "batch": {
            "input_usd_micros_per_1k": 1500,
            "output_usd_micros_per_1k": 7500,
            "cache_read_input_usd_micros_per_1k": 7500,
            "cache_write_input_usd_micros_per_1k": 7500,
        },
    },  # $6/$15 per 1M
    "us.deepseek.r1-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 1350,
            "output_usd_micros_per_1k": 5400,
            "cache_read_input_usd_micros_per_1k": 5400,
            "cache_write_input_usd_micros_per_1k": 5400,
        },
        "batch": {
            "input_usd_micros_per_1k": 1350,
            "output_usd_micros_per_1k": 5400,
            "cache_read_input_usd_micros_per_1k": 5400,
            "cache_write_input_usd_micros_per_1k": 5400,
        },
    },  # $1.35/$5.4 per 1M
    "us.meta.llama3-1-70b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 360,
            "output_usd_micros_per_1k": 720,
            "cache_read_input_usd_micros_per_1k": 720,
            "cache_write_input_usd_micros_per_1k": 720,
        },
        "batch": {
            "input_usd_micros_per_1k": 360,
            "output_usd_micros_per_1k": 720,
            "cache_read_input_usd_micros_per_1k": 720,
            "cache_write_input_usd_micros_per_1k": 720,
        },
    },  # $0.36/$0.72 per 1M
    "us.meta.llama3-1-8b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 220,
            "output_usd_micros_per_1k": 220,
            "cache_read_input_usd_micros_per_1k": 220,
            "cache_write_input_usd_micros_per_1k": 220,
        },
        "batch": {
            "input_usd_micros_per_1k": 220,
            "output_usd_micros_per_1k": 220,
            "cache_read_input_usd_micros_per_1k": 220,
            "cache_write_input_usd_micros_per_1k": 220,
        },
    },  # $0.22/$0.22 per 1M
    "us.meta.llama3-2-11b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 160,
            "output_usd_micros_per_1k": 160,
            "cache_read_input_usd_micros_per_1k": 160,
            "cache_write_input_usd_micros_per_1k": 160,
        },
        "batch": {
            "input_usd_micros_per_1k": 160,
            "output_usd_micros_per_1k": 160,
            "cache_read_input_usd_micros_per_1k": 160,
            "cache_write_input_usd_micros_per_1k": 160,
        },
    },  # $0.16/$0.16 per 1M
    "us.meta.llama3-2-1b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 100,
            "output_usd_micros_per_1k": 100,
            "cache_read_input_usd_micros_per_1k": 100,
            "cache_write_input_usd_micros_per_1k": 100,
        },
        "batch": {
            "input_usd_micros_per_1k": 100,
            "output_usd_micros_per_1k": 100,
            "cache_read_input_usd_micros_per_1k": 100,
            "cache_write_input_usd_micros_per_1k": 100,
        },
    },  # $0.1/$0.1 per 1M
    "us.meta.llama3-2-3b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 150,
            "output_usd_micros_per_1k": 150,
            "cache_read_input_usd_micros_per_1k": 150,
            "cache_write_input_usd_micros_per_1k": 150,
        },
        "batch": {
            "input_usd_micros_per_1k": 150,
            "output_usd_micros_per_1k": 150,
            "cache_read_input_usd_micros_per_1k": 150,
            "cache_write_input_usd_micros_per_1k": 150,
        },
    },  # $0.15/$0.15 per 1M
    "us.meta.llama3-2-90b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 720,
            "output_usd_micros_per_1k": 720,
            "cache_read_input_usd_micros_per_1k": 720,
            "cache_write_input_usd_micros_per_1k": 720,
        },
        "batch": {
            "input_usd_micros_per_1k": 720,
            "output_usd_micros_per_1k": 720,
            "cache_read_input_usd_micros_per_1k": 720,
            "cache_write_input_usd_micros_per_1k": 720,
        },
    },  # $0.72/$0.72 per 1M
    "us.meta.llama3-3-70b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 360,
            "output_usd_micros_per_1k": 360,
            "cache_read_input_usd_micros_per_1k": 360,
            "cache_write_input_usd_micros_per_1k": 360,
        },
        "batch": {
            "input_usd_micros_per_1k": 360,
            "output_usd_micros_per_1k": 360,
            "cache_read_input_usd_micros_per_1k": 360,
            "cache_write_input_usd_micros_per_1k": 360,
        },
    },  # $0.36/$0.36 per 1M
    "us.meta.llama4-maverick-17b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 240,
            "output_usd_micros_per_1k": 485,
            "cache_read_input_usd_micros_per_1k": 485,
            "cache_write_input_usd_micros_per_1k": 485,
        },
        "batch": {
            "input_usd_micros_per_1k": 240,
            "output_usd_micros_per_1k": 485,
            "cache_read_input_usd_micros_per_1k": 485,
            "cache_write_input_usd_micros_per_1k": 485,
        },
    },  # $0.24/$0.485 per 1M
    "us.meta.llama4-scout-17b-instruct-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 85,
            "output_usd_micros_per_1k": 330,
            "cache_read_input_usd_micros_per_1k": 330,
            "cache_write_input_usd_micros_per_1k": 330,
        },
        "batch": {
            "input_usd_micros_per_1k": 85,
            "output_usd_micros_per_1k": 330,
            "cache_read_input_usd_micros_per_1k": 330,
            "cache_write_input_usd_micros_per_1k": 330,
        },
    },  # $0.085/$0.33 per 1M
    "us.mistral.pixtral-large-2502-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 2000,
            "output_usd_micros_per_1k": 6000,
            "cache_read_input_usd_micros_per_1k": 6000,
            "cache_write_input_usd_micros_per_1k": 6000,
        },
        "batch": {
            "input_usd_micros_per_1k": 2000,
            "output_usd_micros_per_1k": 6000,
            "cache_read_input_usd_micros_per_1k": 6000,
            "cache_write_input_usd_micros_per_1k": 6000,
        },
    },  # $2/$6 per 1M
    "us.writer.palmyra-x4-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 2500,
            "output_usd_micros_per_1k": 10000,
            "cache_read_input_usd_micros_per_1k": 10000,
            "cache_write_input_usd_micros_per_1k": 10000,
        },
        "batch": {
            "input_usd_micros_per_1k": 2500,
            "output_usd_micros_per_1k": 10000,
            "cache_read_input_usd_micros_per_1k": 10000,
            "cache_write_input_usd_micros_per_1k": 10000,
        },
    },  # $2.5/$10 per 1M
    "us.writer.palmyra-x5-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 600,
            "output_usd_micros_per_1k": 6000,
            "cache_read_input_usd_micros_per_1k": 6000,
            "cache_write_input_usd_micros_per_1k": 6000,
        },
        "batch": {
            "input_usd_micros_per_1k": 600,
            "output_usd_micros_per_1k": 6000,
            "cache_read_input_usd_micros_per_1k": 6000,
            "cache_write_input_usd_micros_per_1k": 6000,
        },
    },  # $0.6/$6 per 1M
    "writer.palmyra-x4-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 2500,
            "output_usd_micros_per_1k": 10000,
            "cache_read_input_usd_micros_per_1k": 10000,
            "cache_write_input_usd_micros_per_1k": 10000,
        },
        "batch": {
            "input_usd_micros_per_1k": 2500,
            "output_usd_micros_per_1k": 10000,
            "cache_read_input_usd_micros_per_1k": 10000,
            "cache_write_input_usd_micros_per_1k": 10000,
        },
    },  # $2.5/$10 per 1M
    "writer.palmyra-x5-v1:0": {
        "on_demand": {
            "input_usd_micros_per_1k": 600,
            "output_usd_micros_per_1k": 6000,
            "cache_read_input_usd_micros_per_1k": 6000,
            "cache_write_input_usd_micros_per_1k": 6000,
        },
        "batch": {
            "input_usd_micros_per_1k": 600,
            "output_usd_micros_per_1k": 6000,
            "cache_read_input_usd_micros_per_1k": 6000,
            "cache_write_input_usd_micros_per_1k": 6000,
        },
    },  # $0.6/$6 per 1M
    "zai.glm-4.7": {
        "on_demand": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 2200,
            "cache_read_input_usd_micros_per_1k": 2200,
            "cache_write_input_usd_micros_per_1k": 2200,
        },
        "batch": {
            "input_usd_micros_per_1k": 300,
            "output_usd_micros_per_1k": 2200,
            "cache_read_input_usd_micros_per_1k": 2200,
            "cache_write_input_usd_micros_per_1k": 2200,
        },
    },  # $0.3/$2.2 per 1M
    "zai.glm-4.7-flash": {
        "on_demand": {
            "input_usd_micros_per_1k": 70,
            "output_usd_micros_per_1k": 400,
            "cache_read_input_usd_micros_per_1k": 400,
            "cache_write_input_usd_micros_per_1k": 400,
        },
        "batch": {
            "input_usd_micros_per_1k": 70,
            "output_usd_micros_per_1k": 400,
            "cache_read_input_usd_micros_per_1k": 400,
            "cache_write_input_usd_micros_per_1k": 400,
        },
    },  # $0.07/$0.4 per 1M
    "zai.glm-5": {
        "on_demand": {
            "input_usd_micros_per_1k": 1000,
            "output_usd_micros_per_1k": 1600,
            "cache_read_input_usd_micros_per_1k": 1600,
            "cache_write_input_usd_micros_per_1k": 1600,
        },
        "batch": {
            "input_usd_micros_per_1k": 1000,
            "output_usd_micros_per_1k": 1600,
            "cache_read_input_usd_micros_per_1k": 1600,
            "cache_write_input_usd_micros_per_1k": 1600,
        },
    },  # $1/$1.6 per 1M
}

# Fallback for unknown models — Opus 4 rates (most expensive known tier).
# Using a high fallback means unknown models are over-counted rather than
# under-counted, which protects budget limits.
logger = logging.getLogger(__name__)

PricingRates = dict[str, int]
PricingModeMap = dict[str, PricingRates]
PricingMap = dict[str, PricingModeMap]

_FALLBACK_BY_MODE: dict[str, PricingRates] = {
    "on_demand": {
        "input_usd_micros_per_1k": 15_000,
        "output_usd_micros_per_1k": 75_000,
        "cache_read_input_usd_micros_per_1k": 75_000,
        "cache_write_input_usd_micros_per_1k": 75_000,
    },
    "batch": {
        "input_usd_micros_per_1k": 15_000,
        "output_usd_micros_per_1k": 75_000,
        "cache_read_input_usd_micros_per_1k": 75_000,
        "cache_write_input_usd_micros_per_1k": 75_000,
    },
}

_RATE_KEYS = (
    "input_usd_micros_per_1k",
    "output_usd_micros_per_1k",
    "cache_read_input_usd_micros_per_1k",
    "cache_write_input_usd_micros_per_1k",
)
_PRICING_CACHE: PricingMap | None = None
_PRICING_CACHE_RAW: str | None = None


def _normalize_rates(rates: dict[str, int]) -> PricingRates:
    input_rate = int(
        rates.get(
            "input_usd_micros_per_1k", _FALLBACK_BY_MODE["on_demand"]["input_usd_micros_per_1k"]
        )
    )
    output_rate = int(
        rates.get(
            "output_usd_micros_per_1k", _FALLBACK_BY_MODE["on_demand"]["output_usd_micros_per_1k"]
        )
    )
    return {
        "input_usd_micros_per_1k": input_rate,
        "output_usd_micros_per_1k": output_rate,
        "cache_read_input_usd_micros_per_1k": int(
            rates.get("cache_read_input_usd_micros_per_1k", output_rate)
        ),
        "cache_write_input_usd_micros_per_1k": int(
            rates.get("cache_write_input_usd_micros_per_1k", output_rate)
        ),
    }


def _normalize_pricing_map(raw_map: dict) -> PricingMap:
    normalized: PricingMap = {}
    for model_id, value in raw_map.items():
        if not isinstance(value, dict):
            continue
        if "on_demand" in value or "batch" in value:
            mode_map: PricingModeMap = {}
            if "on_demand" in value:
                mode_map["on_demand"] = _normalize_rates(value.get("on_demand", {}))
            if "batch" in value:
                mode_map["batch"] = _normalize_rates(value.get("batch", {}))
            normalized[model_id] = mode_map
            continue
        else:
            on_demand = _normalize_rates(value)
            normalized[model_id] = {
                "on_demand": on_demand,
                "batch": {
                    "input_usd_micros_per_1k": max(1, on_demand["input_usd_micros_per_1k"] // 2),
                    "output_usd_micros_per_1k": max(1, on_demand["output_usd_micros_per_1k"] // 2),
                    "cache_read_input_usd_micros_per_1k": max(
                        1, on_demand["cache_read_input_usd_micros_per_1k"] // 2
                    ),
                    "cache_write_input_usd_micros_per_1k": max(
                        1, on_demand["cache_write_input_usd_micros_per_1k"] // 2
                    ),
                },
            }
    return normalized


def _load_pricing() -> PricingMap:
    global _PRICING_CACHE, _PRICING_CACHE_RAW
    raw = os.environ.get("PRICING_JSON")
    if raw:
        if _PRICING_CACHE is not None and _PRICING_CACHE_RAW == raw:
            return _PRICING_CACHE
        _PRICING_CACHE = _normalize_pricing_map(json.loads(raw))
        _PRICING_CACHE_RAW = raw
        return _PRICING_CACHE
    if _PRICING_CACHE is None or _PRICING_CACHE_RAW is not None:
        _PRICING_CACHE = _normalize_pricing_map(DEFAULT_PRICING)
        _PRICING_CACHE_RAW = None
    return _PRICING_CACHE


def _resolve_rates(model_id: str, pricing_mode: str) -> tuple[PricingRates, bool, list[str]]:
    pricing = _load_pricing()
    fallback_dimensions: list[str] = []
    fallback_applied = False

    model = pricing.get(model_id)
    if not model:
        model = _FALLBACK_BY_MODE
        fallback_applied = True
        fallback_dimensions.append("model_id")
        logger.warning(
            json.dumps(
                {
                    "event": "pricing_fallback_rate",
                    "model_id": model_id,
                    "pricing_mode": pricing_mode,
                    "token_class": "model_id",
                    "applied_fallback_rate": None,
                }
            )
        )

    mode_rates = model.get(pricing_mode)
    if not mode_rates:
        mode_rates = _FALLBACK_BY_MODE[pricing_mode]
        fallback_applied = True
        fallback_dimensions.append("pricing_mode")
        logger.warning(
            json.dumps(
                {
                    "event": "pricing_fallback_rate",
                    "model_id": model_id,
                    "pricing_mode": pricing_mode,
                    "token_class": "pricing_mode",
                    "applied_fallback_rate": None,
                }
            )
        )

    resolved: PricingRates = {}
    for key in _RATE_KEYS:
        if key in mode_rates:
            resolved[key] = int(mode_rates[key])
            continue
        resolved[key] = _FALLBACK_BY_MODE[pricing_mode][key]
        fallback_applied = True
        fallback_dimensions.append(key)
        logger.warning(
            json.dumps(
                {
                    "event": "pricing_fallback_rate",
                    "model_id": model_id,
                    "pricing_mode": pricing_mode,
                    "token_class": key,
                    "applied_fallback_rate": resolved[key],
                }
            )
        )
    return resolved, fallback_applied, sorted(set(fallback_dimensions))


def compute_cost(
    model_id: str,
    pricing_mode: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_write_input_tokens: int,
) -> tuple[int, dict[str, int], bool, list[str], PricingRates]:
    """Return (total_cost, component_costs, fallback_applied, fallback_dimensions, rates)."""
    rates, fallback_applied, fallback_dimensions = _resolve_rates(model_id, pricing_mode)
    components = {
        "input_usd_micros": (input_tokens * rates["input_usd_micros_per_1k"]) // 1000,
        "output_usd_micros": (output_tokens * rates["output_usd_micros_per_1k"]) // 1000,
        "cache_read_input_usd_micros": (
            cache_read_input_tokens * rates["cache_read_input_usd_micros_per_1k"]
        )
        // 1000,
        "cache_write_input_usd_micros": (
            cache_write_input_tokens * rates["cache_write_input_usd_micros_per_1k"]
        )
        // 1000,
    }
    return sum(components.values()), components, fallback_applied, fallback_dimensions, rates

from scripts.gen_pricing import _parse_fm_api


def test_parse_fm_api_captures_modes_and_cache():
    data = {
        "products": {
            "a": {
                "attributes": {
                    "regionCode": "us-east-1",
                    "servicename": "svc",
                    "usagetype": "InputTokenCount",
                }
            },
            "b": {
                "attributes": {
                    "regionCode": "us-east-1",
                    "servicename": "svc",
                    "usagetype": "OutputTokenCount",
                }
            },
            "c": {
                "attributes": {
                    "regionCode": "us-east-1",
                    "servicename": "svc",
                    "usagetype": "BatchInputTokenCount",
                }
            },
            "d": {
                "attributes": {
                    "regionCode": "us-east-1",
                    "servicename": "svc",
                    "usagetype": "BatchOutputTokenCount",
                }
            },
            "e": {
                "attributes": {
                    "regionCode": "us-east-1",
                    "servicename": "svc",
                    "usagetype": "CacheReadInputTokenCount",
                }
            },
            "f": {
                "attributes": {
                    "regionCode": "us-east-1",
                    "servicename": "svc",
                    "usagetype": "CacheWriteInputTokenCount",
                }
            },
        },
        "terms": {
            "OnDemand": {
                sku: {"x": {"priceDimensions": {"y": {"pricePerUnit": {"USD": str(v)}}}}}
                for sku, v in {
                    "a": 1.0,
                    "b": 2.0,
                    "c": 0.5,
                    "d": 1.0,
                    "e": 0.2,
                    "f": 0.4,
                }.items()
            }
        },
    }
    parsed = _parse_fm_api(data)
    assert parsed["svc"]["on_demand"]["input"] == 1.0
    assert parsed["svc"]["on_demand"]["output"] == 2.0
    assert parsed["svc"]["batch"]["input"] == 0.5
    assert parsed["svc"]["batch"]["output"] == 1.0
    assert parsed["svc"]["on_demand"]["cache_read_input"] == 0.2
    assert parsed["svc"]["on_demand"]["cache_write_input"] == 0.4

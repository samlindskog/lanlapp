"""Place finders: one module per source of businesses around a point (a geographic scan's
first step). Like sources/ for boards: names() lists them, load(name) imports one, and each
module keeps to the contract below (tests/test_geo.py injects a fake one the same way).

Finder contract:

    NAME             the module name ("osm")
    DISPLAY_NAME     shown in the scan dialog ("OpenStreetMap (Overpass)")
    configured()     -> bool   whether the service it needs is reachable from this environment
                                (an env var is set, or nothing is needed)
    find(center, radius_m, categories, keywords, tags, log) -> [place]
        center       (lat, lon)
        radius_m     search radius in metres
        categories   registry keys (core/geo.py CATEGORIES); the finder maps each to its own query
        keywords     a string to match against place names, or None
        tags         raw source-specific selector lines the user typed (OSM: tag filters), or []
        log          line logger (the run's log)
    place            {"name", "lat", "lon", "address", "city", "state", "phone", "website",
                      "category": the registry key that found it, "finder_id": the source's own id}
                     name, lat and lon are required; the rest may be None.

A finder talks to its service directly (core/client.py), paces itself, and never touches the
run's browser Session: the businesses' own sites are fetched later by the pipeline.
"""
import importlib
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def names():
    return sorted(f[:-3] for f in os.listdir(HERE) if f.endswith(".py") and not f.startswith("_"))


def load(name):
    return importlib.import_module(f"finders.{name}")


def configured():
    """{name: bool} for every finder."""
    return {n: bool(load(n).configured()) for n in names()}

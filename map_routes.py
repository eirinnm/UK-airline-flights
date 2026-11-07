"""Render passenger routes wholly within the British Isles."""

from __future__ import annotations

import json
import urllib.request
import zipfile
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import LineCollection
from matplotlib.colors import Normalize
from matplotlib.font_manager import FontProperties
from matplotlib.patheffects import withStroke
from pyproj import Geod, Transformer
from shapely.geometry import Point, box
from shapely.ops import unary_union
from shapely.prepared import prep


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUTPUT = ROOT / "british_isles_air_routes.png"
ROUTES_URL = "https://raw.githubusercontent.com/Jonty/airline-route-data/main/airline_routes.json"
NE_URL = "https://naturalearth.s3.amazonaws.com/10m_cultural/ne_10m_admin_0_countries.zip"


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "uk-air-routes-map/1.0"})
    with urllib.request.urlopen(request, timeout=90) as response, target.open("wb") as out:
        out.write(response.read())


def load_geography():
    routes_path = DATA / "airline_routes.json"
    if not routes_path.exists():
        download(ROUTES_URL, routes_path)
    countries_zip = DATA / "ne_10m_admin_0_countries.zip"
    if not countries_zip.exists():
        download(NE_URL, countries_zip)
    shp_dir = DATA / "natural_earth"
    shp_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(countries_zip) as archive:
        archive.extractall(shp_dir)
    world = gpd.read_file(next(shp_dir.glob("*.shp")))
    return json.loads(routes_path.read_text(encoding="utf-8")), world


def main() -> None:
    print("Loading source routes and coastlines…", flush=True)
    airports, world = load_geography()
    print("Selecting island coastlines…", flush=True)
    # Include Ireland, Great Britain, Northern Ireland, Isle of Man, and both
    # Channel Islands. Natural Earth encodes the Crown Dependencies separately.
    names = {"United Kingdom", "Ireland", "Isle of Man", "Guernsey", "Jersey"}
    land = world[world["ADMIN"].isin(names)].copy()
    if land.empty:
        raise RuntimeError("Could not find the British Isles in Natural Earth boundaries")
    land.geometry = land.geometry.simplify(0.008, preserve_topology=True)
    islands = unary_union(land.geometry)
    island_area = prep(islands.buffer(0.035))
    print("Finding airports on the islands…", flush=True)

    positions: dict[str, tuple[float, float]] = {}
    for code, airport in airports.items():
        try:
            lon, lat = float(airport["longitude"]), float(airport["latitude"])
        except (KeyError, TypeError, ValueError):
            continue
        # A small buffer admits airport grounds just offshore while excluding
        # destinations in mainland Europe. Buffer distance is in projected metres.
        point = Point(lon, lat)
        if island_area.covers(point):
            positions[code] = (lon, lat)

    routes: dict[tuple[str, str], dict[str, float | int]] = {}
    for origin, airport in airports.items():
        if origin not in positions:
            continue
        for route in airport.get("routes", []):
            destination = route.get("iata")
            if destination in positions and destination != origin:
                key = (origin, destination)
                routes[key] = {
                    "distance_km": float(route.get("km", 0)),
                }
    if not routes:
        raise RuntimeError("No routes within the British Isles were found in the dataset")
    print(f"Prepared {len(positions)} airports and {len(routes)} routes", flush=True)

    fig = plt.figure(figsize=(12, 18), facecolor="#000000", dpi=220)
    ax = fig.add_axes([0.025, 0.025, 0.95, 0.95], facecolor="#000000")
    projection = "EPSG:27700"  # British National Grid (OSGB36 / Transverse Mercator)
    projected_land = land.to_crs(projection)
    context_region = box(-25, 35, 25, 70)
    context_land = world.loc[world.geometry.intersects(context_region)].copy()
    context_land.geometry = context_land.geometry.intersection(context_region)
    context_land.geometry = context_land.geometry.simplify(0.008, preserve_topology=True)
    projected_context_land = context_land.to_crs(projection)
    projected_context_land.plot(ax=ax, facecolor="#000000", edgecolor="#ffffff",
                                linewidth=0.85, zorder=5)

    # Great-circle arcs are computed geodesically, then projected to Mercator.
    transformer = Transformer.from_crs("EPSG:4326", projection, always_xy=True)
    geod = Geod(a=6_371_008.8, f=0)  # spherical great circles
    route_paths = []
    route_distances = []
    bounds_x = [projected_land.total_bounds[0], projected_land.total_bounds[2]]
    bounds_y = [projected_land.total_bounds[1], projected_land.total_bounds[3]]
    for (origin, destination), details in routes.items():
        a, b = positions[origin], positions[destination]
        _, _, distance_m = geod.inv(a[0], a[1], b[0], b[1])
        arc = [a, *geod.npts(a[0], a[1], b[0], b[1], max(1, int(distance_m / 20_000))), b]
        lon, lat = np.asarray(arc).T
        px, py = transformer.transform(lon, lat)
        route_paths.append(np.column_stack((px, py)))
        route_distances.append(details["distance_km"] or distance_m / 1000)
        bounds_x.extend(px)
        bounds_y.extend(py)

    # Include every coastline and route endpoint with a small visual margin.
    pad_m = 70_000
    xmin, xmax = min(bounds_x) - pad_m, max(bounds_x) + pad_m
    ymin, ymax = min(bounds_y) - pad_m, max(bounds_y) + pad_m
    # Dark graticules are placed underneath the land fill: visible over water,
    # but hidden by the black island shapes.
    grid_paths = []
    for latitude in range(48, 63, 2):
        longitudes = np.linspace(-15, 5, 240)
        gx, gy = transformer.transform(longitudes, np.full_like(longitudes, latitude))
        grid_paths.append(np.column_stack((gx, gy)))
    for longitude in range(-14, 5, 2):
        latitudes = np.linspace(47, 63, 200)
        gx, gy = transformer.transform(np.full_like(latitudes, longitude), latitudes)
        grid_paths.append(np.column_stack((gx, gy)))
    ax.add_collection(LineCollection(grid_paths, colors="#09204d", linewidths=0.65,
                                     alpha=0.9, zorder=1))

    route_collection = LineCollection(
        route_paths,
        cmap="turbo",
        norm=Normalize(vmin=min(route_distances), vmax=max(route_distances)),
        linewidths=1.0,
        alpha=0.82,
        capstyle="round",
        zorder=7,
    )
    route_collection.set_array(np.asarray(route_distances))
    ax.add_collection(route_collection)

    # Small bright airport markers sit above the route strokes. Labels use a
    # dark halo to remain legible where they cross the white coastlines.
    projected_airports = {
        code: transformer.transform(lon, lat)
        for code, (lon, lat) in positions.items()
    }
    airport_xy = np.asarray(list(projected_airports.values()))
    ax.scatter(airport_xy[:, 0], airport_xy[:, 1], s=10, c="#ffffff",
               edgecolors="#050505", linewidths=0.45, zorder=9)
    for code, (x, y) in projected_airports.items():
        ax.annotate(
            code, (x, y), xytext=(0, 0), textcoords="offset points",
            ha="center", va="center",
            color="white", fontsize=7.8, fontweight="bold", zorder=10,
            path_effects=[withStroke(linewidth=1.5, foreground="#000000")],
        )

    title_font = FontProperties(family="Arial Nova", weight=300)
    title_lines = [
        ("Passenger Air Routes of Britain and Ireland", 0.985, 27),
        ("Including the Isle of Man and Channel Islands", 0.946, 17),
    ]
    for text, y, size in title_lines:
        ax.text(
            0.015, y, text, transform=ax.transAxes, ha="left", va="top",
            color="#d0d0d0", fontsize=size, fontproperties=title_font, zorder=11,
        )

    # Compact horizontal distance legend directly beneath the title block.
    colorbar_ax = ax.inset_axes([0.015, 0.055, 0.30, 0.006])
    colorbar_ax.set_facecolor("#000000")
    colorbar = fig.colorbar(route_collection, cax=colorbar_ax, orientation="horizontal")
    colorbar.set_ticks(np.linspace(min(route_distances), max(route_distances), 3))
    colorbar.ax.set_xticklabels([f"{value:.0f}" for value in np.linspace(
        min(route_distances), max(route_distances), 3
    )])
    colorbar.ax.tick_params(colors="#d0d0d0", labelsize=8, length=2, pad=2)
    colorbar.outline.set_visible(False)
    colorbar.set_label("Route distance (km)", color="#d0d0d0", fontsize=9, labelpad=1)

    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")
    fig.savefig(OUTPUT, facecolor="#000000", dpi=220, bbox_inches="tight", pad_inches=0.08)
    print(f"Saved {OUTPUT}")
    print(f"Airports included: {len(positions)}; directional routes: {len(routes)}")
    print(f"Geographies: {', '.join(sorted(names))}")
    route_summary = [
        (distance_km, origin, destination)
        for ((origin, destination), _details), distance_km
        in zip(routes.items(), route_distances)
    ]
    for heading, ranked in (
        ("3 shortest routes:", sorted(route_summary)),
        ("3 longest routes:", sorted(route_summary, reverse=True)),
    ):
        print(heading)
        for distance_km, origin, destination in ranked[:3]:
            print(f"  {origin} → {destination}: {distance_km:,.0f} km")


if __name__ == "__main__":
    main()

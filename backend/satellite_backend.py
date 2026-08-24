import os

# ------------------------------------------------------------
# PROJ DATABASE FIX
# ------------------------------------------------------------
#
# The PostgreSQL/PostGIS installer sets the system environment
# variable PROJ_LIB to its bundled contrib proj directory. That
# proj.db uses database layout version 2, which modern PROJ
# rejects (>= 6 required), so every EPSG lookup through rasterio
# (e.g. CRS.from_epsg(4326)) fails with "The EPSG code is
# unknown". Unset it BEFORE importing rasterio so rasterio falls
# back to its own bundled database.

os.environ.pop("PROJ_LIB", None)

# ------------------------------------------------------------
# GDAL HTTP TIMEOUTS
# ------------------------------------------------------------
#
# COG band reads go over HTTPS. Without timeouts, a stalled
# connection can hang a rasterio read indefinitely, which would
# hang the whole satellite search (sequential or concurrent).
# Bound each read so a stalled download fails fast and the
# scene is simply skipped by the existing error handling.

os.environ.setdefault("GDAL_HTTP_TIMEOUT", "60")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "1")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "1")

# ------------------------------------------------------------
# GDAL VSICURL / COG CONFIG
# ------------------------------------------------------------
#
# Remote COG reads over /vsicurl/ pay one extra HTTP round-trip
# per open when GDAL lists the directory first; COGs carry their
# own IFD/overview pointers, so the listing is useless. Disable
# it. VSI_CACHE keeps downloaded COG blocks in memory so repeated
# reads of the same window (e.g. /metadata then /rgb) do not
# re-download.

os.environ.setdefault("GDAL_DISABLE_READDIR_BEFORE_OPEN", "YES")
os.environ.setdefault("GDAL_DISABLE_READDIR_BEFORE_VIEW", "YES")
os.environ.setdefault("VSI_CACHE", "TRUE")
os.environ.setdefault("VSI_CACHE_SIZE", "67108864")

import math
import json
import time
from collections import Counter
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import rasterio
from rasterio.mask import mask
from rasterio.enums import Resampling

import pystac_client
import planetary_computer
from shapely.geometry import mapping, shape
from rasterio.warp import calculate_default_transform, reproject, transform_bounds
from rasterio.warp import Resampling
from rasterio.crs import CRS
from rasterio.transform import from_origin
from rasterio.features import rasterize
from rasterio.windows import from_bounds, Window, bounds as window_bounds

# ------------------------------------------------------------
# SCENE RESULT CACHE
# ------------------------------------------------------------
#
# /api/scene/{id}/metadata and /api/scene/{id}/rgb both call
# load_satellite_rgb(), which downloads and processes the three
# display bands (tens of seconds). Memoize the display result
# per scene so the second request reuses the first computation:
# one band download per scene selection instead of two. Scene
# data is immutable, so cached results never go stale.
# Exceptions are not cached, so transient failures retry.
# maxsize bounds memory (~40 MB per entry: 1800x1800x3 float32).
# 8 slots: a multi-tile observation loads 2-3 tiles at once and
# the previous observation's entries should stay warm for quick
# back-switching.

# Maximum display size for served imagery (longest side, pixels).
# Applied to the READ WINDOW (full tile or AOI window), so the
# displayed pixel density over the AOI stays the same as before.

MAX_DISPLAY_SIZE = 1800


# ============================================================
# STAC CATALOG / COLLECTION REUSE
# ============================================================
#
# One lazy, cached Planetary Computer catalog client per process
# (lru_cache is thread-safe — concurrent misses are serialized, so
# the catalog is opened exactly once per variant). The plain client
# serves metadata/RGB collection+item lookups; the search client
# carries the sign_inplace modifier exactly like the previous
# per-search open did (signing behavior unchanged). Collections are
# cached per id. All usage is READ-ONLY (plain GETs over urllib3's
# thread-safe connection pool); item signing via
# planetary_computer.sign()/sign_inplace() is always per item.

STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"


@lru_cache(maxsize=1)
def _get_catalog():
    return pystac_client.Client.open(STAC_API_URL)


@lru_cache(maxsize=1)
def _get_search_catalog():
    return pystac_client.Client.open(
        STAC_API_URL,
        modifier=planetary_computer.sign_inplace
    )


@lru_cache(maxsize=4)
def _get_collection(collection_id):
    return _get_catalog().get_collection(collection_id)


def _aoi_read_window(
    src,
    aoi_json
):
    """
    Rasterio window covering the AOI (plus a small 2% margin) in
    the dataset's native CRS, or None.

    None means "no AOI given / invalid / no intersection" and the
    caller falls back to reading the FULL raster — the original
    pre-optimization behavior.

    The window is computed per TILE: the AOI is transformed into
    each tile's own CRS and intersected with that tile's raster,
    so multi-tile observations stay correct (each tile reads only
    its own AOI-relevant pixels).
    """
    if not aoi_json:
        return None

    try:

        geometry = shape(
            json.loads(aoi_json)
        )

    except Exception:

        return None

    if geometry.is_empty or not geometry.is_valid:

        return None

    try:

        minx, miny, maxx, maxy = transform_bounds(
            CRS.from_epsg(4326),
            src.crs,
            *geometry.bounds
        )

    except Exception:

        return None

    margin_x = (maxx - minx) * 0.02

    margin_y = (maxy - miny) * 0.02

    window = from_bounds(
        minx - margin_x,
        miny - margin_y,
        maxx + margin_x,
        maxy + margin_y,
        src.transform
    )

    # Clamp to the raster extent (window may extend past the edge).
    window = window.intersection(
        Window(
            0,
            0,
            src.width,
            src.height
        )
    )

    if window.width < 2 or window.height < 2:

        return None

    return window


@lru_cache(maxsize=16)
def load_satellite_metadata(
    scene_id,
    collection_id,
    aoi_json=None
):
    """
    Cheap metadata for one scene.

    Opens ONLY ONE band's COG header (no pixel data is read or
    downloaded) and returns the display bounds/width/height/crs of
    the read window (full tile when no AOI is given, otherwise the
    AOI window — identical geometry to what /rgb will serve).

    This replaces the previous behavior where /metadata called
    load_satellite_rgb() and downloaded/processed all three RGB
    bands just to return bounds.
    """

    collection = _get_collection(
        collection_id
    )

    item = collection.get_item(
        scene_id
    )

    if item is None:

        raise ValueError(
            f"Could not find scene: {scene_id}"
        )

    item = planetary_computer.sign(
        item
    )

    red_key = (
        "B04"
        if collection_id == "sentinel-2-l2a"
        else "red"
    )

    with rasterio.open(
        item.assets[red_key].href
    ) as src:

        crs = src.crs

        window = _aoi_read_window(
            src,
            aoi_json
        )

        if window is None:

            window = Window(
                0,
                0,
                src.width,
                src.height
            )

        win_left, win_bottom, win_right, win_top = (
            window_bounds(
                window,
                src.transform
            )
        )

        scale = min(
            1.0,
            MAX_DISPLAY_SIZE
            / max(
                window.width,
                window.height
            )
        )

        out_width = max(
            1,
            int(window.width * scale)
        )

        out_height = max(
            1,
            int(window.height * scale)
        )

    if crs is not None:

        left, bottom, right, top = transform_bounds(
            crs,
            CRS.from_epsg(4326),
            win_left,
            win_bottom,
            win_right,
            win_top
        )

        display_bounds = [
            bottom,
            top,
            left,
            right
        ]

    else:

        display_bounds = [
            win_bottom,
            win_top,
            win_left,
            win_right
        ]

    return {
        "bounds": display_bounds,
        "width": out_width,
        "height": out_height,
        "crs": "EPSG:4326"
    }


@lru_cache(maxsize=8)
def load_satellite_rgb(
    scene_id,
    collection_id,
    aoi_json=None
):
    """
    Load a selected satellite scene as a true-color RGB image.

    IMPORTANT:
    This function is for VIEWING.

    The read window is the FULL tile when no AOI is given, or the
    AOI window (per-tile, in the tile's own CRS) when the frontend
    passes the AOI — reducing remote COG reads to only the pixels
    that can contribute to the requested area. The returned bounds
    ALWAYS correspond to the actual served window, so the overlay
    is positioned exactly.

    The three band reads run CONCURRENTLY (max_workers=3); each
    thread opens its own dataset handle, so rasterio/GDAL
    thread-safety is preserved.

    Sentinel-2:
        Red   = B04
        Green = B03
        Blue  = B02

    Landsat 8/9:
        Red   = red
        Green = green
        Blue  = blue
    """

    # --------------------------------------------------------
    # CONNECT TO PLANETARY COMPUTER
    # --------------------------------------------------------

    # --------------------------------------------------------
    # GET SELECTED SCENE
    # --------------------------------------------------------

    collection = _get_collection(
        collection_id
    )

    item = collection.get_item(
        scene_id
    )

    if item is None:

        raise ValueError(
            f"Could not find scene: {scene_id}"
        )


    # --------------------------------------------------------
    # SIGN ASSETS
    # --------------------------------------------------------

    item = planetary_computer.sign(
        item
    )


    # --------------------------------------------------------
    # SELECT RGB BANDS
    # --------------------------------------------------------

    if collection_id == "sentinel-2-l2a":

        red_key = "B04"
        green_key = "B03"
        blue_key = "B02"

    elif collection_id == "landsat-c2-l2":

        red_key = "red"
        green_key = "green"
        blue_key = "blue"

    else:

        raise ValueError(
            f"Unsupported collection: "
            f"{collection_id}"
        )


    # --------------------------------------------------------
    # GET ASSET URLS
    # --------------------------------------------------------

    red_href = item.assets[
        red_key
    ].href

    green_href = item.assets[
        green_key
    ].href

    blue_href = item.assets[
        blue_key
    ].href


    # --------------------------------------------------------
    # READ WINDOW PLAN (header-only open of the red band)
    # --------------------------------------------------------
    #
    # Compute the window (full tile or AOI window) and the
    # display out_shape from the COG header WITHOUT reading
    # pixel data. The three band reads below then download
    # only the window's pixels.

    with rasterio.open(
        red_href
    ) as src:

        crs = src.crs

        window = _aoi_read_window(
            src,
            aoi_json
        )

        if window is None:

            window = Window(
                0,
                0,
                src.width,
                src.height
            )

        win_left, win_bottom, win_right, win_top = (
            window_bounds(
                window,
                src.transform
            )
        )

        scale = min(
            1.0,
            MAX_DISPLAY_SIZE
            / max(
                window.width,
                window.height
            )
        )

        out_width = max(
            1,
            int(window.width * scale)
        )

        out_height = max(
            1,
            int(window.height * scale)
        )


    # --------------------------------------------------------
    # READ THE THREE BANDS CONCURRENTLY
    # --------------------------------------------------------
    #
    # Each worker opens its OWN dataset handle (rasterio
    # datasets are not safe for concurrent reads on the SAME
    # handle; separate handles per thread are — the same
    # pattern the concurrent search already uses). Bounded:
    # exactly 3 workers, one per band.

    def read_band(href):

        with rasterio.open(
            href
        ) as band_src:

            return band_src.read(
                1,
                window=window,
                out_shape=(
                    out_height,
                    out_width
                ),
                resampling=Resampling.bilinear
            )

    with ThreadPoolExecutor(
        max_workers=3
    ) as pool:

        futures = [
            pool.submit(
                read_band,
                href
            )
            for href in (
                red_href,
                green_href,
                blue_href
            )
        ]

        red, green, blue = [
            future.result()
            for future in futures
        ]


    # --------------------------------------------------------
    # STACK RGB
    # --------------------------------------------------------

    rgb = np.stack(
        [
            red,
            green,
            blue
        ],
        axis=-1
    )


    # --------------------------------------------------------
    # STRETCH FOR DISPLAY
    # --------------------------------------------------------

    rgb = stretch_rgb(
        rgb
    )


    # --------------------------------------------------------
    # CONVERT WINDOW BOUNDS TO WGS84 (EPSG:4326)
    # --------------------------------------------------------
    #
    # The bounds correspond to the ACTUAL served window (full
    # tile or AOI window), never a stale full-tile extent.

    if crs is not None:

        left, bottom, right, top = transform_bounds(
            crs,
            CRS.from_epsg(4326),
            win_left,
            win_bottom,
            win_right,
            win_top
        )

        display_bounds = [
            bottom,
            top,
            left,
            right
        ]

    else:

        display_bounds = [
            win_bottom,
            win_top,
            win_left,
            win_right
        ]


    # --------------------------------------------------------
    # RETURN DISPLAY DATA
    # --------------------------------------------------------

    return {

        "rgb": rgb,

        "bounds": display_bounds,

        "width": out_width,

        "height": out_height,

        "crs": "EPSG:4326"

    }

def stretch_rgb(rgb):
    """
    Convert raw satellite RGB values into
    display-ready 0-255 RGBA values.

    Pixels with no valid data in any band
    get alpha 0 (fully transparent).
    """

    rgb = rgb.astype(
        np.float32
    )

    output = np.zeros_like(
        rgb
    )

    any_valid = np.zeros(
        (
            rgb.shape[0],
            rgb.shape[1]
        ),
        dtype=bool
    )

    for band in range(3):

        values = rgb[:, :, band]

        valid = (
            np.isfinite(values)
            & (values > 0)
        )

        if not np.any(valid):
            continue

        any_valid = any_valid | valid

        low = np.percentile(
            values[valid],
            2
        )

        high = np.percentile(
            values[valid],
            98
        )

        if high <= low:
            continue

        stretched = (
            values - low
        ) / (
            high - low
        )

        stretched = np.clip(
            stretched,
            0,
            1
        )

        output[:, :, band] = (
            stretched * 255
        )

    # --------------------------------------------------------
    # ALPHA: pixels with no valid data in ANY band become
    # fully transparent (Sentinel-2/Landsat store no-data
    # as 0 outside the scene footprint).
    # --------------------------------------------------------

    alpha = np.where(
        any_valid,
        255,
        0
    ).astype(
        np.uint8
    )

    return np.dstack([
        output.astype(np.uint8),
        alpha
    ])

# ============================================================
# SETTINGS
# ============================================================

START_DATE = "2026-01-01"
END_DATE = "2026-12-31"

MAX_SCENE_CLOUD = 50.0
MAX_AOI_CLOUD = 20.0

# Bounded concurrency for the per-scene AOI-cloud processing.
# The work is network-bound (downloading quality COGs), so a
# small pool overlaps the slow downloads without flooding
# Planetary Computer with requests.
MAX_SEARCH_WORKERS = 8

COLLECTIONS = [
    "sentinel-2-l2a",
    "landsat-c2-l2"
]


# ============================================================
# SENTINEL-2 SCL CLOUD CLASSES
# ============================================================

SENTINEL_CLOUD_CLASSES = {
    3,   # Cloud shadow
    8,   # Cloud medium probability
    9,   # Cloud high probability
    10   # Thin cirrus
}


# ============================================================
# LANDSAT QA_PIXEL BITS
# ============================================================

LANDSAT_DILATED_CLOUD_BIT = 1
LANDSAT_CIRRUS_BIT = 2
LANDSAT_CLOUD_BIT = 3
LANDSAT_CLOUD_SHADOW_BIT = 4


# ============================================================
# SATELLITE NAME
# ============================================================

def get_satellite_name(item):

    collection = item.collection_id

    if collection == "sentinel-2-l2a":
        return "Sentinel-2"

    if collection == "landsat-c2-l2":

        platform = item.properties.get(
            "platform",
            ""
        )

        if platform == "landsat-8":
            return "Landsat 8"

        if platform == "landsat-9":
            return "Landsat 9"

        return "Landsat"

    return collection


# ============================================================
# RESOLUTION
# ============================================================

def get_resolution(satellite):

    if satellite == "Sentinel-2":
        return 10

    if satellite in [
        "Landsat 8",
        "Landsat 9"
    ]:
        return 30

    return None


# ============================================================
# BIT HELPER
# ============================================================

def bit_is_set(array, bit):

    return (
        (array & (1 << bit)) != 0
    )


# ============================================================
# SENTINEL-2 AOI CLOUD
# ============================================================

def calculate_sentinel_aoi_cloud(
    item,
    aoi_gdf
):

    signed_item = planetary_computer.sign(
        item
    )

    if "SCL" not in signed_item.assets:

        raise RuntimeError(
            f"SCL asset not found for {item.id}"
        )

    scl_asset = signed_item.assets[
        "SCL"
    ]

    with rasterio.open(
        scl_asset.href
    ) as src:

        # Mask-raster pixel size in m^2. SCL is delivered at 20 m
        # for Sentinel-2 L2A (NOT the 10 m display bands), so any
        # coverage calculation must use THIS pixel size, not the
        # product resolution.
        pixel_area_m2 = float(
            src.res[0] * src.res[1]
        )

        aoi_scl = aoi_gdf.to_crs(
            src.crs
        )

        shapes = [
            geometry.__geo_interface__
            for geometry
            in aoi_scl.geometry
        ]

        clipped, clipped_transform = mask(
            src,
            shapes,
            crop=True,
            nodata=0
        )

        scl = clipped[0]

    valid_mask = (
        scl != 0
    )

    valid_pixels = int(
        np.sum(valid_mask)
    )

    if valid_pixels == 0:

        raise RuntimeError(
            f"No valid SCL pixels "
            f"inside AOI for {item.id}"
        )

    cloud_mask = np.isin(
        scl,
        list(SENTINEL_CLOUD_CLASSES)
    )

    cloud_pixels = int(
        np.sum(
            cloud_mask & valid_mask
        )
    )

    return {
        "pct": (
            cloud_pixels
            / valid_pixels
            * 100.0
        ),
        "valid_pixels": valid_pixels,
        "cloudy_pixels": cloud_pixels,
        "pixel_area_m2": pixel_area_m2,
        "valid_mask": valid_mask,
        "cloudy_mask": (
            cloud_mask
            & valid_mask
        ),
        "transform": clipped_transform,
        "crs": src.crs
    }


# ============================================================
# LANDSAT AOI CLOUD
# ============================================================

def calculate_landsat_aoi_cloud(
    item,
    aoi_gdf
):

    signed_item = planetary_computer.sign(
        item
    )

    if "qa_pixel" in signed_item.assets:

        qa_asset = signed_item.assets[
            "qa_pixel"
        ]

    elif "QA_PIXEL" in signed_item.assets:

        qa_asset = signed_item.assets[
            "QA_PIXEL"
        ]

    else:

        raise RuntimeError(
            f"QA_PIXEL asset not found "
            f"for {item.id}"
        )

    with rasterio.open(
        qa_asset.href
    ) as src:

        # Mask-raster pixel size in m^2. QA_PIXEL is 30 m for
        # Landsat (same as the display bands), used for the
        # whole-AOI coverage calculation.
        pixel_area_m2 = float(
            src.res[0] * src.res[1]
        )

        aoi_qa = aoi_gdf.to_crs(
            src.crs
        )

        shapes = [
            geometry.__geo_interface__
            for geometry
            in aoi_qa.geometry
        ]

        # nodata=1: pixels outside the AOI polygon are filled with 1,
        # which sets QA_PIXEL bit 0 (fill) so they are excluded as
        # invalid. nodata=0 would leave bit 0 unset and count the
        # whole crop bounding box as "valid clear" pixels, diluting
        # the AOI cloud percentage.
        clipped, clipped_transform = mask(
            src,
            shapes,
            crop=True,
            nodata=1
        )

        qa = clipped[0]

    fill_mask = bit_is_set(
        qa,
        0
    )

    valid_mask = ~fill_mask

    valid_pixels = int(
        np.sum(valid_mask)
    )

    if valid_pixels == 0:

        raise RuntimeError(
            f"No valid QA pixels "
            f"inside AOI for {item.id}"
        )

    dilated_cloud = bit_is_set(
        qa,
        LANDSAT_DILATED_CLOUD_BIT
    )

    cirrus = bit_is_set(
        qa,
        LANDSAT_CIRRUS_BIT
    )

    cloud = bit_is_set(
        qa,
        LANDSAT_CLOUD_BIT
    )

    cloud_shadow = bit_is_set(
        qa,
        LANDSAT_CLOUD_SHADOW_BIT
    )

    cloud_related = (
        dilated_cloud
        | cirrus
        | cloud
        | cloud_shadow
    )

    cloud_pixels = int(
        np.sum(
            cloud_related & valid_mask
        )
    )

    return {
        "pct": (
            cloud_pixels
            / valid_pixels
            * 100.0
        ),
        "valid_pixels": valid_pixels,
        "cloudy_pixels": cloud_pixels,
        "pixel_area_m2": pixel_area_m2,
        "valid_mask": valid_mask,
        "cloudy_mask": (
            cloud_related
            & valid_mask
        ),
        "transform": clipped_transform,
        "crs": src.crs
    }


# ============================================================
# SEARCH WORKER (per candidate scene)
# ============================================================
#
# The expensive per-scene AOI-cloud calculation downloads a
# quality raster over the network (seconds to tens of seconds
# per scene). Each worker processes ONE candidate scene and
# computes its per-tile AOI statistics.
#
# NO per-tile rejection happens here. Every AOI-intersecting
# tile is collected (with its valid/cloudy masks) so that the
# observation layer can evaluate the COMPLETE AOI — the union
# of all tiles — and decide usability on the combined numbers.
# The cloud classification logic is IDENTICAL to the old
# per-item filter; only the decision point moved.

def _collect_candidate(
    item,
    aoi_gdf
):

    scene_cloud = item.properties.get(
        "eo:cloud_cover"
    )

    if scene_cloud is None:
        return None

    satellite = get_satellite_name(
        item
    )

    platform = item.properties.get(
        "platform",
        ""
    )

    try:

        if satellite == "Sentinel-2":

            stats = calculate_sentinel_aoi_cloud(
                item,
                aoi_gdf
            )

        elif satellite in [
            "Landsat 8",
            "Landsat 9"
        ]:

            stats = calculate_landsat_aoi_cloud(
                item,
                aoi_gdf
            )

        else:

            return None

    except Exception:

        # If quality data cannot be read,
        # the tile cannot contribute.
        return None

    return {
        "date": item.datetime,
        "satellite": satellite,
        "platform": platform,
        "aoi_cloud": float(
            stats["pct"]
        ),
        "aoi_valid_pixels": int(
            stats["valid_pixels"]
        ),
        "aoi_cloudy_pixels": int(
            stats["cloudy_pixels"]
        ),
        "aoi_pixel_area_m2": float(
            stats["pixel_area_m2"]
        ),
        "valid_mask": stats["valid_mask"],
        "cloudy_mask": stats["cloudy_mask"],
        "mask_transform": stats["transform"],
        "mask_crs": stats["crs"],
        "scene_cloud": float(
            scene_cloud
        ),
        "resolution": get_resolution(
            satellite
        ),
        "scene_id": item.id,
        "collection": item.collection_id,
        "item": item
    }


# ============================================================
# OBSERVATION GROUPING
# ============================================================
#
# Product model: one observation = one satellite acquisition
# (pass identity + collection) and ALL of its AOI-intersecting
# tiles that contribute valid pixels to the AOI.
#
# Grouping keys (_group_key):
#   - Sentinel-2: adjacent MGRS tiles of one acquisition share
#     the exact sensing datetime + platform + relative orbit,
#     so (datetime->seconds, collection, platform) merges them
#     into one observation while never merging different
#     acquisitions (different orbits/days have different
#     datetimes; S2A and S2B have different platforms).
#   - Landsat: adjacent WRS-2 ROW scenes of one pass do NOT
#     share item datetime (~24 s apart per row, verified with
#     real data), so datetime is NOT a valid key. The pass
#     identity is (acquisition date YYYYMMDD, WRS path,
#     platform), read from the scene id; adjacent rows of one
#     pass merge, unrelated scenes never do.
#
# Whole-AOI statistics (NO per-tile percentage averaging):
#   - aoi_cells: the AOI's exact discrete pixel budget on the
#     common mask grid (cells whose centre lies inside the AOI
#     polygon — S2 SCL @ 20 m, Landsat QA_PIXEL @ 30 m).
#   - aoi_valid_pixels / aoi_cloudy_pixels: the UNION of every
#     tile's AOI masks on that same grid — each AOI pixel is
#     counted exactly once, even where Landsat rows/paths
#     overlap (~13-18 km sidelap). S2 MGRS tiles abut exactly.
#   - aoi_coverage = union_valid / aoi_cells (discrete basis)
#   - aoi_cloud     = union_cloudy / union_valid
#
# Acceptance (BOTH apply, observation level, after grouping):
#   1. PERMANENT PRODUCT RULE: 100% AOI coverage — every AOI
#      cell must be covered by valid pixels (pixel-exact
#      comparison, no tolerance, no user setting).
#   2. The existing 20% AOI-cloud threshold on the COMBINED
#      whole-AOI cloud.
# Tiles are NEVER dropped before grouping; an observation is
# kept whole or rejected as a whole.

def _group_key(
    result
):
    """
    Acquisition identity for one STAC item.

    Sentinel-2: adjacent MGRS tiles of one acquisition share the
    exact sensing datetime, collection and platform -> key on the
    datetime (second precision). Different orbits or satellites
    have different datetimes / platforms and never merge.

    Landsat: adjacent WRS-2 ROW scenes of one pass do NOT share
    item datetime (verified ~24 s apart per row), so datetime is
    NOT a valid grouping key. The pass identity is the acquisition
    DATE + WRS path + platform, read from the scene id
    (LC09_L2SP_128060_20260815_... -> path 128, date 20260815).
    Adjacent rows of one pass merge; different paths, days or
    satellites never do.
    """
    collection = result["collection"]

    if collection == "landsat-c2-l2":

        parts = result["scene_id"].split("_")

        return (
            "landsat",
            parts[3],           # acquisition date YYYYMMDD
            parts[2][:3],       # WRS-2 path
            result["platform"]  # landsat-8 / landsat-9
        )

    return (
        "sentinel",
        result["date"].replace(
            microsecond=0
        ),
        collection,
        result["platform"]
    )


def _union_aoi_masks(
    tiles,
    aoi_gdf
):
    """
    Combine every tile's AOI valid/cloudy masks into ONE pair of
    whole-AOI masks on a common grid, so each AOI pixel counts
    exactly once even where tiles overlap (Landsat rows/paths
    sidelap ~13-18 km; S2 MGRS tiles abut exactly).

    The common grid is the AOI bounding box in the reference
    tile's CRS, snapped to the reference tile's grid, at the
    mask raster's pixel size (SCL 20 m / QA_PIXEL 30 m). Each
    tile's clipped mask is reprojected onto it with nearest
    neighbour and OR-ed into the union.
    """
    ref = tiles[0]

    ref_crs = ref["mask_crs"]

    ref_transform = ref["mask_transform"]

    dx = abs(
        ref_transform.a
    )

    dy = abs(
        ref_transform.e
    )

    aoi_ref = aoi_gdf.to_crs(
        ref_crs
    )

    minx, miny, maxx, maxy = (
        aoi_ref.total_bounds
    )

    # Snap the common grid to the reference grid so same-CRS
    # tiles (S2 abutting tiles share the UTM grid) warp as a
    # lossless 1:1 copy.
    origin_x = ref_transform.c

    origin_y = ref_transform.f

    minx = origin_x + math.floor(
        (minx - origin_x) / dx
    ) * dx

    maxy = origin_y - math.floor(
        (origin_y - maxy) / dy
    ) * dy

    width = max(
        1,
        int(
            math.ceil(
                (maxx - minx) / dx
            )
        )
    )

    height = max(
        1,
        int(
            math.ceil(
                (maxy - miny) / dy
            )
        )
    )

    dst_transform = from_origin(
        minx,
        maxy,
        dx,
        dy
    )

    # --------------------------------------------------------
    # AOI CELL BUDGET ON THE COMMON GRID
    # --------------------------------------------------------
    #
    # The number of mask-resolution cells whose CENTRE lies
    # inside the AOI polygon (all_touched=False — the same
    # convention rasterio.mask uses for the per-tile masks).
    # This is the exact discrete pixel budget of the AOI on
    # THIS grid; comparing the union's valid count against it
    # is a float-free way to require "no uncovered AOI pixel".

    aoi_raster = rasterize(
        [
            geometry.__geo_interface__
            for geometry
            in aoi_ref.geometry
        ],
        out_shape=(
            height,
            width
        ),
        transform=dst_transform,
        all_touched=False,
        fill=0,
        default_value=1,
        dtype=np.uint8
    )

    aoi_cells = int(
        np.sum(
            aoi_raster > 0
        )
    )

    valid_union = np.zeros(
        (height, width),
        dtype=bool
    )

    cloudy_union = np.zeros(
        (height, width),
        dtype=bool
    )

    for tile in tiles:

        src_valid = np.zeros(
            (height, width),
            dtype=np.uint8
        )

        reproject(
            source=tile["valid_mask"].astype(
                np.uint8
            ),
            destination=src_valid,
            src_transform=tile["mask_transform"],
            src_crs=tile["mask_crs"],
            src_nodata=0,
            dst_transform=dst_transform,
            dst_crs=ref_crs,
            dst_nodata=0,
            resampling=Resampling.nearest
        )

        valid_union |= (
            src_valid > 0
        )

        src_cloudy = np.zeros(
            (height, width),
            dtype=np.uint8
        )

        reproject(
            source=tile["cloudy_mask"].astype(
                np.uint8
            ),
            destination=src_cloudy,
            src_transform=tile["mask_transform"],
            src_crs=tile["mask_crs"],
            src_nodata=0,
            dst_transform=dst_transform,
            dst_crs=ref_crs,
            dst_nodata=0,
            resampling=Resampling.nearest
        )

        cloudy_union |= (
            src_cloudy > 0
        )

    return (
        int(
            np.sum(valid_union)
        ),
        int(
            np.sum(cloudy_union)
        ),
        aoi_cells
    )


def _build_observations(
    results,
    aoi_gdf,
    max_aoi_cloud=MAX_AOI_CLOUD
):
    """
    Group ALL collected tiles into observations and evaluate the
    COMPLETE AOI (union of every tile's AOI pixels, each counted
    once). An observation is usable iff:
      1. its whole-AOI coverage is 100% (every AOI cell covered
         by valid pixels — permanent product rule, pixel-exact),
      AND
      2. its whole-AOI cloud is <= max_aoi_cloud (unchanged).
    Tiles are never dropped before grouping.
    """

    groups = {}

    for result in results:

        key = _group_key(
            result
        )

        groups.setdefault(
            key,
            []
        ).append(
            result
        )

    observations = []

    for key, tiles in groups.items():

        tiles.sort(
            key=lambda t: t["scene_id"]
        )

        collection = tiles[0]["collection"]

        platform = tiles[0]["platform"]

        satellite = tiles[0]["satellite"]

        resolution = tiles[0]["resolution"]

        # ----------------------------------------------------
        # WHOLE-AOI VALID/CLOUDY PIXELS (UNION, COUNTED ONCE)
        # ----------------------------------------------------
        #
        # Single tile: the union of one tile's mask is a lossless
        # 1:1 copy onto its own grid (identical counts to the
        # per-tile stats). Multiple tiles: union the per-tile
        # masks on a common grid so overlapping tiles do not
        # double-count. aoi_cells is the AOI's exact discrete
        # pixel budget on that same grid.

        valid_pixels, cloudy_pixels, aoi_cells = _union_aoi_masks(
            tiles,
            aoi_gdf
        )

        if valid_pixels == 0:

            continue

        aoi_cloud = (
            cloudy_pixels
            / valid_pixels
            * 100.0
        )

        # ----------------------------------------------------
        # PERMANENT PRODUCT RULE — 100% AOI COVERAGE
        # ----------------------------------------------------
        #
        # The COMPLETE AOI must be covered by valid satellite
        # pixels. The check is observation-level (AFTER the
        # tiles are grouped — individual tiles may cover only
        # part of the AOI) and pixel-exact: every AOI cell on
        # the mask grid must be covered by the union of the
        # tiles' valid masks. Any genuinely uncovered AOI area
        # rejects the whole observation. There is deliberately
        # NO configurable tolerance or UI setting.

        if valid_pixels < aoi_cells:

            continue

        # ----------------------------------------------------
        # OBSERVATION ACCEPTANCE — CLOUD
        # ----------------------------------------------------
        #
        # The existing 20% AOI-cloud threshold applies to the
        # COMBINED whole-AOI cloud. No per-tile rejection.

        if aoi_cloud > max_aoi_cloud:

            continue

        # Coverage on the same discrete basis as the acceptance
        # rule: accepted observations always show exactly 100.0%.
        coverage = min(
            100.0,
            valid_pixels
            / aoi_cells
            * 100.0
        )

        dt = min(
            t["date"]
            for t in tiles
        ).replace(
            microsecond=0
        )

        # ----------------------------------------------------
        # OBSERVATION DICT (JSON-SAFE — no pystac objects)
        # ----------------------------------------------------

        observations.append({
            "observation_id": (
                f"{dt:%Y%m%dT%H%M%SZ}_"
                f"{platform or 'unknown'}_"
                f"{collection}"
            ),
            "datetime": dt.isoformat(),
            "date": dt.isoformat(),
            "satellite": satellite,
            "platform": platform,
            "collection": collection,
            "resolution": resolution,
            "scene_cloud": max(
                t["scene_cloud"]
                for t in tiles
            ),
            "aoi_cloud": float(
                aoi_cloud
            ),
            "aoi_coverage": float(
                coverage
            ),
            "aoi_total_pixels": aoi_cells,
            "aoi_valid_pixels": valid_pixels,
            "aoi_cloudy_pixels": cloudy_pixels,
            "tile_count": len(tiles),
            "tiles": [
                {
                    "scene_id": t["scene_id"],
                    "collection": t["collection"],
                    "scene_cloud": t["scene_cloud"],
                    "aoi_cloud": t["aoi_cloud"],
                    "aoi_valid_pixels": t["aoi_valid_pixels"],
                    "aoi_cloudy_pixels": t["aoi_cloudy_pixels"],
                    "bounds": [
                        float(v)
                        for v in t["item"].bbox
                    ]
                }
                for t in tiles
            ]
        })

    observations.sort(
        key=lambda o: o["datetime"],
        reverse=True
    )

    return observations


# ============================================================
# MAIN SEARCH FUNCTION
# ============================================================

def search_satellite_scenes(
    geometry,
    start_date=START_DATE,
    end_date=END_DATE,
    max_scene_cloud=MAX_SCENE_CLOUD,
    max_aoi_cloud=MAX_AOI_CLOUD
):

    # --------------------------------------------------------
    # CONVERT SHAPELY GEOMETRY → GEODATAFRAME
    # --------------------------------------------------------

    import geopandas as gpd

    aoi_gdf = gpd.GeoDataFrame(
        geometry=[geometry],
        crs="EPSG:4326"
    )

    # --------------------------------------------------------
    # SEARCH DIAGNOSTICS (Stage 1 instrumentation — logging only,
    # no logic change, no frozen-behavior change)
    # --------------------------------------------------------

    t_start = time.perf_counter()

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    catalog = _get_search_catalog()

    # --------------------------------------------------------
    # SEARCH
    # --------------------------------------------------------

    search = catalog.search(
        collections=COLLECTIONS,
        intersects=geometry.__geo_interface__,
        datetime=f"{start_date}/{end_date}"
    )

    all_items = list(
        search.items()
    )

    # ------------------------------------------------------------
    # SEARCH DIAGNOSTICS — Stage 1 (total items, per collection)
    # ------------------------------------------------------------

    t_items = time.perf_counter()

    print(f"[search] Stage 1 — STAC items found: {len(all_items)}")

    for col, n in Counter(
        item.collection_id
        for item in all_items
    ).items():

        print(f"[search]   {col}: {n} item(s)")

    print(
        f"[search]   stage 1 elapsed: "
        f"{t_items - t_start:.1f}s"
    )

    # ------------------------------------------------------------
    # SEARCH DIAGNOSTICS — candidate counters (per collection)
    # ------------------------------------------------------------

    cand_stats = {}

    for item in all_items:

        col = item.collection_id

        st = cand_stats.setdefault(
            col,
            {
                "items": 0,
                "no_cloud_cover": 0,
                "ok": 0,
                "failed": 0
            }
        )

        st["items"] += 1

        if item.properties.get(
            "eo:cloud_cover"
        ) is None:

            st["no_cloud_cover"] += 1

    # ------------------------------------------------------------
    # PROCESS CONCURRENTLY (BOUNDED WORKER POOL)
    # ------------------------------------------------------------
    #
    # Candidate scenes are independent: each worker downloads
    # its own quality raster and computes the per-tile AOI stats
    # and masks. NO per-tile rejection happens here — every
    # AOI-intersecting tile is collected so the observation layer
    # can evaluate the COMPLETE AOI and decide usability on the
    # combined numbers. rasterio opens a fresh dataset handle per
    # worker thread (safe), the shared aoi_gdf is only read
    # (to_crs returns new objects), and results are collected and
    # sorted identically afterwards.

    results = []

    with ThreadPoolExecutor(
        max_workers=MAX_SEARCH_WORKERS
    ) as pool:

        futures = [
            pool.submit(
                _collect_candidate,
                item,
                aoi_gdf
            )
            for item in all_items
        ]

        future_item = dict(
            zip(futures, all_items)
        )

        for future in as_completed(
            futures
        ):

            result = future.result()

            col = future_item[
                future
            ].collection_id

            st = cand_stats.setdefault(
                col,
                {
                    "items": 0,
                    "no_cloud_cover": 0,
                    "ok": 0,
                    "failed": 0
                }
            )

            if result is not None:

                results.append(
                    result
                )

                st["ok"] += 1

            else:

                st["failed"] += 1

    # ------------------------------------------------------------
    # SEARCH DIAGNOSTICS — Stage 2 (quality-read outcome)
    # ------------------------------------------------------------

    t_candidates = time.perf_counter()

    print("[search] Stage 2 — candidate processing:")

    for col, st in cand_stats.items():

        print(
            f"[search]   {col}: {st['items']} candidate(s), "
            f"{st['no_cloud_cover']} missing eo:cloud_cover, "
            f"{st['ok']} quality-read OK, "
            f"{st['failed']} dropped (quality read failed / "
            f"no eo:cloud_cover)"
        )

    print(
        f"[search]   stage 2 elapsed: "
        f"{t_candidates - t_items:.1f}s"
    )

    # ------------------------------------------------------------
    # SEARCH DIAGNOSTICS — Stages 3-5 (grouping + rejection mirror)
    # ------------------------------------------------------------
    #
    # The mirror (_search_diagnostics) re-runs the union-mask math for
    # every group and is EXPENSIVE (~30-40 s). It runs only when the
    # SEARCH_DIAGNOSTICS env var is set to 1/true (case-insensitive).
    # Stage 3 (group counts from _group_key alone) is cheap and always
    # printed. If _build_observations is ever modified, the mirror must
    # be updated to match it.

    diag_enabled = _search_diagnostics_enabled()

    if diag_enabled:

        diag = _search_diagnostics(
            results,
            aoi_gdf,
            max_aoi_cloud
        )

    formed_collections = {}

    for r in results:

        formed_collections.setdefault(
            _group_key(r),
            r["collection"]
        )

    print("[search] Stage 3 — observations formed (before filtering):")

    for col, n in Counter(
        formed_collections.values()
    ).items():

        tiles_in_col = sum(
            1
            for r in results
            if r["collection"] == col
        )

        print(
            f"[search]   {col}: {n} observation(s) "
            f"from {tiles_in_col} candidate tile(s)"
        )

    if diag_enabled:

        print(
            "[search] Stage 4/5 — rejection breakdown per collection:"
        )

        for col, d in diag.items():

            print(
                f"[search]   {col}: groups={d['groups']} "
                f"no_valid_pixels={d['no_valid_pixels']} "
                f"coverage_fail={d['coverage_fail']} "
                f"cloud_fail={d['cloud_fail']} "
                f"accepted={d['accepted']}"
            )

    # ------------------------------------------------------------
    # GROUP INTO OBSERVATIONS (NEWEST FIRST)
    # ------------------------------------------------------------
    #
    # The collected tiles become observations: one card per
    # acquisition, containing EVERY AOI-intersecting tile.
    # Whole-AOI coverage and cloud are computed over the union
    # of the tile masks (each AOI pixel counted once) and the
    # existing 20% AOI-cloud threshold decides observation
    # usability.

    observations = _build_observations(
        results,
        aoi_gdf,
        max_aoi_cloud
    )

    # ------------------------------------------------------------
    # SEARCH DIAGNOSTICS — final summary (+ mirror self-check when
    # SEARCH_DIAGNOSTICS is enabled)
    # ------------------------------------------------------------

    t_done = time.perf_counter()

    print("[search] Stage 6 — accepted observations (final):")

    for col, n in Counter(
        o["collection"]
        for o in observations
    ).items():

        print(f"[search]   {col}: {n} observation(s)")

    if diag_enabled:

        mirror_accepted = sum(
            d["accepted"]
            for d in diag.values()
        )

        print(
            f"[search]   self-check: mirror accepted={mirror_accepted} "
            f"== returned={len(observations)} -> "
            f"{'MATCH' if mirror_accepted == len(observations) else 'MISMATCH'}"
        )

    print(
        f"[search] search finished in {t_done - t_start:.1f}s "
        f"(items {t_items - t_start:.1f}s | candidates "
        f"{t_candidates - t_items:.1f}s | group/filter "
        f"{t_done - t_candidates:.1f}s)"
    )

    return observations


def _search_diagnostics_enabled():
    """True when the SEARCH_DIAGNOSTICS env var is set to 1/true
    (case-insensitive). Gates the expensive rejection-mirror computation.
    """
    return os.environ.get(
        "SEARCH_DIAGNOSTICS",
        ""
    ).strip().lower() in ("1", "true")


def _search_diagnostics(
    results,
    aoi_gdf,
    max_aoi_cloud
):
    """INSTRUMENTATION (Stage 1) — read-only mirror of the grouping and
    acceptance classification.

    Calls only frozen helpers (_group_key, _union_aoi_masks); performs NO
    downloads and changes NO behavior. Reports why candidate tiles would be
    rejected by _build_observations. If the frozen logic ever changes, this
    mirror must be updated to match it.
    """

    groups = {}

    for result in results:

        key = _group_key(result)

        groups.setdefault(
            key,
            []
        ).append(
            result
        )

    per_collection = {}

    for key, tiles in groups.items():

        collection = tiles[0]["collection"]

        d = per_collection.setdefault(
            collection,
            {
                "groups": 0,
                "no_valid_pixels": 0,
                "coverage_fail": 0,
                "cloud_fail": 0,
                "accepted": 0
            }
        )

        valid_pixels, cloudy_pixels, aoi_cells = _union_aoi_masks(
            tiles,
            aoi_gdf
        )

        d["groups"] += 1

        if valid_pixels == 0:

            d["no_valid_pixels"] += 1

        elif valid_pixels < aoi_cells:

            d["coverage_fail"] += 1

        elif (
            cloudy_pixels
            / valid_pixels
            * 100.0
        ) > max_aoi_cloud:

            d["cloud_fail"] += 1

        else:

            d["accepted"] += 1

    return per_collection
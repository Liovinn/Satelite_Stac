# ============================================================
# NDVI ANALYTICAL PIPELINE
# ============================================================
#
# Consumes a SELECTED observation produced by the frozen
# satellite layer (backend/satellite_backend.py). It performs
# NO new STAC search, NO cloud re-filtering and NO coverage
# re-evaluation — the observation arrived with:
#   AOI cloud <= 20% AND exact 100% AOI coverage (post-grouping).
#
# Pipeline (per the NDVI architecture):
#
#   Frozen observation
#       -> per-tile AOI-windowed Red/NIR reads (native res)
#       -> reflectance scaling (S2 /10000; Landsat *0.0000275-0.2)
#       -> explicit validity mask (red & nir & quality & finite
#          & red+nir > 0)
#       -> per-tile float32 NDVI (NaN = invalid; never 0.0)
#       -> multi-tile mosaic on a common grid (first-valid-wins,
#          deterministic order = observation.tiles order)
#       -> exact AOI polygon mask (rasterize, all_touched=False)
#       -> statistics + histogram from the FINAL masked raster
#       -> RGBA/PNG visualization (separate downstream step)
#       -> GeoTIFF of the float32 analytical raster
#
# The display PNG is NEVER used as the analytical source.
# ============================================================

import io
import json
import math
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, bounds as window_bounds
from rasterio.windows import from_bounds as window_from_bounds

import pystac_client
import planetary_computer
from PIL import Image

from .satellite_backend import (
    _aoi_read_window,          # frozen helper: per-tile AOI window
    bit_is_set,
    SENTINEL_CLOUD_CLASSES,
    LANDSAT_DILATED_CLOUD_BIT,
    LANDSAT_CIRRUS_BIT,
    LANDSAT_CLOUD_BIT,
    LANDSAT_CLOUD_SHADOW_BIT,
)

# ============================================================
# BAND MAPPING + REFLECTANCE SCALING
# ============================================================

def ndvi_band_keys(collection):
    """
    Analytical bands per collection:
      Sentinel-2 L2A: Red=B04, NIR=B08 (both 10 m)
      Landsat 8/9 L2: Red=SR band 4, NIR=SR band 5 (both 30 m).
    On Planetary Computer the landsat-c2-l2 assets for these
    bands are named "red" and "nir08" (their raster:bands
    metadata declares scale=2.75e-05, offset=-0.2 — the
    standard Collection 2 SR scaling used by _reflectance).
    """
    if collection == "sentinel-2-l2a":
        return "B04", "B08"
    if collection == "landsat-c2-l2":
        return "red", "nir08"
    raise ValueError(f"Unsupported collection: {collection}")


def _reflectance(stored, collection):
    """
    Convert stored product values to surface reflectance
    (float32). NO clipping to [0,1] — invalid pixels are handled
    by the explicit validity mask, not by value clipping.
    """
    values = stored.astype(np.float32)
    if collection == "sentinel-2-l2a":
        return values / np.float32(10000.0)
    # Landsat Collection 2 SR scaling
    return values * np.float32(0.0000275) - np.float32(0.2)


# ============================================================
# NDVI VALIDITY SETTINGS
# ============================================================
#
# Fixes for the diagnosed Landsat NDVI extremes (max 121.5 etc.):
#
# 1. Landsat Collection 2 SR scaling (DN * 0.0000275 - 0.2) produces
#    NEGATIVE reflectance on dark pixels (DN < ~7273). Reflectance is
#    physically >= 0; negative values are an atmospheric-correction
#    artifact. Such pixels are MASKED (never clipped to 0).
#
# 2. Near-zero red+nir denominators amplify reflectance noise into
#    arbitrary NDVI. A conservative epsilon (reflectance units) guards
#    the denominator. Validated against real Landsat data: normal
#    vegetation has red+nir ~ 0.2-0.6, so this only removes dark
#    no-signal pixels.
#
# 3. QA_PIXEL cloud-confidence bits 9-10 (0 low / 1 medium / 2 high /
#    3 highest): pixels with high/highest confidence but no explicit
#    cloud bit are haze/cloud-edge candidates. Optional extra mask,
#    validated before enabling (0 = disabled).

NDVI_DENOM_EPSILON = 0.02

LANDSAT_CLOUD_CONFIDENCE_THRESHOLD = 0  # 0 = disabled; 2 = mask conf >= 2

# Bounded per-tile parallelism for multi-tile observations. The
# per-tile work is network/raster-bound (COG window reads + quality
# resample); a small pool overlaps the downloads. Observations are
# typically 1-2 tiles (S2 MGRS pairs, Landsat WRS rows); 4 keeps
# big multi-tile observations fast without flooding Planetary
# Computer with concurrent range requests.
NDVI_TILE_WORKERS = 4


# ============================================================
# QUALITY / CLOUD MASKING (same product semantics as the frozen
# cloud filter: S2 SCL classes {3,8,9,10}; Landsat QA_PIXEL
# fill + dilated-cloud + cirrus + cloud + shadow bits)
# ============================================================

def _quality_valid_mask(quality, collection):
    if collection == "sentinel-2-l2a":
        # SCL: 0 = no data; cloud classes are invalid for NDVI.
        return (
            (quality != 0)
            & ~np.isin(
                quality,
                list(SENTINEL_CLOUD_CLASSES)
            )
        )
    # Landsat QA_PIXEL
    fill = bit_is_set(quality, 0)
    cloud_related = (
        bit_is_set(quality, LANDSAT_DILATED_CLOUD_BIT)
        | bit_is_set(quality, LANDSAT_CIRRUS_BIT)
        | bit_is_set(quality, LANDSAT_CLOUD_BIT)
        | bit_is_set(quality, LANDSAT_CLOUD_SHADOW_BIT)
    )
    valid = (~fill) & (~cloud_related)
    if LANDSAT_CLOUD_CONFIDENCE_THRESHOLD > 0:
        # QA_PIXEL bits 9-10: cloud confidence (0 low, 1 medium,
        # 2 high, 3 highest). Pixels with high/highest confidence but
        # no explicit cloud bit are haze/cloud-edge candidates that
        # the explicit bit mask alone accepts. Optional extra mask —
        # enabled only after validation (see NDVI VALIDITY SETTINGS).
        cloud_confidence = (quality >> 9) & 0b11
        valid &= cloud_confidence < LANDSAT_CLOUD_CONFIDENCE_THRESHOLD
    return valid


# ============================================================
# PER-TILE NDVI
# ============================================================

def _read_windowed_band(href, window):
    with rasterio.open(href) as src:
        return src.read(1, window=window)


def _tile_ndvi(signed_item, collection, aoi_json, aoi_gdf):
    """
    Compute one tile's float32 NDVI over the tile-specific AOI
    window at NATIVE analytical resolution (S2 10 m / Landsat
    30 m). Red and NIR are read concurrently (separate dataset
    handles per thread — the same safe pattern the frozen RGB
    loader uses). The quality mask (SCL 20 m / QA_PIXEL 30 m) is
    resampled onto the analytical grid with nearest-neighbour.
    Invalid pixels become NaN — never 0.0.
    """
    red_key, nir_key = ndvi_band_keys(collection)

    red_href = signed_item.assets[red_key].href
    nir_href = signed_item.assets[nir_key].href

    with rasterio.open(red_href) as src:

        crs = src.crs

        window = _aoi_read_window(src, aoi_json)

        if window is None:

            window = Window(0, 0, src.width, src.height)

        # Deterministic integer window (rasterio would round a
        # float window internally anyway).
        window = window.round_offsets().round_lengths()

        transform = src.window_transform(window)

        out_shape = (
            window.height,
            window.width
        )

    with ThreadPoolExecutor(max_workers=2) as pool:

        futures = [
            pool.submit(
                _read_windowed_band,
                href,
                window
            )
            for href in (
                red_href,
                nir_href
            )
        ]

        red_stored = futures[0].result()

        nir_stored = futures[1].result()

    # --------------------------------------------------------
    # QUALITY MASK OVER THE SAME GEOGRAPHIC WINDOW
    # --------------------------------------------------------

    quality_key = None

    if collection == "sentinel-2-l2a":

        quality_key = "SCL"

    elif "qa_pixel" in signed_item.assets:

        quality_key = "qa_pixel"

    elif "QA_PIXEL" in signed_item.assets:

        quality_key = "QA_PIXEL"

    if quality_key is None:

        raise RuntimeError(
            f"Quality asset not found for {signed_item.id}"
        )

    with rasterio.open(
        signed_item.assets[quality_key].href
    ) as qsrc:

        # Geographic extent of the analytical window. NOTE:
        # rasterio.windows.bounds(window, transform) expects
        # the FULL raster transform, not the window transform
        # (it would apply the window offsets twice). Use
        # array_bounds with the window transform instead.
        geo_bounds = rasterio.transform.array_bounds(
            window.height,
            window.width,
            transform
        )

        qwindow = window_from_bounds(
            *geo_bounds,
            qsrc.transform
        ).round_offsets().round_lengths()

        qwindow = qwindow.intersection(
            Window(0, 0, qsrc.width, qsrc.height)
        )

        if qwindow.width < 1 or qwindow.height < 1:

            quality_valid = np.zeros(
                (out_shape[0], out_shape[1]),
                dtype=bool
            )

        else:

            quality_native = qsrc.read(
                1,
                window=qwindow
            )

            qtransform = qsrc.window_transform(
                qwindow
            )

            quality_resampled = np.zeros(
                out_shape,
                dtype=np.uint8
            )

            reproject(
                source=_quality_valid_mask(
                    quality_native,
                    collection
                ).astype(np.uint8),
                destination=quality_resampled,
                src_transform=qtransform,
                src_crs=crs,
                src_nodata=0,
                dst_transform=transform,
                dst_crs=crs,
                dst_nodata=0,
                resampling=Resampling.nearest
            )

            quality_valid = (
                quality_resampled > 0
            )

    # --------------------------------------------------------
    # REFLECTANCE + VALIDITY + NDVI
    # --------------------------------------------------------

    red = _reflectance(red_stored, collection)
    nir = _reflectance(nir_stored, collection)

    denom = red + nir

    # FIX 1 — mask physically invalid NEGATIVE reflectance (Landsat
    # Collection 2 SR scaling DN * 0.0000275 - 0.2 goes negative on
    # dark pixels, DN < ~7273). Reflectance is physically >= 0;
    # negative values are an atmospheric-correction artifact that
    # makes NDVI blow up when red ~= -nir. Masked, never clipped.
    reflectance_valid = (
        (red >= np.float32(0.0))
        & (nir >= np.float32(0.0))
    )

    # FIX 2 — denominator protection. red+nir near zero amplifies
    # reflectance noise into arbitrary NDVI (denom -> 0+ yields any
    # value). Epsilon in reflectance units; normal vegetation sits at
    # red+nir ~ 0.2-0.6, so this only removes dark no-signal pixels.
    denom_valid = (
        denom > np.float32(NDVI_DENOM_EPSILON)
    )

    valid = (
        np.isfinite(red)
        & np.isfinite(nir)
        & quality_valid
        & reflectance_valid
        & denom_valid
    )

    with np.errstate(divide="ignore", invalid="ignore"):

        ndvi = np.where(
            valid,
            (nir - red) / denom,
            np.nan
        ).astype(np.float32)

    # Flag unexpected out-of-range values (do NOT clip them).
    finite_values = ndvi[valid]

    if finite_values.size > 0:

        if (
            finite_values.min() < -1.0 - 1e-6
            or finite_values.max() > 1.0 + 1e-6
        ):

            print(
                f"[ndvi] WARNING {signed_item.id}: "
                f"NDVI range [{finite_values.min():.4f}, "
                f"{finite_values.max():.4f}]"
            )

    return {
        "ndvi": ndvi,
        "valid_mask": valid,
        "transform": transform,
        "crs": crs
    }


# ============================================================
# MULTI-TILE MOSAIC
# ============================================================
#
# Common grid: AOI bounding box in the reference tile's CRS,
# snapped to the reference tile's grid origin, at the
# ANALYTICAL pixel size (S2 10 m / Landsat 30 m). Each tile's
# NDVI is reprojected with nearest-neighbour.
#
# OVERLAP RULE (deterministic): tiles are merged in the
# observation's tile order (the frozen layer sorts them by
# scene_id ascending). The FIRST tile that has valid data at a
# pixel wins; later tiles only fill pixels still NoData. No
# averaging of overlapping pixels and no averaging of tile
# statistics anywhere.

def _mosaic_ndvi(tile_results, aoi_gdf):
    ref = tile_results[0]

    dx = abs(ref["transform"].a)
    dy = abs(ref["transform"].e)

    aoi_ref = aoi_gdf.to_crs(ref["crs"])

    minx, miny, maxx, maxy = aoi_ref.total_bounds

    origin_x = ref["transform"].c
    origin_y = ref["transform"].f

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

    mosaic = np.full(
        (height, width),
        np.nan,
        dtype=np.float32
    )

    for tile in tile_results:

        if (
            not tile["valid_mask"].any()
        ):

            # This tile has NO valid pixels over the AOI window
            # (e.g., the AOI portion falls outside its valid
            # footprint, like a WRS row whose data ends before
            # the AOI). It cannot contribute to the mosaic, so
            # skip it entirely — this also avoids a rasterio/
            # GDAL warp pathology (an all-NaN source with NaN
            # nodata can leave the process unable to encode
            # later PNGs).

            continue

        tile_reprojected = np.full(
            (height, width),
            np.nan,
            dtype=np.float32
        )

        reproject(
            source=tile["ndvi"],
            destination=tile_reprojected,
            src_transform=tile["transform"],
            src_crs=tile["crs"],
            src_nodata=np.nan,
            dst_transform=dst_transform,
            dst_crs=ref["crs"],
            dst_nodata=np.nan,
            resampling=Resampling.nearest
        )

        fill = (
            ~np.isnan(tile_reprojected)
            & np.isnan(mosaic)
        )

        mosaic[fill] = tile_reprojected[fill]

    return mosaic, dst_transform, ref["crs"]


# ============================================================
# EXACT AOI MASK
# ============================================================
#
# Pixels outside the AOI POLYGON become NaN (bounding box is
# never used as the analytical boundary). rasterize with
# all_touched=False — the same centre-in-polygon convention the
# frozen coverage layer uses.

def _apply_aoi_mask(mosaic, aoi_gdf, transform, crs):

    aoi_ref = aoi_gdf.to_crs(crs)

    aoi_raster = rasterize(
        [
            geometry.__geo_interface__
            for geometry
            in aoi_ref.geometry
        ],
        out_shape=mosaic.shape,
        transform=transform,
        all_touched=False,
        fill=0,
        default_value=1,
        dtype=np.uint8
    )

    masked = mosaic.copy()

    masked[aoi_raster == 0] = np.nan

    return masked


# ============================================================
# STATISTICS + HISTOGRAM (from the FINAL AOI-clipped raster)
# ============================================================

def _ndvi_statistics(mosaic):

    valid_values = mosaic[~np.isnan(mosaic)]

    if valid_values.size == 0:

        return None, None

    # Auditable flag (never a clip): how many valid pixels fall
    # outside [-1, 1] — a known Landsat SR artifact on dark
    # pixels whose red reflectance is slightly negative.
    out_of_range = int(
        np.sum(
            (valid_values < -1.0)
            | (valid_values > 1.0)
        )
    )

    stats = {
        "valid_pixel_count": int(
            valid_values.size
        ),
        "out_of_range_pixels": out_of_range,
        "mean": float(
            np.mean(valid_values)
        ),
        "median": float(
            np.median(valid_values)
        ),
        "min": float(
            np.min(valid_values)
        ),
        "max": float(
            np.max(valid_values)
        ),
        "std": float(
            np.std(valid_values)
        ),
        "p10": float(
            np.percentile(valid_values, 10)
        ),
        "p25": float(
            np.percentile(valid_values, 25)
        ),
        "p75": float(
            np.percentile(valid_values, 75)
        ),
        "p90": float(
            np.percentile(valid_values, 90)
        )
    }

    counts, edges = np.histogram(
        valid_values,
        bins=32,
        range=(-1.0, 1.0)
    )

    histogram = {
        "counts": [
            int(c)
            for c in counts
        ],
        "edges": [
            float(e)
            for e in edges
        ]
    }

    return stats, histogram


# ============================================================
# VISUALIZATION (separate from analytics; palette is a
# presentation concern, never encoded into the raster)
# ============================================================

# Piecewise-linear NDVI colour LUT (stops: value -> RGB).
# Bare soil/water -> brown/tan, sparse -> yellow, vegetation
# -> green (darker = denser). The frontend legend gradient must
# match these stops.

NDVI_LUT_STOPS = [
    (-1.0, (97, 66, 30)),
    (0.0, (178, 137, 60)),
    (0.2, (222, 205, 90)),
    (0.4, (110, 184, 80)),
    (0.6, (30, 130, 60)),
    (1.0, (8, 70, 35))
]


def _ndvi_to_rgba(mosaic):
    height, width = mosaic.shape

    valid = ~np.isnan(mosaic)

    values = np.where(
        valid,
        np.clip(mosaic, -1.0, 1.0),
        0.0
    )

    stops_x = np.array(
        [stop[0] for stop in NDVI_LUT_STOPS],
        dtype=np.float64
    )

    channels = [
        np.interp(
            values,
            stops_x,
            [stop[1][channel] for stop in NDVI_LUT_STOPS]
        ).astype(np.uint8)
        for channel in range(3)
    ]

    rgba = np.dstack(
        channels
        + [
            np.where(valid, 255, 0).astype(np.uint8)
        ]
    )

    return rgba


def _rgba_to_png(rgba):
    image = Image.fromarray(rgba, mode="RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _mosaic_to_geotiff(mosaic, transform, crs):
    """GeoTIFF of the float32 analytical raster (NaN nodata)."""
    with MemoryFile() as mem:

        with mem.open(
            driver="GTiff",
            height=mosaic.shape[0],
            width=mosaic.shape[1],
            count=1,
            dtype="float32",
            crs=crs,
            transform=transform,
            nodata=np.nan
        ) as dst:

            dst.write(mosaic, 1)

        return mem.read()


# ============================================================
# OBSERVATION-LEVEL ENTRY + CACHE
# ============================================================
#
# One entry per (observation_id, AOI) — bounded LRU so repeated
# analysis / visualization of the same observation is instant
# without unbounded memory growth. The float32 mosaic itself is
# not retained (stats/GeoTIFF/PNG already materialized); the
# analytical raster remains available as the GeoTIFF.

_NDVI_CACHE = {}
_NDVI_CACHE_ORDER = []
_NDVI_CACHE_MAX = 8


def _normalize_aoi(aoi):
    """Canonical string form of the AOI geometry (sort_keys so
    key order in JSON never changes the cache key)."""
    return json.dumps(aoi, sort_keys=True)


def _cache_bump(key):
    if key in _NDVI_CACHE_ORDER:
        _NDVI_CACHE_ORDER.remove(key)
    _NDVI_CACHE_ORDER.append(key)


def _cache_put(key, entry):
    _cache_bump(key)
    _NDVI_CACHE[key] = entry
    while len(_NDVI_CACHE_ORDER) > _NDVI_CACHE_MAX:
        oldest = _NDVI_CACHE_ORDER.pop(0)
        _NDVI_CACHE.pop(oldest, None)


# ============================================================
# STAC CATALOG / COLLECTION REUSE
# ============================================================
#
# One lazy, cached Planetary Computer catalog client per process
# (lru_cache is thread-safe — concurrent misses are serialized, so
# the catalog is opened exactly once). get_collection is likewise
# cached per collection id. The client is used for READ-ONLY
# catalog/collection/item lookups only (plain GETs over urllib3's
# thread-safe connection pool); planetary_computer.sign() is always
# called per item, so SAS-token signing behavior is unchanged.

STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"


@lru_cache(maxsize=1)
def _get_catalog():
    return pystac_client.Client.open(STAC_API_URL)


@lru_cache(maxsize=4)
def _get_collection(collection_id):
    return _get_catalog().get_collection(collection_id)


def compute_ndvi(observation, aoi):
    """
    Full NDVI analysis for a frozen observation + AOI geometry.

    observation: the observation object produced by the frozen
        layer (has collection, tiles[].scene_id, date, ...).
    aoi: GeoJSON geometry dict (the same geometry the frozen
        layer used for the search).

    Returns {stats, histogram, metadata, png, geotiff}.
    """
    from shapely.geometry import shape as shapely_shape

    import geopandas as gpd

    key = (
        observation["observation_id"],
        _normalize_aoi(aoi)
    )

    cached = _NDVI_CACHE.get(key)

    if cached is not None:

        _cache_bump(key)

        return cached

    aoi_geometry = shapely_shape(aoi)

    if aoi_geometry.is_empty or not aoi_geometry.is_valid:

        raise ValueError("AOI geometry is empty or invalid.")

    aoi_gdf = gpd.GeoDataFrame(
        geometry=[aoi_geometry],
        crs="EPSG:4326"
    )

    tiles = observation.get("tiles") or []

    if not tiles:

        raise ValueError("Observation has no tiles.")

    # The frozen /api/aoi response carries the collection on
    # each tile entry (and optionally on the observation).
    collection = (
        observation.get("collection")
        or tiles[0].get("collection")
    )

    if not collection:

        raise ValueError("Observation has no collection.")

    # Hoisted OUT of the per-tile work: one STAC collection fetch
    # per observation instead of one per tile.
    stac_collection = _get_collection(collection)

    def _process_tile(tile):

        item = stac_collection.get_item(
            tile["scene_id"]
        )

        if item is None:

            raise ValueError(
                f"Scene not found: {tile['scene_id']}"
            )

        signed = planetary_computer.sign(
            item
        )

        return _tile_ndvi(
            signed,
            collection,
            _normalize_aoi(aoi),
            aoi_gdf
        )

    # Bounded parallel per-tile processing. The per-tile work is
    # network/raster-bound; the pool overlaps the downloads. Results
    # are collected in SUBMISSION order (tiles order), so the mosaic
    # receives tile_results in exactly the same deterministic order
    # as the sequential version — first-valid-wins and all mosaic
    # semantics are unchanged. _tile_ndvi spawns its own 2-worker
    # band pool internally, so the effective concurrency is bounded
    # at ~2 * NDVI_TILE_WORKERS threads.
    with ThreadPoolExecutor(
        max_workers=min(
            NDVI_TILE_WORKERS,
            len(tiles)
        )
    ) as pool:

        futures = [
            pool.submit(
                _process_tile,
                tile
            )
            for tile in tiles
        ]

        tile_results = [
            future.result()
            for future in futures
        ]

    # --------------------------------------------------------
    # MOSAIC -> EXACT AOI MASK -> STATISTICS
    # --------------------------------------------------------

    mosaic, dst_transform, dst_crs = _mosaic_ndvi(
        tile_results,
        aoi_gdf
    )

    mosaic = _apply_aoi_mask(
        mosaic,
        aoi_gdf,
        dst_transform,
        dst_crs
    )

    stats, histogram = _ndvi_statistics(mosaic)

    if stats is None:

        raise ValueError(
            "No valid NDVI pixels inside the AOI."
        )

    # --------------------------------------------------------
    # METADATA (same [bottom, top, left, right] contract as the
    # frozen RGB metadata)
    # --------------------------------------------------------

    left, bottom, right, top = transform_bounds(
        dst_crs,
        CRS.from_epsg(4326),
        *rasterio.transform.array_bounds(
            mosaic.shape[0],
            mosaic.shape[1],
            dst_transform
        )
    )

    resolution = observation.get(
        "resolution"
    )

    if not resolution:

        resolution = (
            "10 m"
            if collection == "sentinel-2-l2a"
            else "30 m"
        )

    metadata = {
        "observation_id": observation[
            "observation_id"
        ],
        "date": observation["date"],
        "satellite": observation["satellite"],
        "collection": collection,
        "resolution": resolution,
        "tile_count": len(tiles),
        "tile_ids": [
            tile["scene_id"]
            for tile in tiles
        ],
        "aoi_cloud": observation.get(
            "aoi_cloud"
        ),
        "aoi_coverage": observation.get(
            "aoi_coverage"
        ),
        "bounds": [
            bottom,
            top,
            left,
            right
        ],
        "crs": "EPSG:4326",
        "width": int(mosaic.shape[1]),
        "height": int(mosaic.shape[0])
    }

    # --------------------------------------------------------
    # DOWNSTREAM OUTPUTS (visualization + georeferenced raster)
    # --------------------------------------------------------

    png = _rgba_to_png(
        _ndvi_to_rgba(mosaic)
    )

    geotiff = _mosaic_to_geotiff(
        mosaic,
        dst_transform,
        dst_crs
    )

    entry = {
        "stats": stats,
        "histogram": histogram,
        "metadata": metadata,
        "png": png,
        "geotiff": geotiff
    }

    _cache_put(key, entry)

    return entry


def get_ndvi_entry(observation_id, aoi_json):
    """
    Look up a previously computed entry (for visualize/raster
    endpoints). Returns None when the observation/AOI was not
    analyzed yet.
    """
    key = (
        observation_id,
        _normalize_aoi(
            json.loads(aoi_json)
        )
    )

    entry = _NDVI_CACHE.get(key)

    if entry is not None:

        _cache_bump(key)

    return entry

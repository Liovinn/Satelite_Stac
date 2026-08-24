from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from shapely.geometry import shape
from pydantic import BaseModel
from typing import Any, Dict
from .satellite_backend import (
    search_satellite_scenes,
    load_satellite_rgb,
    load_satellite_metadata
)
from fastapi.responses import Response, JSONResponse
from pystac_client.exceptions import APIError
import io
from PIL import Image

from .ndvi import (
    compute_ndvi,
    get_ndvi_entry
)

# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title="Satellite Viewer API",
    version="1.0.0"
)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,

    allow_origins=["*"],

    allow_credentials=True,

    allow_methods=["*"],

    allow_headers=["*"],
)


# ============================================================
# AOI REQUEST MODEL
# ============================================================

class AOIRequest(BaseModel):

    geometry: Dict[str, Any]


class NDVIRequest(BaseModel):

    observation: Dict[str, Any]

    aoi: Dict[str, Any]


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/api/health")
def health():

    return {
        "success": True,
        "message": "Satellite Viewer backend is running."
    }


# ============================================================
# AOI ENDPOINT
# ============================================================

@app.post("/api/aoi")
def receive_aoi(
    request: AOIRequest
):

    geometry_data = request.geometry

    # ========================================================
    # CONVERT GEOJSON → SHAPELY
    # ========================================================

    try:

        aoi_geometry = shape(
            geometry_data
        )

    except Exception as e:

        return JSONResponse(
            {
                "success": False,
                "message": f"Invalid geometry: {e}"
            },
            status_code=400
        )


    # ========================================================
    # BASIC GEOMETRY VALIDATION
    # ========================================================

    if aoi_geometry.is_empty:

        return JSONResponse(
            {
                "success": False,
                "message": "AOI geometry is empty."
            },
            status_code=400
        )


    if not aoi_geometry.is_valid:

        return JSONResponse(
            {
                "success": False,
                "message": "AOI geometry is invalid."
            },
            status_code=400
        )


    # ========================================================
    # SEARCH SATELLITE IMAGERY
    # ========================================================

    try:

        observations = search_satellite_scenes(
            aoi_geometry
        )

    except Exception as e:

        return JSONResponse(
            {
                "success": False,
                "message": f"Satellite search failed: {e}"
            },
            status_code=500
        )


    # ========================================================
    # RETURN RESULTS
    # ========================================================
    #
    # Each entry is one OBSERVATION (one acquisition, one or
    # more tiles). The tiles array keeps the individual scene
    # ids so the frontend can load every tile's imagery.

    api_observations = []

    for observation in observations:

        collection = observation["collection"]

        if collection == "sentinel-2-l2a":

            satellite = "Sentinel-2"
            resolution = "10 m"

        elif collection == "landsat-c2-l2":

            satellite = observation.get(
                "satellite",
                "Landsat"
            )

            resolution = "30 m"

        else:

            satellite = collection
            resolution = "Unknown"

        api_observations.append({

            "observation_id": observation[
                "observation_id"
            ],

            "date": observation["date"],

            "satellite": satellite,

            "resolution": resolution,

            "scene_cloud": observation[
                "scene_cloud"
            ],

            "aoi_cloud": observation[
                "aoi_cloud"
            ],

            "aoi_coverage": observation[
                "aoi_coverage"
            ],

            "tile_count": observation[
                "tile_count"
            ],

            "tiles": observation["tiles"]

        })


    return {

        "success": True,

        "message":
        "Satellite imagery search completed.",

        "observations": api_observations

    }


@app.get("/api/scene/{scene_id}/rgb")
def get_scene_rgb(
    scene_id: str,
    collection: str,
    aoi: str = None
):

    try:

        # ----------------------------------------------------
        # LOAD DISPLAY RASTER (windowed when the AOI is given)
        # ----------------------------------------------------

        result = load_satellite_rgb(
            scene_id,
            collection,
            aoi
        )


        # ----------------------------------------------------
        # GET RGB ARRAY
        # ----------------------------------------------------

        rgb = result["rgb"]


        # ----------------------------------------------------
        # CONVERT NUMPY ARRAY → PNG
        # ----------------------------------------------------

        image = Image.fromarray(
            rgb,
            mode="RGBA"
        )


        buffer = io.BytesIO()


        image.save(
            buffer,
            format="PNG"
        )


        # ----------------------------------------------------
        # RETURN IMAGE
        # ----------------------------------------------------

        return Response(
            content=buffer.getvalue(),
            media_type="image/png"
        )


    except (ValueError, APIError) as e:

        message = str(e)

        if (
            "Could not find scene" in message
            or "NotFoundError" in message
            or "No collection with id" in message
        ):

            status_code = 404

        else:

            # e.g. "Unsupported collection: ..."
            status_code = 400

        return JSONResponse(
            {
                "success": False,
                "message": message
            },
            status_code=status_code
        )

    except Exception as e:

        return JSONResponse(
            {
                "success": False,
                "message":
                    f"Could not load satellite imagery: {e}"
            },
            status_code=500
        )

@app.get("/api/scene/{scene_id}/metadata")
def get_scene_metadata(
    scene_id: str,
    collection: str,
    aoi: str = None
):

    try:

        # ----------------------------------------------------
        # CHEAP METADATA: header-only COG open (no pixel data).
        # The old implementation called load_satellite_rgb()
        # here, downloading and processing all three RGB bands
        # just to return bounds — the biggest single bottleneck
        # in the card-to-imagery path.
        # ----------------------------------------------------

        result = load_satellite_metadata(
            scene_id,
            collection,
            aoi
        )

        return {

            "success": True,

            "bounds": result["bounds"],

            "width": result["width"],

            "height": result["height"],

            "crs": result["crs"]

        }

    except (ValueError, APIError) as e:

        message = str(e)

        if (
            "Could not find scene" in message
            or "NotFoundError" in message
            or "No collection with id" in message
        ):

            status_code = 404

        else:

            status_code = 400

        return JSONResponse(
            {
                "success": False,
                "message": message
            },
            status_code=status_code
        )

    except Exception as e:

        return JSONResponse(
            {
                "success": False,
                "message":
                    f"Could not load scene metadata: {e}"
            },
            status_code=500
        )


# ============================================================
# NDVI ENDPOINTS
# ============================================================
#
# NDVI consumes the SELECTED observation produced by the frozen
# satellite layer — no new STAC search, no cloud re-filtering,
# no coverage re-evaluation. The frontend sends the observation
# object it is already displaying (which passed the frozen
# layer's AOI cloud <= 20% AND exact 100% coverage rules).
#
#   POST /api/ndvi/analyze        -> statistics + histogram +
#                                    geospatial metadata
#   GET  /api/ndvi/{id}/visualize -> RGBA PNG overlay (cached)
#   GET  /api/ndvi/{id}/raster    -> float32 GeoTIFF (cached)
#
# The float32 analytical raster is NEVER derived from the PNG.

@app.post("/api/ndvi/analyze")
def ndvi_analyze(
    request: NDVIRequest
):

    try:

        entry = compute_ndvi(
            request.observation,
            request.aoi
        )

        return {

            "success": True,

            "stats": entry["stats"],

            "histogram": entry["histogram"],

            "metadata": entry["metadata"]

        }

    except (ValueError, KeyError) as e:

        message = str(e)

        if "Scene not found" in message:

            status_code = 404

        else:

            # invalid/empty AOI geometry, missing tiles/collection,
            # malformed observation payload, or no valid pixels
            # inside the AOI for this selection
            status_code = 400

        return JSONResponse(
            {
                "success": False,
                "message": message
            },
            status_code=status_code
        )

    except Exception as e:

        return JSONResponse(
            {
                "success": False,
                "message":
                    f"NDVI analysis failed: {e}"
            },
            status_code=500
        )


@app.get("/api/ndvi/{observation_id}/visualize")
def ndvi_visualize(
    observation_id: str,
    aoi: str = None
):

    if not aoi:

        return JSONResponse(
            {
                "success": False,
                "message": "Missing aoi parameter."
            },
            status_code=400
        )

    try:

        entry = get_ndvi_entry(
            observation_id,
            aoi
        )

        if entry is None:

            return JSONResponse(
                {
                    "success": False,
                    "message":
                        "NDVI not computed for this "
                        "observation/AOI. Call "
                        "/api/ndvi/analyze first."
                },
                status_code=404
            )

        return Response(
            content=entry["png"],
            media_type="image/png"
        )

    except Exception as e:

        return JSONResponse(
            {
                "success": False,
                "message":
                    f"Could not load NDVI visualization: {e}"
            },
            status_code=500
        )


@app.get("/api/ndvi/{observation_id}/raster")
def ndvi_raster(
    observation_id: str,
    aoi: str = None
):

    if not aoi:

        return JSONResponse(
            {
                "success": False,
                "message": "Missing aoi parameter."
            },
            status_code=400
        )

    try:

        entry = get_ndvi_entry(
            observation_id,
            aoi
        )

        if entry is None:

            return JSONResponse(
                {
                    "success": False,
                    "message":
                        "NDVI not computed for this "
                        "observation/AOI. Call "
                        "/api/ndvi/analyze first."
                },
                status_code=404
            )

        return Response(
            content=entry["geotiff"],
            media_type="image/tiff",
            headers={
                "Content-Disposition":
                    "attachment; "
                    f"filename={observation_id}_ndvi.tif"
            }
        )

    except Exception as e:

        return JSONResponse(
            {
                "success": False,
                "message":
                    f"Could not load NDVI raster: {e}"
            },
            status_code=500
        )
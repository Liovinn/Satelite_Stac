// ============================================================
// MAP INITIALIZATION
// ============================================================

const map = L.map("map", {
    zoomControl: false
}).setView(
    [-2.5, 118.0],
    5
);


// ============================================================
// ZOOM CONTROL (BOTTOM LEFT)
// ============================================================
//
// The default top-left position collides with the application
// control panel; place Leaflet's native zoom control at the
// bottom-left instead.

L.control.zoom({
    position: "bottomleft"
}).addTo(map);


// ============================================================
// OPENSTREETMAP
// ============================================================

L.tileLayer(
    "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    {
        maxZoom: 19,

        attribution:
            '&copy; OpenStreetMap contributors'
    }
).addTo(map);


// ============================================================
// AOI LAYER
// ============================================================

const aoiLayer = L.featureGroup().addTo(map);


// ============================================================
// DRAW CONTROL
// ============================================================

const drawControl = new L.Control.Draw({

    draw: {

        polygon: {

            shapeOptions: {

                color: "#3388ff",

                weight: 2,

                opacity: 1.0,

                fillColor: "#3388ff",

                fillOpacity: 0.05
            }
        },

        rectangle: {

            shapeOptions: {

                color: "#3388ff",

                weight: 2,

                opacity: 1.0,

                fillColor: "#3388ff",

                fillOpacity: 0.05
            }
        },

        polyline: false,

        circle: false,

        circlemarker: false,

        marker: false

    },

    edit: {

        featureGroup: aoiLayer

    }

});


// We don't show the draw control permanently.
// We will activate drawing using our own button.


// ============================================================
// STATUS
// ============================================================

const statusElement =
    document.getElementById(
        "status"
    );


function setStatus(message) {

    statusElement.textContent =
        message;
}


// ============================================================
// API BASE
// ============================================================

const API_BASE =
    "http://127.0.0.1:8000";


// ============================================================
// DRAW BUTTON
// ============================================================
//
// One active draw handler at a time: clicking Draw while a draw
// is already active disables the previous handler before starting
// a new one (public L.Draw API only — no private internals).

let activeDrawHandler = null;

document
    .getElementById("draw-button")
    .addEventListener(
        "click",
        () => {

            setStatus(
                "✎ Draw a polygon on the map."
            );

            if (
                activeDrawHandler
            ) {

                activeDrawHandler.disable();
            }

            const drawer =
                new L.Draw.Polygon(
                    map,
                    drawControl.options.draw.polygon
                );

            activeDrawHandler =
                drawer;

            drawer.enable();
        }
    );


// ============================================================
// AOI SEARCH SEQUENCING
// ============================================================
//
// Each AOI search (draw) gets an incrementing id. A response
// that is no longer the latest — because the AOI was cleared
// or a newer AOI was drawn — is ignored, so stale results can
// never repopulate the UI.

let aoiSearchId = 0;

// Aborts the in-flight AOI search when a newer search starts or
// the AOI is cleared, so an abandoned search stops consuming
// backend work and connections.
let aoiSearchAbort = null;

// The geometry of the CURRENT AOI (GeoJSON), kept so the backend
// can window each tile's RGB read to the AOI's extent. Set on
// draw/upload, cleared with Clear AOI.
let currentAoiGeojson = null;


// ============================================================
// POLYGON CREATED
// ============================================================

map.on(
    L.Draw.Event.CREATED,
    async function (event) {

        // ----------------------------------------------------
        // SEARCH SEQUENCING: capture this search's id
        // ----------------------------------------------------

        const searchId =
            ++aoiSearchId;

        // ----------------------------------------------------
        // ABORT ANY PREVIOUS SEARCH STILL IN FLIGHT
        // ----------------------------------------------------

        if (
            aoiSearchAbort
        ) {

            aoiSearchAbort.abort();
        }

        aoiSearchAbort =
            new AbortController();

        // The completed draw handler is done — no active draw
        // remains until the Draw button is clicked again.
        activeDrawHandler =
            null;

        // ----------------------------------------------------
        // REMOVE PREVIOUS AOI
        // ----------------------------------------------------

        aoiLayer.clearLayers();


        // ----------------------------------------------------
        // ADD NEW AOI TO MAP
        // ----------------------------------------------------

        const layer =
            event.layer;

        aoiLayer.addLayer(
            layer
        );


        // ----------------------------------------------------
        // ZOOM TO AOI
        // ----------------------------------------------------

        if (
            layer.getBounds &&
            layer.getBounds().isValid()
        ) {

            map.fitBounds(
                layer.getBounds(),
                {
                    padding: [
                        40,
                        40
                    ]
                }
            );
        }


        // ----------------------------------------------------
        // CONVERT LEAFLET GEOMETRY TO GEOJSON
        // ----------------------------------------------------

        const geojson =
            layer.toGeoJSON();

        // Remember the AOI geometry so tile RGB reads can be
        // windowed to the AOI extent (backend optimization).
        currentAoiGeojson =
            geojson.geometry;


        // ----------------------------------------------------
        // UPDATE STATUS
        // ----------------------------------------------------

        setStatus(
            "✓ AOI selected"
        );


        // ----------------------------------------------------
        // SEND AOI TO FASTAPI
        // ----------------------------------------------------

        setStatus(
            "⟳ Searching imagery..."
        );

        try {

            const response =
                await fetch(
                    API_BASE + "/api/aoi",
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json"
                        },

                        body: JSON.stringify({
                            geometry:
                                geojson.geometry
                        }),

                        signal:
                            aoiSearchAbort.signal
                    }
                );


            const result =
                await response.json();


            // ------------------------------------------------
            // STALE SEARCH GUARD
            // ------------------------------------------------
            //
            // The AOI was cleared or a newer AOI was drawn
            // while this search was in flight — ignore this
            // response so it cannot repopulate the UI.

            if (
                searchId !== aoiSearchId
            ) {

                return;

            }


            // ------------------------------------------------
            // BACKEND RESPONSE
            // ------------------------------------------------

            if (result.success) {

                console.log(
                    "Satellite search results:",
                    result
                );


                setStatus(
                    `✓ ${result.observations.length} observations available`
                );


                displayObservations(
                    result.observations
                );

            } else {

                setStatus(
                    result.message ||
                    "Backend rejected AOI."
                );
            }


        } catch (error) {

            // An intentional abort (a newer search started or the
            // AOI was cleared) is never a user-visible failure.
            if (
                error.name === "AbortError"
            ) {

                return;
            }

            // Ignore failures from stale searches — the AOI was
            // cleared or a newer AOI was drawn meanwhile.
            if (
                searchId !== aoiSearchId
            ) {

                return;

            }

            console.error(
                "AOI request failed:",
                error
            );


            setStatus(
                "Could not connect to backend."
            );

        }

    }
);


// ============================================================
// FILE UPLOAD
// ============================================================

const uploadButton =
    document.getElementById(
        "upload-button"
    );

const fileInput =
    document.getElementById(
        "file-input"
    );


uploadButton.addEventListener(
    "click",
    () => {

        fileInput.click();

    }
);


fileInput.addEventListener(
    "change",
    async function () {

        const file =
            fileInput.files[0];

        if (!file) {
            return;
        }


        setStatus(
            `Loading ${file.name}...`
        );


        // For GeoJSON we can immediately
        // display it in the browser.

        if (
            file.name
                .toLowerCase()
                .endsWith(
                    ".geojson"
                )
            ||
            file.name
                .toLowerCase()
                .endsWith(
                    ".json"
                )
        ) {

            try {

                const text =
                    await file.text();

                const geojson =
                    JSON.parse(
                        text
                    );

                displayAOI(
                    geojson
                );

                setStatus(
                    "✓ AOI selected"
                );

            } catch (error) {

                console.error(
                    error
                );

                setStatus(
                    "Could not read GeoJSON."
                );
            }

            return;
        }


        // GPKG / SHP will eventually
        // be sent to FastAPI.

        setStatus(
            "This file type will be processed by the backend."
        );
    }
);


// ============================================================
// DISPLAY AOI
// ============================================================

function displayAOI(
    geojson
) {

    aoiLayer.clearLayers();

    // Remember the AOI geometry (Feature, FeatureCollection or
    // bare geometry) for windowed tile RGB reads.
    currentAoiGeojson =
        geojson.type === "FeatureCollection"
            ? geojson.features[0].geometry
            : (
                geojson.type === "Feature"
                    ? geojson.geometry
                    : geojson
            );


    const layer =
        L.geoJSON(
            geojson,
            {
                style: {

                    color: "#3388ff",

                    weight: 2,

                    opacity: 1.0,

                    fillColor: "#3388ff",

                    fillOpacity: 0.05
                }
            }
        );


    layer.eachLayer(
        function (item) {

            aoiLayer.addLayer(
                item
            );

        }
    );


    if (
        aoiLayer.getBounds()
            .isValid()
    ) {

        map.fitBounds(
            aoiLayer.getBounds(),
            {
                padding: [
                    40,
                    40
                ]
            }
        );
    }
}


// ============================================================
// CLEAR AOI
// ============================================================

function clearAoi() {

    // Invalidate any in-flight search or scene-selection
    // response, so a stale result cannot repopulate the UI
    // after the AOI has been cleared.
    aoiSearchId++;
    sceneSelectionId++;

    // Abort any in-flight AOI search / observation selection
    // fetches — their responses are stale by definition now.
    if (
        aoiSearchAbort
    ) {

        aoiSearchAbort.abort();
        aoiSearchAbort = null;
    }

    if (
        sceneSelectionAbort
    ) {

        sceneSelectionAbort.abort();
        sceneSelectionAbort = null;
    }

    // Forget the AOI geometry (windowed RGB reads no longer apply).
    currentAoiGeojson = null;

    // Disable an active draw tool through the public API so its
    // temporary vertices/markers are removed as well.
    if (
        activeDrawHandler
    ) {

        activeDrawHandler.disable();
        activeDrawHandler = null;
    }

    // Remove the AOI polygon(s).
    aoiLayer.clearLayers();

    // Remove every satellite overlay of the current observation.
    if (
        window.satelliteOverlays
    ) {

        window.satelliteOverlays.forEach(
            function (overlay) {

                map.removeLayer(
                    overlay
                );

            }
        );

        window.satelliteOverlays =
            [];
    }

    // Remove the imagery cards.
    const sceneResults =
        document.getElementById(
            "scene-results"
        );

    if (sceneResults) {

        sceneResults.remove();

    }

    // Reset status and map view to the default state.
    setStatus(
        "Select an area to begin."
    );

    map.setView(
        [-2.5, 118.0],
        5
    );
}


document
    .getElementById("clear-button")
    .addEventListener(
        "click",
        clearAoi
    );


// ============================================================
// LOCATION SEARCH
// ============================================================

const searchInput =
    document.getElementById(
        "location-search"
    );

const searchButton =
    document.getElementById(
        "search-button"
    );


async function searchLocation() {

    const query =
        searchInput.value.trim();

    if (!query) {
        return;
    }


    setStatus(
        "Searching location..."
    );


    try {

        const url =
            "https://nominatim.openstreetmap.org/search"
            +
            "?format=json"
            +
            "&limit=1"
            +
            "&q="
            +
            encodeURIComponent(
                query
            );


        const response =
            await fetch(
                url
            );


        const results =
            await response.json();


        if (
            results.length === 0
        ) {

            setStatus(
                "Location not found."
            );

            return;
        }


        const result =
            results[0];


        const lat =
            parseFloat(
                result.lat
            );

        const lon =
            parseFloat(
                result.lon
            );


        map.setView(
            [
                lat,
                lon
            ],
            14
        );


        setStatus(
            result.display_name
        );


    } catch (error) {

        console.error(
            error
        );

        setStatus(
            "Location search failed."
        );
    }
}


searchButton.addEventListener(
    "click",
    searchLocation
);


searchInput.addEventListener(
    "keydown",
    function (event) {

        if (
            event.key === "Enter"
        ) {

            searchLocation();

        }

    }
);
function displayObservations(
    observations
) {

    const existing =
        document.getElementById(
            "scene-results"
        );


    if (existing) {

        existing.remove();

    }


    const container =
        document.createElement(
            "div"
        );


    container.id =
        "scene-results";

    container.className =
        "scene-results";


    const title =
        document.createElement(
            "div"
        );

    title.className =
        "section-title";

    title.textContent =
        "Available imagery";


    container.appendChild(
        title
    );


    if (
        !observations ||
        observations.length === 0
    ) {

        const empty =
            document.createElement(
                "div"
            );

        empty.className =
            "empty-message";

        empty.textContent =
            "No suitable satellite imagery found.";

        container.appendChild(
            empty
        );

    }


    observations.forEach(
        function (obs) {

            const card =
                document.createElement(
                    "div"
                );

            card.className =
                "scene-card";


            // ------------------------------------------------
            // HEADER: date (left) + satellite (right)
            // ------------------------------------------------

            const header =
                document.createElement(
                    "div"
                );

            header.className =
                "scene-header";

            const date =
                document.createElement(
                    "span"
                );

            date.className =
                "scene-date";

            date.textContent =
                obs.date.slice(0, 10);

            date.title =
                obs.date;

            const satellite =
                document.createElement(
                    "span"
                );

            satellite.className =
                "scene-satellite";

            satellite.textContent =
                obs.satellite;

            header.appendChild(
                date
            );

            header.appendChild(
                satellite
            );


            // ------------------------------------------------
            // METRICS: AOI cloud (primary) + resolution
            // ------------------------------------------------

            const metrics =
                document.createElement(
                    "div"
                );

            metrics.className =
                "scene-metrics";

            const aoiMetric =
                document.createElement(
                    "div"
                );

            aoiMetric.className =
                "scene-metric";

            const aoiLabel =
                document.createElement(
                    "span"
                );

            aoiLabel.className =
                "scene-metric-label";

            aoiLabel.textContent =
                "AOI cloud";

            const aoiValue =
                document.createElement(
                    "span"
                );

            aoiValue.className =
                "scene-metric-value";

            aoiValue.textContent =
                `${obs.aoi_cloud.toFixed(2)}%`;

            aoiMetric.appendChild(
                aoiLabel
            );

            aoiMetric.appendChild(
                aoiValue
            );

            const resMetric =
                document.createElement(
                    "div"
                );

            resMetric.className =
                "scene-metric";

            const resLabel =
                document.createElement(
                    "span"
                );

            resLabel.className =
                "scene-metric-label";

            resLabel.textContent =
                "Resolution";

            const resValue =
                document.createElement(
                    "span"
                );

            resValue.className =
                "scene-metric-value";

            resValue.textContent =
                obs.resolution;

            resMetric.appendChild(
                resLabel
            );

            resMetric.appendChild(
                resValue
            );

            metrics.appendChild(
                aoiMetric
            );

            const coverageMetric =
                document.createElement(
                    "div"
                );

            coverageMetric.className =
                "scene-metric";

            const coverageLabel =
                document.createElement(
                    "span"
                );

            coverageLabel.className =
                "scene-metric-label";

            coverageLabel.textContent =
                "AOI coverage";

            const coverageValue =
                document.createElement(
                    "span"
                );

            coverageValue.className =
                "scene-metric-value";

            coverageValue.textContent =
                `${obs.aoi_coverage.toFixed(1)}%`;

            coverageMetric.appendChild(
                coverageLabel
            );

            coverageMetric.appendChild(
                coverageValue
            );

            metrics.appendChild(
                coverageMetric
            );

            metrics.appendChild(
                resMetric
            );

            const tilesMetric =
                document.createElement(
                    "div"
                );

            tilesMetric.className =
                "scene-metric";

            const tilesLabel =
                document.createElement(
                    "span"
                );

            tilesLabel.className =
                "scene-metric-label";

            tilesLabel.textContent =
                "Tiles";

            const tilesValue =
                document.createElement(
                    "span"
                );

            tilesValue.className =
                "scene-metric-value";

            tilesValue.textContent =
                `${obs.tile_count} tile` +
                (
                    obs.tile_count === 1
                        ? ""
                        : "s"
                );

            tilesMetric.appendChild(
                tilesLabel
            );

            tilesMetric.appendChild(
                tilesValue
            );

            metrics.appendChild(
                tilesMetric
            );

            card.appendChild(
                header
            );

            card.appendChild(
                metrics
            );


            // ------------------------------------------------
            // SECONDARY: scene cloud (already in API response)
            // ------------------------------------------------

            if (
                obs.scene_cloud !== undefined
                &&
                obs.scene_cloud !== null
            ) {

                const sceneCloud =
                    document.createElement(
                        "div"
                    );

                sceneCloud.className =
                    "scene-cloud-secondary";

                sceneCloud.textContent =
                    `Scene cloud ${obs.scene_cloud.toFixed(1)}%`;

                card.appendChild(
                    sceneCloud
                );
            }


            // ------------------------------------------------
            // CLICK: mark selected, then load scene
            // ------------------------------------------------

            card.addEventListener(
                "click",
                function () {

                    container
                        .querySelectorAll(
                            ".scene-card.selected"
                        )
                        .forEach(
                            function (c) {

                                c.classList.remove(
                                    "selected"
                                );
                            }
                        );

                    card.classList.add(
                        "selected"
                    );

                    selectObservation(
                        obs
                    );

                }
            );


            container.appendChild(
                card
            );

        }
    );


    document
        .querySelector(
            ".control-panel"
        )
        .appendChild(
            container
        );
}
// ------------------------------------------------------------
// OBSERVATION SELECTION SEQUENCING
// ------------------------------------------------------------
//
// selectObservation() is async and each card click fires
// /metadata and /rgb requests per tile that take seconds. If a
// slower response from an EARLIER selection finishes after a
// newer selection, it must not replace the newer observation's
// overlays or status. Every invocation captures the current
// selection id and is allowed to touch the map only while it is
// still the LATEST selection; otherwise it is stale and ignored.

let sceneSelectionId = 0;

// Aborts the in-flight observation selection (tile metadata
// fetches) when a newer selection starts or the AOI is cleared.
let sceneSelectionAbort = null;

// The observation currently selected by the frozen satellite
// flow (set inside selectObservation). NDVI consumes this.
let selectedObservation = null;

async function selectObservation(
    observation
) {

    const selectionId =
        ++sceneSelectionId;

    // Abort any previous selection's tile fetches still in
    // flight — a newer selection supersedes them.
    if (
        sceneSelectionAbort
    ) {

        sceneSelectionAbort.abort();
    }

    sceneSelectionAbort =
        new AbortController();

    // Remember the observation the user selected, so the NDVI
    // layer can consume it (the frozen selection flow is
    // otherwise untouched).
    selectedObservation =
        observation;

    const tiles =
        observation.tiles || [];

    // AOI query param: lets the backend window each tile's RGB
    // read to the AOI extent (reduces remote COG reads). Omitted
    // when no AOI is active (backend then serves the full tile).
    const aoiParam =
        currentAoiGeojson
            ? "&aoi="
                + encodeURIComponent(
                    JSON.stringify(
                        currentAoiGeojson
                    )
                )
            : "";

    setStatus(
        `⟳ Loading ${observation.satellite} imagery...`
    );


    try {

        // ----------------------------------------------------
        // LOAD EVERY TILE OF THE OBSERVATION (IN PARALLEL)
        // ----------------------------------------------------
        //
        // Each tile needs its own /metadata call (which also
        // populates the backend RGB cache) to obtain the tile's
        // precise EPSG:4326 bounds. A tile that fails to load is
        // skipped; the remaining tiles are still displayed.

        const loadedTiles =
            await Promise.all(
                tiles.map(
                    async function (tile) {

                        try {

                            const metadataResponse =
                                await fetch(
                                    API_BASE
                                    + "/api/scene/"
                                    +
                                    encodeURIComponent(
                                        tile.scene_id
                                    )
                                    +
                                    "/metadata?collection="
                                    +
                                    encodeURIComponent(
                                        tile.collection
                                    )
                                    +
                                    aoiParam,

                                    {
                                        signal:
                                            sceneSelectionAbort.signal
                                    }
                                );

                            const metadata =
                                await metadataResponse.json();

                            if (
                                !metadata.success
                            ) {

                                console.error(
                                    "Tile metadata failed:",
                                    tile.scene_id,
                                    metadata.message
                                );

                                return null;
                            }

                            return {
                                imageUrl:
                                    API_BASE
                                    + "/api/scene/"
                                    +
                                    encodeURIComponent(
                                        tile.scene_id
                                    )
                                    +
                                    "/rgb?collection="
                                    +
                                    encodeURIComponent(
                                        tile.collection
                                    )
                                    +
                                    aoiParam,
                                bounds: [
                                    [
                                        metadata.bounds[0],
                                        metadata.bounds[2]
                                    ],
                                    [
                                        metadata.bounds[1],
                                        metadata.bounds[3]
                                    ]
                                ]
                            };

                        } catch (error) {

                            // Intentional abort (a newer selection
                            // started, or the AOI was cleared) —
                            // not a tile failure.
                            if (
                                error.name === "AbortError"
                            ) {

                                return null;
                            }

                            console.error(
                                "Tile load failed:",
                                tile.scene_id,
                                error
                            );

                            return null;
                        }
                    }
                )
            );


        // STALE-RESPONSE GUARD: a newer card was selected while
        // this observation's tiles were loading. Ignore this
        // result entirely — it must not remove/replace the
        // current overlays or write status. Everything below
        // this point is synchronous, so this single check covers
        // the whole map/status update.
        if (
            selectionId !== sceneSelectionId
        ) {

            return;

        }

        const displayTiles =
            loadedTiles.filter(
                Boolean
            );

        if (
            displayTiles.length === 0
        ) {

            setStatus(
                "Could not load satellite imagery."
            );

            return;
        }


        // ----------------------------------------------------
        // TILE LOADING PROGRESS
        // ----------------------------------------------------
        //
        // The observation's tiles download in PARALLEL and each
        // overlay becomes visible as soon as its own image
        // finishes. Track that progress so the status line tells
        // the user how many tiles are done instead of showing a
        // premature "✓" (which previously appeared while some
        // tiles were still loading, making the AOI look only
        // partially covered).
        //
        // Tiles whose metadata failed are counted as failures
        // immediately; the remaining tiles report through their
        // image load/error events. Every status write is guarded
        // by the selection id so a stale observation's tile can
        // never overwrite the current UI.

        const totalTiles =
            tiles.length;

        const progress = {
            loaded: 0,
            failed: 0
        };

        loadedTiles.forEach(
            function (loaded, index) {

                if (
                    loaded === null
                ) {

                    progress.failed++;
                }
            }
        );

        const tileLoaded =
            function () {

                if (
                    selectionId !== sceneSelectionId
                ) {

                    return;
                }

                progress.loaded++;

                if (
                    totalTiles === 1
                ) {

                    // Single-tile observations keep the simple
                    // completed message — no "1/1" counter.

                    setStatus(
                        `✓ ${observation.date.slice(0, 10)} · ${observation.satellite} · ${observation.tile_count} tile(s)`
                    );

                } else if (
                    progress.failed > 0
                ) {

                    // A tile already failed — stay in the warning
                    // state (never claim a clean completion).

                    setStatus(
                        `⚠ ${progress.loaded}/${totalTiles} tiles loaded`
                    );

                } else if (
                    progress.loaded === totalTiles
                ) {

                    setStatus(
                        `✓ ${observation.date.slice(0, 10)} · ${observation.satellite} · ${observation.tile_count} tile(s)`
                    );

                } else {

                    setStatus(
                        `${totalTiles} tiles · ${progress.loaded}/${totalTiles} loaded`
                    );
                }
            };

        const tileFailed =
            function () {

                if (
                    selectionId !== sceneSelectionId
                ) {

                    return;
                }

                progress.failed++;

                if (
                    totalTiles === 1
                ) {

                    setStatus(
                        "⚠ Could not load satellite imagery."
                    );

                } else {

                    setStatus(
                        `⚠ ${progress.loaded}/${totalTiles} tiles loaded`
                    );
                }
            };

        // Initial status once the overlays exist (images still
        // downloading): multi-tile observations show the live
        // counter starting at 0/M; single-tile observations keep
        // the simple "⟳ Loading..." message (no counter).

        const updateTileStatus =
            function () {

                if (
                    selectionId !== sceneSelectionId
                ) {

                    return;
                }

                if (
                    totalTiles > 1
                ) {

                    if (
                        progress.failed > 0
                    ) {

                        setStatus(
                            `⚠ ${progress.loaded}/${totalTiles} tiles loaded`
                        );

                    } else {

                        setStatus(
                            `${totalTiles} tiles · ${progress.loaded}/${totalTiles} loaded`
                        );
                    }
                }
            };


        // ----------------------------------------------------
        // REMOVE ALL OVERLAYS OF THE PREVIOUS OBSERVATION
        // ----------------------------------------------------

        if (
            window.satelliteOverlays
        ) {

            window.satelliteOverlays.forEach(
                function (overlay) {

                    map.removeLayer(
                        overlay
                    );

                }
            );
        }

        window.satelliteOverlays =
            [];


        // ----------------------------------------------------
        // SATELLITE PANE (BELOW VECTOR/AOI PANE)
        // ----------------------------------------------------
        //
        // imageOverlay renders in the overlay pane (z-index 400),
        // painting over the AOI vectors in the same pane. Give
        // the satellite image its own pane between the tile pane
        // (200) and the overlay pane (400) so the AOI boundary
        // stays visible on top.

        if (!map.getPane("satellite")) {

            const satellitePane =
                map.createPane("satellite");

            satellitePane.style.zIndex =
                350;
        }


        // ----------------------------------------------------
        // CREATE ONE IMAGE OVERLAY PER TILE
        // ----------------------------------------------------
        //
        // Every required tile gets its own overlay positioned
        // with its own precisely transformed EPSG:4326 bounds.
        // The tiles abut exactly, so the combined imagery covers
        // the AOI continuously without any pixel-level mosaic.

        window.satelliteOverlays =
            displayTiles.map(
                function (tile) {

                    const overlay =
                        L.imageOverlay(
                            tile.imageUrl,
                            tile.bounds,
                            {
                                opacity: 1.0,

                                interactive: false,

                                pane: "satellite"
                            }
                        ).addTo(
                            map
                        );

                    // Report this tile's progress through the
                    // status line when its image finishes (or
                    // fails). The counters are guarded against
                    // stale selections inside the handlers.
                    overlay.on(
                        "load",
                        tileLoaded
                    );

                    overlay.on(
                        "error",
                        tileFailed
                    );

                    return overlay;
                }
            );


        // ----------------------------------------------------
        // ZOOM TO SATELLITE IMAGE
        // ----------------------------------------------------

        map.fitBounds(
            aoiLayer.getBounds(),
            {
                padding: [
                    40,
                    40
                ]
            }
        );


        // ----------------------------------------------------
        // ENFORCE ZOOM CAP
        // ----------------------------------------------------
        //
        // fitBounds' animated path can silently skip the
        // maxZoom option when a zoom animation is already in
        // flight, so cap the zoom with an explicit synchronous
        // setZoom instead. Keeps the view focused on the AOI
        // without zooming so deep that the display raster
        // becomes pixelated.

        if (
            map.getZoom() > 14
        ) {

            map.setZoom(
                14,
                {
                    animate: false
                }
            );
        }


        // ----------------------------------------------------
        // KEEP AOI ABOVE SATELLITE
        // ----------------------------------------------------

        aoiLayer.bringToFront();


        // ----------------------------------------------------
        // STATUS — TILE LOADING PROGRESS
        // ----------------------------------------------------
        //
        // Multi-tile observations show a live "N/M loaded"
        // counter (starting at 0/M) that updates as each tile's
        // image finishes; the completed "✓" state appears only
        // when every tile has loaded. Single-tile observations
        // keep the simple "⟳ Loading..." message until their
        // single image finishes.

        updateTileStatus();


    } catch (error) {

        // Ignore failures from stale selections — the current
        // scene's loading/error state must not be overwritten.
        if (
            selectionId !== sceneSelectionId
        ) {

            return;

        }

        console.error(
            "Satellite image loading failed:",
            error
        );


        setStatus(
            "Failed to load satellite imagery."
        );
    }
}

// ============================================================
// NDVI VIEWER
// ============================================================
//
// Consumes the observation selected by the frozen satellite
// layer. The NDVI overlay is a SEPARATE visualization layer:
// the frozen RGB overlays are only hidden/restored (never
// modified), and the float32 analytical raster lives on the
// backend (statistics/PNG/GeoTIFF all come from it).

const ndviButton =
    document.getElementById(
        "ndvi-button"
    );

const ndviPanel =
    document.getElementById(
        "ndvi-panel"
    );

let ndviActive = false;

// Aborts an in-flight NDVI analysis when a new one starts or the
// NDVI view is hidden (new AOI / new observation / Clear AOI).
let ndviAnalysisAbort = null;

let ndviOverlay = null;

function hideNdviView() {

    // Abort any in-flight NDVI analysis — its response is no
    // longer wanted once the NDVI view is being hidden.
    if (
        ndviAnalysisAbort
    ) {

        ndviAnalysisAbort.abort();
        ndviAnalysisAbort = null;
    }

    if (
        ndviOverlay
    ) {

        map.removeLayer(
            ndviOverlay
        );

        ndviOverlay =
            null;
    }

    ndviPanel.classList.remove(
        "visible"
    );

    ndviActive = false;

    ndviButton.textContent =
        "Analyze NDVI";
}

function restoreRgbOverlays() {

    if (
        window.satelliteOverlays
    ) {

        window.satelliteOverlays.forEach(
            function (overlay) {

                overlay.addTo(
                    map
                );
            }
        );
    }
}

function renderNdviStats(stats) {

    const fields = [
        ["mean", stats.mean.toFixed(4)],
        ["median", stats.median.toFixed(4)],
        ["min", stats.min.toFixed(4)],
        ["max", stats.max.toFixed(4)],
        ["std dev", stats.std.toFixed(4)],
        ["P10", stats.p10.toFixed(4)],
        ["P25", stats.p25.toFixed(4)],
        ["P75", stats.p75.toFixed(4)],
        ["P90", stats.p90.toFixed(4)],
        ["valid px", Number(stats.valid_pixel_count).toLocaleString()]
    ];

    document.getElementById(
        "ndvi-stats"
    ).innerHTML =
        fields.map(
            function (field) {

                return (
                    `<div class="ndvi-stat">`
                    +
                    `<span class="ndvi-stat-label">${field[0]}</span>`
                    +
                    `<span class="ndvi-stat-value">${field[1]}</span>`
                    +
                    `</div>`
                );
            }
        ).join("");
}

function renderNdviHistogram(histogram) {

    const maxCount =
        Math.max(
            ...histogram.counts,
            1
        );

    document.getElementById(
        "ndvi-histogram"
    ).innerHTML =
        histogram.counts.map(
            function (count) {

                return (
                    `<div class="ndvi-hist-bar" `
                    +
                    `style="height:`
                    +
                    `${Math.round(count / maxCount * 100)}%"`
                    +
                    `></div>`
                );
            }
        ).join("");
}

ndviButton.addEventListener(
    "click",
    async function () {

        // ------------------------------------------------
        // TOGGLE OFF: restore the RGB viewer as-is.
        // ------------------------------------------------

        if (
            ndviActive
        ) {

            hideNdviView();

            restoreRgbOverlays();

            if (
                selectedObservation
            ) {

                setStatus(
                    `✓ ${selectedObservation.date.slice(0, 10)} · ${selectedObservation.satellite} · ${selectedObservation.tile_count} tile(s)`
                );
            }

            return;
        }

        if (
            !selectedObservation
            ||
            !currentAoiGeojson
        ) {

            setStatus(
                "Select an observation first, then analyze NDVI."
            );

            return;
        }

        setStatus(
            "⟳ Computing NDVI..."
        );

        // Abort any previous analysis still in flight.
        if (
            ndviAnalysisAbort
        ) {

            ndviAnalysisAbort.abort();
        }

        ndviAnalysisAbort =
            new AbortController();

        try {

            const response =
                await fetch(
                    API_BASE + "/api/ndvi/analyze",
                    {
                        method: "POST",

                        headers: {
                            "Content-Type":
                                "application/json"
                        },

                        body: JSON.stringify({
                            observation:
                                selectedObservation,

                            aoi:
                                currentAoiGeojson
                        }),

                        signal:
                            ndviAnalysisAbort.signal
                    }
                );

            const result =
                await response.json();

            if (
                !result.success
            ) {

                setStatus(
                    result.message ||
                    "NDVI analysis failed."
                );

                return;
            }

            const stats =
                result.stats;

            const metadata =
                result.metadata;

            // --------------------------------------------
            // HIDE THE RGB OVERLAYS (they stay cached in
            // window.satelliteOverlays and are restored on
            // toggle-off).
            // --------------------------------------------

            if (
                window.satelliteOverlays
            ) {

                window.satelliteOverlays.forEach(
                    function (overlay) {

                        map.removeLayer(
                            overlay
                        );
                    }
                );
            }

            // --------------------------------------------
            // NDVI OVERLAY (window-exact bounds from the
            // backend metadata — same contract as RGB).
            // --------------------------------------------

            const aoiParam =
                "?aoi="
                +
                encodeURIComponent(
                    JSON.stringify(
                        currentAoiGeojson
                    )
                );

            ndviOverlay =
                L.imageOverlay(
                    API_BASE
                    + "/api/ndvi/"
                    +
                    encodeURIComponent(
                        metadata.observation_id
                    )
                    +
                    "/visualize"
                    +
                    aoiParam,
                    [
                        [
                            metadata.bounds[0],
                            metadata.bounds[2]
                        ],
                        [
                            metadata.bounds[1],
                            metadata.bounds[3]
                        ]
                    ],
                    {
                        opacity: 1.0,

                        interactive: false,

                        pane: "satellite"
                    }
                ).addTo(
                    map
                );

            ndviActive = true;

            ndviButton.textContent =
                "Hide NDVI";

            // --------------------------------------------
            // STATS + HISTOGRAM + LEGEND
            // --------------------------------------------

            renderNdviStats(
                stats
            );

            renderNdviHistogram(
                result.histogram
            );

            document.getElementById(
                "ndvi-legend-min"
            ).textContent =
                "-1.0";

            document.getElementById(
                "ndvi-legend-max"
            ).textContent =
                "+1.0";

            ndviPanel.classList.add(
                "visible"
            );

            setStatus(
                `✓ NDVI · ${metadata.date.slice(0, 10)} · ${metadata.satellite} · mean ${stats.mean.toFixed(3)}`
            );

        } catch (error) {

            // Intentional abort (a newer analysis started or the
            // NDVI view was hidden) — not a user-visible failure.
            if (
                error.name === "AbortError"
            ) {

                return;
            }

            console.error(
                "NDVI request failed:",
                error
            );

            setStatus(
                "Could not connect to backend for NDVI."
            );
        }
    }
);

// ------------------------------------------------
// RESET THE NDVI VIEW when the satellite context
// changes (new AOI drawn, new observation selected,
// AOI cleared) so a stale NDVI overlay can never
// outlive its observation. The frozen flows are not
// modified — these are additive listeners.
// ------------------------------------------------

map.on(
    "draw:created",
    function () {

        if (
            ndviActive
        ) {

            hideNdviView();

            restoreRgbOverlays();
        }
    }
);

document.addEventListener(
    "click",
    function (event) {

        if (
            ndviActive
            &&
            event.target.closest(
                ".scene-card"
            )
        ) {

            hideNdviView();
        }
    }
);

document
    .getElementById(
        "clear-button"
    )
    .addEventListener(
        "click",
        function () {

            if (
                ndviActive
            ) {

                hideNdviView();
            }
        }
    );
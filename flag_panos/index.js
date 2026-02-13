const CITY = 'amsterdam'
const SIDEWALK_SERVER_FQDN = `https://sidewalk-${CITY}.cs.washington.edu`

const OUTPUT_JSON = `${CITY}_pano_image_data.json`
const UNRETRIEVABLE_PANOS_JSON = `${CITY}_unretrievable_panos.json`

const CHUNK_SIZE = 10000;

function getPanos(url, callback) {
    // grab panorama info from Project Sidewalk endpoint
    fetch(url)
        .then(response => response.json())
        .then(result => callback(result));
}

async function flag_panos_for_redownload(pano_data) {
    // initially, filter out panos that already have image data or have empty pano_id
    filtered_pano_data = pano_data.filter(pano => pano["pano_id"] && (!pano["width"] || !pano["height"]));
    console.log(filtered_pano_data.length);

    // instantiate streetviewservice instance
    let streetViewService = new google.maps.StreetViewService();

    let new_pano_data = [];
    let failed_to_retrieve_metadata = [];

    // Check pano metadata in chunks
    for (let i = 0; i < filtered_pano_data.length; i += CHUNK_SIZE) {
        let metadata_promises = [];

        pano_slice = filtered_pano_data.slice(i, i + CHUNK_SIZE);
        for (let pano of pano_slice) {
            // console.log(pano)
            let metadata_promise = streetViewService.getPanorama({pano: pano["pano_id"]}, function(svPanoData, status) {
                if (status === google.maps.StreetViewStatus.OK) {
                    tiles = svPanoData.tiles;
                    new_pano_data.push({
                        pano_id: pano["pano_id"],
                        image_width: tiles.worldSize.width,
                        image_height: tiles.worldSize.height,
                        tile_width: tiles.tileSize.width,
                        tile_height: tiles.tileSize.height,
                        copyright: svPanoData.copyright,
                        center_heading: tiles.centerHeading,
                        origin_heading: tiles.originHeading,
                        origin_pitch: tiles.originPitch
                    });
                } else {
                    // no street view data available for this panorama.
                    //console.error(`Error loading Street View imagery for ${pano["pano_id"]}: ${status}`);
                    failed_to_retrieve_metadata.push({pano_id: pano["pano_id"]});
                }
            });

            metadata_promises.push(metadata_promise);
        }

        // wait for all metadata promises to resolve
        // TODO: add a final flag in order to post everything when all batches iterated over
        results = await Promise.allSettled(metadata_promises)

        // .then(results => {
        // see how many failed in chunk
        console.log(results.filter(result => result.status == "rejected").length);

        // check updated new_pano_data length
        console.log(new_pano_data.length);

        // check if this chunk was the last chunk
        last_chunk = i + CHUNK_SIZE >= filtered_pano_data.length;

        if (last_chunk) {
            // turn pano_data list into JSON
            let json_pano_data = JSON.stringify(new_pano_data);

            // use Blob in order to create download URL for the JSON file
            let pano_data_blob = new Blob([json_pano_data], {type: "application/json"});
            let pano_data_url  = URL.createObjectURL(pano_data_blob);

            // visualize link on webpage
            let a_pano_data = document.createElement('a');
            a_pano_data.href        = pano_data_url;
            a_pano_data.download    = OUTPUT_JSON;
            a_pano_data.textContent = `Download ${OUTPUT_JSON}`;

            document.getElementById('json-download').appendChild(a_pano_data);

            // turn unretrievable panos list into JSON
            let unretrievable_panos_json = JSON.stringify(failed_to_retrieve_metadata);

            // use Blob in order to create download URL for the JSON file
            let unretrievable_panos_blob = new Blob([unretrievable_panos_json], {type: "application/json"});
            let unretrievable_panos_url  = URL.createObjectURL(unretrievable_panos_blob);

            // visualize link on webpage
            let a_unretrievable = document.createElement('a');
            a_unretrievable.href        = unretrievable_panos_url;
            a_unretrievable.download    = UNRETRIEVABLE_PANOS_JSON;
            a_unretrievable.textContent = `Download ${UNRETRIEVABLE_PANOS_JSON}`;

            document.getElementById('json-download').appendChild(a_unretrievable);
        } else {
            // sleep for a minute to not exceed QPM rate-limit on Google's end.
            console.log("Sleeping for 1 min to not exceed QPM limit")
            await new Promise(r => setTimeout(r, 60000));
            console.log("Done Sleeping")
        }
    }
}

function initialize() {
    // Get pano_ids from Project Sidewalk api.
    // Afterwards, filter for panos with no image size data and query for said image metadata.
    getPanos(SIDEWALK_SERVER_FQDN + '/adminapi/panos', (data) => flag_panos_for_redownload(data));
}
import pandas as pd

CITY = 'amsterdam'
# JSON_TO_CONVERT = f'{CITY}_unretrievable_panos.json'
# CSV_OUTPUT = f'{CITY}_unretrievable_panos.csv'

JSON_TO_CSV_MAP = {
    f'{CITY}_pano_image_data.json': f'{CITY}_pano_image_data.csv',
    f'{CITY}_unretrievable_panos.json': f'{CITY}_unretrievable_panos.csv'
}

for json_file in JSON_TO_CSV_MAP.keys():
    with open(json_file, encoding='utf-8') as f:
        df = pd.read_json(f)

    df.to_csv(JSON_TO_CSV_MAP[json_file], encoding='utf-8', index=False)

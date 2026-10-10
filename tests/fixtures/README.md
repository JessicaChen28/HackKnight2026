# Real Azgaar fixture

`frepoyland.json.gz` contains `minimal` (the unfiltered Minimal JSON object) and
`geometry_source` (the live cell-coordinate snapshot) for **Frepoyland**, seed
`514444308`, captured from https://azgaar.github.io/Fantasy-Map-Generator/ on
October 10, 2026 by this project's Playwright pipeline.

The fixture includes 529 settlements, 21 state records (including neutral),
20 land features, and 6 water features (including lakes and two oceans).
It is compressed solely to keep the repository small. Tests need no browser,
network, or user exports. The original exports remain in the ignored `exports/`
directory. Azgaar is [MIT licensed](https://github.com/Azgaar/Fantasy-Map-Generator/blob/master/LICENSE).

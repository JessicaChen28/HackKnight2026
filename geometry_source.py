"""Read Azgaar's geographic cell mesh without modifying browser state."""

# Feature.vertices describes one outline, not all holes, and is empty for oceans.
# Cell rings capture the whole coverage, including map edges and nested islands.
EXTRACT_GEOMETRY = """() => {
    const {cells, vertices, features} = window.pack;
    if (!cells?.v || !cells?.f || !vertices?.p) {
        throw new Error('Azgaar cell/vertex coordinates are unavailable');
    }
    if (!cells.i || cells.i.length > 100000 || !Array.isArray(features) || features.length > 100000) {
        throw new Error('Azgaar geometry exceeds supported collection limits');
    }
    let vertexCount = 0;
    const groups = new Map();
    for (const feature of features) {
        if (feature && Number.isInteger(feature.i)) {
            groups.set(feature.i, {feature_id: feature.i, cells: []});
        }
    }
    for (const cell of cells.i) {
        if (!Array.isArray(cells.v[cell]) || (vertexCount += cells.v[cell].length) > 1000000) {
            throw new Error('Azgaar geometry exceeds supported vertex limits');
        }
        const group = groups.get(cells.f[cell]);
        if (!group) throw new Error(`Unknown feature for cell ${cell}`);
        const ring = cells.v[cell].map(vertex => {
            const p = vertices.p[vertex];
            if (!p || p.length < 2 || !p.every(Number.isFinite)) {
                throw new Error(`Invalid coordinate for vertex ${vertex}`);
            }
            return [p[0], p[1]];
        });
        if (ring.length < 3) throw new Error(`Incomplete polygon for cell ${cell}`);
        group.cells.push(ring);
    }
    return {
        schema_version: 1,
        coordinate_system: 'azgaar_map',
        source: 'pack.cells.v + pack.vertices.p, grouped by pack.cells.f',
        seed: window.options.map.seed,
        map_width: window.options.map.graph.width,
        map_height: window.options.map.graph.height,
        features: Array.from(groups.values())
    };
}"""


def extract_geometry(page) -> dict:
    """Requires the same completed world that produced the Minimal JSON."""
    return page.evaluate(EXTRACT_GEOMETRY)

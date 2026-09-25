# LandXML Road & Terrain GIS Tools

A QGIS Processing provider for inspecting and importing LandXML terrain and road-design data. Originally created by **Edmond Akello** under GPL-2.0-or-later; the original attribution and license remain in place.

## Compatibility and validation

- **QGIS 4.2.2:** provider registration and Processing algorithms tested with a macOS QGIS 4.2.2 runtime. A desktop smoke test loaded the plugin, inspected a LandXML terrain export, and created and displayed its TIN GeoTIFF. The visual overlay is not a survey-control check.
- **QGIS 3.44:** the code uses documented 3.44 APIs, including `QMetaType` field types, but has not been run in a QGIS 3.44 installation. Metadata declares 3.44–4.99 to allow testing on both series.
- **Autodesk Civil 3D:** standard LandXML road and terrain entities are covered by synthetic regression fixtures. The upstream project documents earlier validation against a large Civil 3D export; that file is not in this repository and the result is not reproduced by this test suite.
- **Bentley OpenRoads Designer:** terrain and breakline structures were examined in private LandXML 1.2 exports and reproduced in a committed synthetic fixture. The committed suite tests those structures without redistributing project data. OpenRoads alignment, profile and corridor exports have not been supplied or validated. The standard LandXML geometry parser may handle them when they use supported elements, but that is unverified for OpenRoads.

## OpenRoads Designer

**OpenRoads Designer → Export LandXML → QGIS LandXML Road & Terrain GIS Tools → Inspect LandXML → import terrain, alignment, profile, or features.**

The plugin reads exported LandXML. It does **not** parse DGN files or proprietary Bentley data. Export the entities needed for the QGIS workflow, inspect the file first, and compare results with known survey control. Bentley lists LandXML among OpenRoads Designer's export formats: [Bentley product documentation](https://www.bentley.com/products/openroads-designer/).

The examined OpenRoads terrain exports contain `Surfaces/Surface/Definition/Pnts` and `Faces`, plus `SourceData/Breaklines/Breakline/PntList3D`. Breaklines carry `Feature@code` and `Property` IDs, but no source feature name. The importer retains that code, breakline type and IDs without treating IDs as names. The exports declare `USSurveyFoot` but omit a CRS and vertical unit. The structural comparison is in [docs/openroads-sample-audit.md](docs/openroads-sample-audit.md).

## Processing tools

| Group | Tools |
| --- | --- |
| Utilities | Inspect LandXML |
| Terrain conversion | TIN to GeoTIFF |
| Vector extraction | Horizontal alignments |
| Road design extraction | Vertical profile graph and optional control points; 3D centerlines; cross sections and optional points; station points; Complete Road Design to GeoPackage |
| Surface extraction | TIN surface boundary; feature lines; breaklines |

**Inspect LandXML** reports vendor/evidence, version, horizontal and vertical units, declared CRS, axis-order uncertainty, surfaces with point/face counts, alignments, profiles, cross sections, breaklines, feature lines, source bounds, elevation range and warnings. It can save JSON. Inspection never changes coordinates.

The Complete Road Design export writes an engineering GeoPackage and optional GeoTIFF/contour outputs. `alignment_elements` retains radius, curve length, spiral length and direction per element. Profiles and profile controls use **station/elevation graph coordinates with no map CRS**; they are not mapped onto the horizontal alignment. Breakline and feature-line layers preserve source names/descriptions when present, surface name, type, code and source IDs. When multiple surfaces are present, the exporter processes all unless a name is selected.

## Coordinate and unit handling

LandXML numeric coordinates are kept in stored order. The plugin does not infer X/Y order, datum, false easting/northing, offset, CRS, or elevations. The user must choose an output CRS. Swap, offset, 2-D Helmert and reprojection are available only as explicit Processing choices. Reprojection also requires an explicitly chosen source CRS. A LandXML CRS declaration is shown for reference and is never applied automatically.

`USSurveyFoot`, international `foot` and `meter` are distinct. If LandXML declares one of these units, the selected CRS used to interpret stored coordinates must have matching horizontal units; otherwise Processing stops. Reprojection uses the selected source and output CRSs. The plugin does not infer a missing vertical unit or convert elevations. A source CRS and axis order must be verified against project metadata and control before design use.

## Supported entities and limits

- TIN points and triangular faces, multiple surfaces, TIN edge boundaries, breakline and feature-line `PntList3D`/`PntList2D` geometry.
- Horizontal `Line`, circular `Curve`, and **explicitly declared clothoid** `Spiral`; compound geometry is kept as connected segments. Unsupported or disconnected geometry is reported rather than joined into a false line.
- `PVI` and `ParaCurve` vertical controls, tangent grades, crest/sag classification and sampled profile graph lines.
- Absolute-coordinate cross-section point lists, with source station and elevation where supplied. Station-relative design-template sections are reported and skipped because their map positions cannot be established from the section alone.
- 3D centerlines require one matching vertical profile covering the full alignment station range. Missing Z values are not filled with zero.

Current limits: circular vertical curves and non-clothoid spirals are reported as unsupported; station equations and semantic terrain void/hole classification are not implemented; 2D and 3D line records must be exported separately through individual Processing tools. OpenRoads alignment, profile and corridor behavior needs a real exported sample. LandXML export from QGIS was considered separately and is not included in 1.5.0. GeoPackage, GeoTIFF and 3D vector output are supported; no DGN writer is attempted.

## Install the development plugin

In QGIS, choose **Settings → User Profiles → Open Active Profile Folder**. Inside that folder, open `python/plugins` (create those directories if needed) and place this repository checkout there as a directory named `landxml_road_terrain`. A symbolic link to a checkout elsewhere also works. Restart QGIS, enable **LandXML Road & Terrain GIS Tools** in **Plugins → Manage and Install Plugins → Installed**, then search the **Processing Toolbox** for **Inspect LandXML**. QGIS supplies NumPy and GDAL/OGR; this plugin has no separately installed Python dependency.

## Test and manual check

Run the parser and headless QGIS Processing suite from an environment with QGIS Python bindings and GDAL/OGR available:

```sh
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

Some QGIS distributions require their bundled Python executable and environment variables. See [the compatibility audit](docs/qgis4-audit.md) for the tested runtime and coverage.

Manual check in QGIS 4.2.2:

The committed fixtures use synthetic coordinates. The example EPSG choices below check Processing unit behavior only and do not establish a real-world location.

1. Run **Inspect LandXML** on `tests/fixtures/openroads/terrain_minimal.xml`; confirm OpenRoads Designer, `USSurveyFoot`, one TIN surface, one breakline, and warnings for missing CRS/vertical unit.
2. Run **Extract LandXML Breaklines** with **Use stored coordinates** and an explicitly selected ftUS CRS for the synthetic fixture. Confirm one 3D line and `feature_code=Breakline`, `surface_name=Synthetic terrain`, `source_user_id=42`.
3. Run **LandXML TIN to GeoTIFF** on the same fixture at pixel size 1; inspect its CRS, extent, NoData and elevation range. Repeat with a deliberately mismatched meter CRS and confirm Processing rejects the unit mismatch.
4. Run alignment, profile and cross-section tools on `tests/fixtures/civil3d/road_minimal.xml` with a meter CRS such as EPSG:26915. Check the line/curve/clothoid chain, profile graph's absent map CRS, control grades and section Z values.
5. On a copy of an actual project export, run **Inspect** first, choose source/output CRSs and any explicit coordinate operation, then compare source and output bounds with known control. Do not assign a CRS solely from coordinate magnitudes.

Only synthetic LandXML fixtures are committed. Private project exports should remain outside Git.

## History and license

See [CHANGELOG.md](CHANGELOG.md) and [HISTORY.md](HISTORY.md). Original work and attribution: [EdmondAkello/LandXML-Road-and-Terrain-GIS-Tools](https://github.com/EdmondAkello/LandXML-Road-and-Terrain-GIS-Tools). GPL-2.0-or-later; see [LICENSE](LICENSE).

import math
import os
from .landxml.common import descendants, first_descendant
from .landxml.parser import load_document
from .landxml.features import read_line_features
from .landxml.profile import read_profile_controls
from .landxml.sections import read_cross_sections
from .processing_common import BoundsTracker, coordinate_choices
from qgis.core import (
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingParameterFile,
    QgsProcessingParameterCrs,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterEnum,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingException,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPoint,
    QgsPointXY,
    QgsFields,
    QgsField,
    QgsWkbTypes,
)
from .compat import FIELD_STRING, FIELD_DOUBLE, FIELD_INT
from .core import transform_vertices
from .landxml.geometry import read_alignments
from .core import read_tin
from .landxml.profile import read_vertical_profile
from .params import number_param

METHODS = [
    "Use stored coordinates",
    "Swap X / Y",
    "Apply X / Y offset",
    "Swap X / Y + offset",
    "2-D Helmert (similarity)",
    "Reproject from source CRS",
]


def _xy(text):
    v = [float(x) for x in (text or "").split()[:2]]
    if len(v) < 2:
        raise ValueError("Expected X Y coordinates")
    return v[0], v[1]


def _param_common(a):
    a.addParameter(
        QgsProcessingParameterString(
            "ALIGNMENT", "Alignment name (blank = all)", defaultValue="", optional=True
        )
    )
    a.addParameter(
        QgsProcessingParameterEnum(
            "METHOD", "Coordinate interpretation", options=METHODS, defaultValue=0
        )
    )
    a.addParameter(number_param("DX", "Delta X / Easting", 0, -1e9, 1e9, decimals=4))
    a.addParameter(number_param("DY", "Delta Y / Northing", 0, -1e9, 1e9, decimals=4))
    a.addParameter(
        number_param("ROTATION", "Rotation (degrees)", 0, -360, 360, decimals=8)
    )
    a.addParameter(number_param("SCALE", "Scale factor", 1, 1e-8, 1000, decimals=10))
    a.addParameter(
        QgsProcessingParameterCrs(
            "SOURCE_CRS",
            "Source CRS (for reprojection)",
            defaultValue=None,
            optional=True,
        )
    )
    a.addParameter(
        QgsProcessingParameterCrs(
            "OUTPUT_CRS", "Output CRS (result layer / raster)", defaultValue=None
        )
    )


def _method_map(i):
    return ["stored", "swap", "offset", "swap_offset", "helmert", "reproject"][i]


def _transform_params(parameters, context, algorithm, feedback, path):
    method, out, source, params, _document = coordinate_choices(
        algorithm, parameters, context, feedback, path
    )
    return method, out, source, params


def _sink(a, parameters, context, fields, wkb):
    return a.parameterAsSink(
        parameters,
        "OUTPUT",
        context,
        fields,
        wkb,
        a.parameterAsCrs(parameters, "OUTPUT_CRS", context),
    )


def _line_chain(points):
    return QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in points])


def _alignment_parts(path, segment_length=5.0, alignment_filter=""):
    alignments, unsupported = read_alignments(path, segment_length)
    result = []
    for alignment in alignments:
        if alignment_filter and alignment["name"] != alignment_filter:
            continue
        result.append(
            (
                alignment["name"],
                alignment["desc"],
                alignment["sta_start"],
                alignment["sta_end"],
                [segment["points"] for segment in alignment["segments"]],
                unsupported,
            )
        )
    return result


class ProfileAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self):
        return ProfileAlgorithm()

    def name(self):
        return "landxml_profiles_to_vector"

    def displayName(self):
        return "Extract LandXML Profiles"

    def group(self):
        return "Road design extraction"

    def groupId(self):
        return "road_design_extraction"

    def shortHelpString(self):
        return "Extract vertical profile geometry and PVI/grade data into GIS lines and profile points."

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                "INPUT",
                "LandXML file",
                behavior=QgsProcessingParameterFile.Behavior.File,
                fileFilter="LandXML (*.xml *.landxml)",
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                "ALIGNMENT",
                "Alignment name (blank = all)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                "PROFILE", "Profile name (blank = all)", defaultValue="", optional=True
            )
        )
        self.addParameter(
            number_param(
                "POINT_INTERVAL",
                "Profile sampling interval (source station units)",
                5,
                0.01,
                10000,
                decimals=2,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                "OUTPUT",
                "Profile graph lines (station/elevation, no map CRS)",
                type=QgsProcessing.SourceType.TypeVectorLine,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                "CONTROL_POINTS",
                "Profile control points (station/elevation, no map CRS)",
                type=QgsProcessing.SourceType.TypeVectorPoint,
                optional=True,
            )
        )

    def processAlgorithm(self, p, c, fb):
        path = self.parameterAsFile(p, "INPUT", c)
        alignment_filter = self.parameterAsString(p, "ALIGNMENT", c).strip()
        profile_filter = self.parameterAsString(p, "PROFILE", c).strip()
        if not path or not os.path.isfile(path):
            raise QgsProcessingException("Input LandXML file does not exist.")
        doc = load_document(path)
        fb.pushInfo(
            "Profile graph coordinates are station/elevation in source units; no map CRS is assigned."
        )
        fields = QgsFields()
        for n in (
            "alignment_name",
            "profile_name",
            "source_vendor",
            "source_file",
            "source_name",
            "source_description",
        ):
            fields.append(QgsField(n, FIELD_STRING, len=254))
        for n in ("station_start", "station_end"):
            fields.append(QgsField(n, FIELD_DOUBLE))
        sink, dest = self.parameterAsSink(
            p,
            "OUTPUT",
            c,
            fields,
            QgsWkbTypes.Type.LineString,
            QgsCoordinateReferenceSystem(),
        )
        if sink is None:
            raise QgsProcessingException("Could not create profile graph layer.")
        control_fields = QgsFields()
        for name in (
            "alignment_name",
            "profile_name",
            "control_type",
            "curve_type",
            "source_vendor",
            "source_file",
            "source_name",
            "source_description",
        ):
            control_fields.append(QgsField(name, FIELD_STRING, len=254))
        for name in ("station", "elevation", "grade_in", "grade_out", "curve_length"):
            control_fields.append(QgsField(name, FIELD_DOUBLE))
        control_sink, control_dest = (None, None)
        if p.get("CONTROL_POINTS"):
            control_sink, control_dest = self.parameterAsSink(
                p,
                "CONTROL_POINTS",
                c,
                control_fields,
                QgsWkbTypes.Type.Point,
                QgsCoordinateReferenceSystem(),
            )
            if control_sink is None:
                raise QgsProcessingException(
                    "Could not create profile control point layer."
                )
        n = 0
        interval = max(self.parameterAsDouble(p, "POINT_INTERVAL", c), 0.01)
        for alignment_name, prof in doc.profiles():
            alignment_name = alignment_name or ""
            if alignment_filter and alignment_name != alignment_filter:
                continue
            if fb.isCanceled():
                raise QgsProcessingException("Profile extraction cancelled.")
            if profile_filter and prof.attrib.get("name", "") != profile_filter:
                continue
            # ProfAlign is a flat PVI/ParaCurve/CircCurve sequence, not a
            # ProfileGeom/Line/Curve wrapper -- see landxml.profile.
            pts = read_vertical_profile(prof, interval)
            if len(pts) < 2:
                continue
            sta, end = pts[0][0], pts[-1][0]
            # Represent profile as station,elevation pairs in GIS coordinate space; horizontal X=station, Y=elevation.
            geom = QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in pts])
            feat = QgsFeature(fields)
            feat.setGeometry(geom)
            feat.setAttributes(
                [
                    alignment_name,
                    prof.attrib.get("name"),
                    doc.vendor,
                    os.path.basename(path),
                    prof.attrib.get("name"),
                    prof.attrib.get("desc"),
                    sta,
                    end,
                ]
            )
            sink.addFeature(feat)
            if control_sink is not None:
                for control in read_profile_controls(prof):
                    point = QgsFeature(control_fields)
                    point.setGeometry(
                        QgsGeometry.fromPointXY(
                            QgsPointXY(control["station"], control["elevation"])
                        )
                    )
                    point.setAttributes(
                        [
                            alignment_name,
                            prof.attrib.get("name"),
                            control["control_type"],
                            control["curve_type"],
                            doc.vendor,
                            os.path.basename(path),
                            prof.attrib.get("name"),
                            prof.attrib.get("desc"),
                            control["station"],
                            control["elevation"],
                            control["grade_in"],
                            control["grade_out"],
                            control["curve_length"],
                        ]
                    )
                    control_sink.addFeature(point)
            n += 1
        return {
            "OUTPUT": dest,
            "CONTROL_POINTS": control_dest or "",
            "PROFILE_COUNT": n,
        }


class Centerline3DAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self):
        return Centerline3DAlgorithm()

    def name(self):
        return "landxml_3d_centerlines"

    def displayName(self):
        return "Create 3D Road Centerlines"

    def group(self):
        return "Road design extraction"

    def groupId(self):
        return "road_design_extraction"

    def shortHelpString(self):
        return "Creates 3D centerlines by combining horizontal alignment geometry with the matching LandXML vertical profile when available."

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                "INPUT",
                "LandXML file",
                behavior=QgsProcessingParameterFile.Behavior.File,
                fileFilter="LandXML (*.xml *.landxml)",
            )
        )
        _param_common(self)
        self.addParameter(
            number_param(
                "SEGMENT",
                "Maximum geometry segment length (source horizontal units)",
                5,
                0.01,
                10000,
                decimals=2,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                "OUTPUT", "3D centerlines", type=QgsProcessing.SourceType.TypeVectorLine
            )
        )

    def processAlgorithm(self, p, c, fb):
        path = self.parameterAsFile(p, "INPUT", c)
        m, out, src, tp = _transform_params(p, c, self, fb, path)
        alignment_filter = self.parameterAsString(p, "ALIGNMENT", c).strip()
        seg = self.parameterAsDouble(p, "SEGMENT", c)
        fields = QgsFields()
        for n, ln in (
            ("alignment", 254),
            ("sta_start", 80),
            ("sta_end", 80),
            ("z_source", 80),
        ):
            fields.append(QgsField(n, FIELD_STRING, len=ln))
        sink, dest = _sink(self, p, c, fields, QgsWkbTypes.Type.LineStringZ)
        root = load_document(path).root
        # Build sampled vertical profiles keyed by alignment name. ProfAlign
        # is a flat PVI/ParaCurve/CircCurve sequence -- see landxml.profile.
        # A ProfAlign's own `name` is a profile label (e.g. "frl" = finish
        # road level), not the owning alignment's name: real LandXML nests
        # <Profile><ProfAlign> inside the <Alignment> it belongs to, it does
        # not cross-reference it by an `alignment=` attribute equal to a
        # ProfAlign name. Associate by walking each Alignment's own subtree
        # first; fall back to an explicit alignment= reference if an exporter
        # uses that pattern instead.
        profiles = {}
        for a_el in descendants(root, "Alignment"):
            key = a_el.attrib.get("name", "")
            if key in profiles:
                continue
            pa = first_descendant(a_el, "ProfAlign")
            if pa is not None:
                samples = read_vertical_profile(pa, seg)
                if samples:
                    profiles[key] = samples
        for prof in descendants(root, "ProfAlign"):
            ref = prof.attrib.get("alignment")
            if ref and ref not in profiles:
                samples = read_vertical_profile(prof, seg)
                if samples:
                    profiles[ref] = samples

        def z_at(samples, sta):
            if not samples or sta < samples[0][0] or sta > samples[-1][0]:
                return None
            for i in range(1, len(samples)):
                st0, z0 = samples[i - 1]
                st1, z1 = samples[i]
                if sta <= st1:
                    q = (sta - st0) / max(st1 - st0, 1e-12)
                    return z0 + q * (z1 - z0)
            return samples[-1][1]

        parts_all = _alignment_parts(path, seg, alignment_filter)
        for ai, (name, desc, sta0, sta1, parts, unsupported) in enumerate(parts_all):
            if fb.isCanceled():
                break
            pts = []
            for part in parts:
                if pts and part and pts[-1] == part[0]:
                    pts.extend(part[1:])
                else:
                    pts.extend(part)
            if len(pts) < 2:
                continue
            import numpy as np

            raw = np.asarray([[x, y, 0] for x, y in pts], dtype=float)
            cum = [0.0]
            for i in range(1, len(raw)):
                cum.append(
                    cum[-1]
                    + math.hypot(raw[i, 0] - raw[i - 1, 0], raw[i, 1] - raw[i - 1, 1])
                )
            if not sta0:
                fb.pushWarning(
                    f"Skipped 3D centerline '{name}': alignment start station is missing."
                )
                continue
            s0 = float(sta0)
            prof = profiles.get(name)
            if not prof:
                fb.pushWarning(
                    f"Skipped 3D centerline '{name}': no matching vertical profile; Z was not fabricated."
                )
                continue
            z = [z_at(prof, s0 + d) for d in cum]
            if any(value is None for value in z):
                fb.pushWarning(
                    f"Skipped 3D centerline '{name}': profile does not cover the full alignment station range."
                )
                continue
            arr = transform_vertices(raw, method=m, **tp)
            xy = arr[:, :2]
            points3 = [
                QgsPoint(float(x), float(y), float(zz)) for (x, y), zz in zip(xy, z)
            ]
            feat = QgsFeature(fields)
            feat.setGeometry(QgsGeometry.fromPolyline(points3))
            feat.setAttributes([name, sta0, sta1, "Vertical profile"])
            sink.addFeature(feat)
            fb.setProgress(int(100 * (ai + 1) / max(1, len(parts_all))))
        return {"OUTPUT": dest}


class SurfaceBoundaryAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self):
        return SurfaceBoundaryAlgorithm()

    def name(self):
        return "landxml_surface_boundary"

    def displayName(self):
        return "Extract TIN Surface Boundary"

    def group(self):
        return "Surface extraction"

    def groupId(self):
        return "surface_extraction"

    def shortHelpString(self):
        return "Extracts the outer boundary of the LandXML TIN as a polygon/line."

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                "INPUT",
                "LandXML file",
                behavior=QgsProcessingParameterFile.Behavior.File,
                fileFilter="LandXML (*.xml *.landxml)",
            )
        )
        _param_common(self)
        self.addParameter(
            QgsProcessingParameterString(
                "SURFACE",
                "Surface name (blank = first)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                "OUTPUT",
                "Surface boundary",
                type=QgsProcessing.SourceType.TypeVectorLine,
            )
        )

    def processAlgorithm(self, p, c, fb):
        path = self.parameterAsFile(p, "INPUT", c)
        surf = self.parameterAsString(p, "SURFACE", c) or None
        xyz, faces, meta = read_tin(path, surf)
        m, out, src, tp = _transform_params(p, c, self, fb, path)
        xyz = transform_vertices(xyz, method=m, **tp)
        transformed_bounds = BoundsTracker()
        transformed_bounds.add(xyz)
        transformed_bounds.report(fb)
        edges = {}
        for a, b, cc in faces:
            for u, v in [(a, b), (b, cc), (cc, a)]:
                k = tuple(sorted((int(u), int(v))))
                edges[k] = edges.get(k, 0) + 1
        bedges = [k for k, v in edges.items() if v == 1]
        adj = {}
        for a, b in bedges:
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
        lines = []
        unused = set(bedges)
        while unused:
            a, b = next(iter(unused))
            line = [a, b]
            unused.remove((a, b))
            unused.discard((b, a))
            cur = b
            prev = a
            while True:
                nxts = [
                    n
                    for n in adj.get(cur, [])
                    if n != prev and (min(cur, n), max(cur, n)) in unused
                ]
                if not nxts:
                    break
                nxt = nxts[0]
                unused.discard((min(cur, nxt), max(cur, nxt)))
                line.append(nxt)
                prev, cur = cur, nxt
            lines.append(line)
        fields = QgsFields()
        fields.append(QgsField("surface", FIELD_STRING, len=254))
        for name in (
            "surface_name",
            "source_vendor",
            "source_file",
            "source_name",
            "source_description",
        ):
            fields.append(QgsField(name, FIELD_STRING, len=254))
        sink, dest = _sink(self, p, c, fields, QgsWkbTypes.Type.LineString)
        if sink is None:
            raise QgsProcessingException("Could not create surface boundary layer.")
        document = load_document(path)
        surface = next(
            (
                item
                for item in document.surfaces()
                if item.attrib.get("name") == meta["surface_name"]
            ),
            None,
        )
        for line in lines:
            f = QgsFeature(fields)
            f.setGeometry(
                QgsGeometry.fromPolylineXY(
                    [QgsPointXY(xyz[i, 0], xyz[i, 1]) for i in line]
                )
            )
            f.setAttributes(
                [
                    meta["surface_name"],
                    meta["surface_name"],
                    document.vendor,
                    os.path.basename(path),
                    meta["surface_name"],
                    surface.attrib.get("desc") if surface is not None else None,
                ]
            )
            sink.addFeature(f)
        return {"OUTPUT": dest}


class StationPointsAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self):
        return StationPointsAlgorithm()

    def name(self):
        return "landxml_station_points"

    def displayName(self):
        return "Create Alignment Station Points"

    def group(self):
        return "Road design extraction"

    def groupId(self):
        return "road_design_extraction"

    def shortHelpString(self):
        return "Creates points along LandXML alignments at a user-defined station interval."

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                "INPUT",
                "LandXML file",
                behavior=QgsProcessingParameterFile.Behavior.File,
                fileFilter="LandXML (*.xml *.landxml)",
            )
        )
        _param_common(self)
        self.addParameter(
            number_param("INTERVAL", "Station interval", 20, 0.01, 100000, decimals=2)
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                "INCLUDE_ENDPOINT", "Include alignment end station", defaultValue=True
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                "OUTPUT",
                "Station points",
                type=QgsProcessing.SourceType.TypeVectorPoint,
            )
        )

    def processAlgorithm(self, p, c, fb):
        path = self.parameterAsFile(p, "INPUT", c)
        m, out, src, tp = _transform_params(p, c, self, fb, path)
        alignment_filter = self.parameterAsString(p, "ALIGNMENT", c).strip()
        include_end = self.parameterAsBoolean(p, "INCLUDE_ENDPOINT", c)
        fields = QgsFields()
        for n in ("alignment", "station", "bearing_deg"):
            fields.append(
                QgsField(n, FIELD_DOUBLE if n != "alignment" else FIELD_STRING, len=254)
            )
        sink, dest = _sink(self, p, c, fields, QgsWkbTypes.Type.Point)
        interval = self.parameterAsDouble(p, "INTERVAL", c)
        for name, desc, sta0, sta1, parts, _ in _alignment_parts(
            path, 5.0, alignment_filter
        ):
            if not sta0:
                fb.pushWarning(
                    f"Skipped station points for '{name}': start station is missing."
                )
                continue
            pts = []
            for part in parts:
                pts.extend(part[1:] if pts and part and pts[-1] == part[0] else part)
            if len(pts) < 2:
                continue
            import numpy as np

            raw = np.asarray([[x, y, 0] for x, y in pts], dtype=float)
            cum = [0.0]
            for i in range(1, len(raw)):
                cum.append(
                    cum[-1]
                    + math.hypot(raw[i, 0] - raw[i - 1, 0], raw[i, 1] - raw[i - 1, 1])
                )
            s = 0.0
            stations = []
            while s < cum[-1] - 1e-8:
                stations.append(s)
                s += interval
            if include_end:
                stations.append(cum[-1])
            for distance in stations:
                i = next(
                    (j for j in range(1, len(cum)) if cum[j] >= distance), len(cum) - 1
                )
                prev = i - 1
                segment = max(cum[i] - cum[prev], 1e-12)
                q = (distance - cum[prev]) / segment
                source = raw[prev] + q * (raw[i] - raw[prev])
                xy = transform_vertices(
                    np.asarray([source], dtype=float), method=m, **tp
                )[0]
                bearing = (
                    math.degrees(
                        math.atan2(raw[i, 0] - raw[prev, 0], raw[i, 1] - raw[prev, 1])
                    )
                    + 360
                ) % 360
                f = QgsFeature(fields)
                f.setGeometry(
                    QgsGeometry.fromPointXY(QgsPointXY(float(xy[0]), float(xy[1])))
                )
                f.setAttributes([name, float(sta0) + distance, bearing])
                sink.addFeature(f)
        return {"OUTPUT": dest}


class CrossSectionsAlgorithm(QgsProcessingAlgorithm):
    def createInstance(self):
        return CrossSectionsAlgorithm()

    def name(self):
        return "landxml_cross_sections"

    def displayName(self):
        return "Extract LandXML Cross-Sections"

    def group(self):
        return "Road design extraction"

    def groupId(self):
        return "road_design_extraction"

    def shortHelpString(self):
        return "Extracts LandXML CrossSect geometry as line features. Supports PntList3D/PntList2D-style section point lists and station attributes where present. CrossSect records that only carry station-relative DesignCrossSectSurf/CrossSectPnt design-template geometry (pavement/subbase layers etc.), without an absolute-coordinate point list, are skipped and reported in Processing messages rather than being placed at a fabricated position."

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                "INPUT",
                "LandXML file",
                behavior=QgsProcessingParameterFile.Behavior.File,
                fileFilter="LandXML (*.xml *.landxml)",
            )
        )
        _param_common(self)
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                "OUTPUT",
                "Cross-section lines",
                type=QgsProcessing.SourceType.TypeVectorLine,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                "POINTS",
                "Cross-section points",
                type=QgsProcessing.SourceType.TypeVectorPoint,
                optional=True,
            )
        )

    def processAlgorithm(self, p, c, fb):
        path = self.parameterAsFile(p, "INPUT", c)
        m, out, src, tp = _transform_params(p, c, self, fb, path)
        alignment_filter = self.parameterAsString(p, "ALIGNMENT", c).strip()
        doc = load_document(path)
        records, warnings = read_cross_sections(doc.root)
        records = [
            record
            for record in records
            if not alignment_filter or record.alignment_name == alignment_filter
        ]
        dimensions = {len(record.points[0]) for record in records}
        if len(dimensions) > 1:
            raise QgsProcessingException(
                "Selected cross sections mix 2D and 3D coordinates; export them separately."
            )
        dim = 3 if dimensions == {3} else 2
        fields = QgsFields()
        for name in (
            "alignment_name",
            "source_name",
            "source_description",
            "source_vendor",
            "source_file",
        ):
            fields.append(QgsField(name, FIELD_STRING, len=254))
        fields.append(QgsField("station", FIELD_DOUBLE))
        fields.append(QgsField("point_count", FIELD_INT))
        wkb = QgsWkbTypes.Type.LineStringZ if dim == 3 else QgsWkbTypes.Type.LineString
        sink, dest = _sink(self, p, c, fields, wkb)
        if sink is None:
            raise QgsProcessingException("Could not create cross-section line layer.")
        point_fields = QgsFields()
        for name in ("alignment_name", "source_name", "source_vendor", "source_file"):
            point_fields.append(QgsField(name, FIELD_STRING, len=254))
        for name in ("station", "offset", "elevation"):
            point_fields.append(QgsField(name, FIELD_DOUBLE))
        point_fields.append(QgsField("point_index", FIELD_INT))
        point_sink, point_dest = None, None
        if p.get("POINTS"):
            point_wkb = QgsWkbTypes.Type.PointZ if dim == 3 else QgsWkbTypes.Type.Point
            point_sink, point_dest = self.parameterAsSink(
                p, "POINTS", c, point_fields, point_wkb, out
            )
            if point_sink is None:
                raise QgsProcessingException(
                    "Could not create cross-section point layer."
                )
        import numpy as np

        transformed_bounds = BoundsTracker()
        for record in records:
            if fb.isCanceled():
                raise QgsProcessingException("Cross-section extraction cancelled.")
            arr = transform_vertices(
                np.asarray(record.points, dtype=float), method=m, **tp
            )
            transformed_bounds.add(arr)
            geom = (
                QgsGeometry.fromPolyline([QgsPoint(*map(float, row)) for row in arr])
                if dim == 3
                else QgsGeometry.fromPolylineXY(
                    [QgsPointXY(*map(float, row)) for row in arr]
                )
            )
            feature = QgsFeature(fields)
            feature.setGeometry(geom)
            feature.setAttributes(
                [
                    record.alignment_name,
                    record.name,
                    record.description,
                    doc.vendor,
                    os.path.basename(path),
                    record.station,
                    len(record.points),
                ]
            )
            sink.addFeature(feature)
            if point_sink is not None:
                for index, row in enumerate(arr):
                    point = QgsFeature(point_fields)
                    point.setGeometry(
                        QgsGeometry.fromPoint(QgsPoint(*map(float, row)))
                        if dim == 3
                        else QgsGeometry.fromPointXY(QgsPointXY(*map(float, row)))
                    )
                    point.setAttributes(
                        [
                            record.alignment_name,
                            record.name,
                            doc.vendor,
                            os.path.basename(path),
                            record.station,
                            None,
                            float(row[2]) if dim == 3 else None,
                            index,
                        ]
                    )
                    point_sink.addFeature(point)
        for warning in warnings[:20]:
            fb.pushWarning(warning)
        if len(warnings) > 20:
            fb.pushWarning(f"{len(warnings) - 20} additional sections were skipped.")
        transformed_bounds.report(fb)
        return {
            "OUTPUT": dest,
            "POINTS": point_dest or "",
            "SECTION_COUNT": len(records),
        }


class GenericLinesAlgorithm(QgsProcessingAlgorithm):
    MODE = "FEATURE"

    def createInstance(self):
        return GenericLinesAlgorithm(self.mode)

    def __init__(self, mode="FEATURE"):
        super().__init__()
        self.mode = mode

    def name(self):
        return "landxml_" + (
            "feature_lines" if self.mode == "FEATURE" else "breaklines"
        )

    def displayName(self):
        return "Extract LandXML " + (
            "Feature Lines" if self.mode == "FEATURE" else "Breaklines"
        )

    def group(self):
        return "Surface extraction"

    def groupId(self):
        return "surface_extraction"

    def shortHelpString(self):
        return "Extracts 3D polyline-style FeatureLine or Breakline records from LandXML when present. Unsupported structures are skipped with a Processing message."

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFile(
                "INPUT",
                "LandXML file",
                behavior=QgsProcessingParameterFile.Behavior.File,
                fileFilter="LandXML (*.xml *.landxml)",
            )
        )
        _param_common(self)
        self.addParameter(
            QgsProcessingParameterString(
                "NAME_FILTER",
                "Feature/breakline name (blank = all)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                "OUTPUT", "Output lines", type=QgsProcessing.SourceType.TypeVectorLine
            )
        )

    def processAlgorithm(self, p, c, fb):
        path = self.parameterAsFile(p, "INPUT", c)
        m, out, src, tp = _transform_params(p, c, self, fb, path)
        name_filter = self.parameterAsString(p, "NAME_FILTER", c).strip()
        doc = load_document(path)
        records, warnings = read_line_features(doc.root)
        kind = "FeatureLine" if self.mode == "FEATURE" else "Breakline"
        records = [
            record
            for record in records
            if record.kind == kind and (not name_filter or record.name == name_filter)
        ]
        dimensions = {len(record.points[0]) for record in records}
        if len(dimensions) > 1:
            raise QgsProcessingException(
                "Selected lines mix 2D and 3D coordinates; export them separately to preserve their dimensions."
            )
        fields = QgsFields()
        for field in (
            "name",
            "description",
            "type",
            "source_name",
            "source_description",
            "feature_name",
            "feature_type",
            "surface_name",
            "feature_code",
            "breakline_type",
            "source_vendor",
            "source_file",
            "source_id",
            "source_user_id",
        ):
            fields.append(QgsField(field, FIELD_STRING, len=254))
        wkb = (
            QgsWkbTypes.Type.LineStringZ
            if dimensions == {3}
            else QgsWkbTypes.Type.LineString
        )
        sink, dest = _sink(self, p, c, fields, wkb)
        if sink is None:
            raise QgsProcessingException("Could not create output line layer.")
        import numpy as np

        transformed_bounds = BoundsTracker()
        for record in records:
            if fb.isCanceled():
                raise QgsProcessingException("Line extraction cancelled.")
            xyz = transform_vertices(
                np.asarray(record.points, dtype=float), method=m, **tp
            )
            transformed_bounds.add(xyz)
            if len(record.points[0]) == 3:
                geom = QgsGeometry.fromPolyline(
                    [QgsPoint(float(x), float(y), float(z)) for x, y, z in xyz]
                )
            else:
                geom = QgsGeometry.fromPolylineXY(
                    [QgsPointXY(float(x), float(y)) for x, y in xyz]
                )
            f = QgsFeature(fields)
            f.setGeometry(geom)
            f.setAttributes(
                [
                    record.name,
                    record.description,
                    record.kind,
                    record.name,
                    record.description,
                    record.name,
                    record.kind,
                    record.surface_name,
                    record.feature_code,
                    record.breakline_type,
                    doc.vendor,
                    os.path.basename(path),
                    record.properties.get("id"),
                    record.properties.get("UsrID"),
                ]
            )
            sink.addFeature(f)
        for warning in warnings[:20]:
            fb.pushWarning(warning)
        if len(warnings) > 20:
            fb.pushWarning(
                f"{len(warnings) - 20} additional line records were skipped."
            )
        transformed_bounds.report(fb)
        return {"OUTPUT": dest}


class FeatureLinesAlgorithm(GenericLinesAlgorithm):
    def __init__(self):
        super().__init__("FEATURE")

    def createInstance(self):
        return FeatureLinesAlgorithm()


class BreaklinesAlgorithm(GenericLinesAlgorithm):
    def __init__(self):
        super().__init__("BREAK")

    def createInstance(self):
        return BreaklinesAlgorithm()

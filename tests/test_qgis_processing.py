"""QGIS 4 runtime checks for provider registration and engineering outputs."""

# ruff: noqa: E402 - bootstrap the plugin package from its checkout directory

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
if "landxml_plugin" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "landxml_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsWkbTypes,
)
from osgeo import gdal, ogr

from landxml_plugin.provider import LandXMLTinToGeoTIFFProvider


OPENROADS = str(ROOT / "tests" / "fixtures" / "openroads" / "terrain_minimal.xml")
CIVIL3D = str(ROOT / "tests" / "fixtures" / "civil3d" / "road_minimal.xml")


class QgisProcessingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QgsApplication([], False)
        cls.app.initQgis()
        cls.provider = LandXMLTinToGeoTIFFProvider()
        cls.provider.loadAlgorithms()
        cls.algorithms = {
            algorithm.name(): algorithm for algorithm in cls.provider.algorithms()
        }

    def setUp(self):
        self.context = QgsProcessingContext()
        self.feedback = QgsProcessingFeedback()

    def _params(self, path, crs):
        return {
            "INPUT": path,
            "METHOD": 0,
            "OUTPUT_CRS": QgsCoordinateReferenceSystem(crs),
        }

    def test_provider_registers_all_algorithms(self):
        self.assertEqual(len(self.algorithms), 11)
        self.assertIn("inspect_landxml", self.algorithms)

    def test_inspect_report(self):
        result = self.algorithms["inspect_landxml"].processAlgorithm(
            {"INPUT": OPENROADS}, self.context, self.feedback
        )
        report = json.loads(result["REPORT"])
        self.assertEqual(report["vendor"], "OpenRoads Designer")
        self.assertEqual(report["horizontal_unit"], "USSurveyFoot")

    def test_crs_is_required_and_units_must_match(self):
        algorithm = self.algorithms["landxml_breaklines"]
        with self.assertRaisesRegex(QgsProcessingException, "Choose an output CRS"):
            algorithm.processAlgorithm(
                {"INPUT": OPENROADS, "OUTPUT": "memory:"}, self.context, self.feedback
            )
        params = self._params(OPENROADS, "EPSG:26915")
        params["OUTPUT"] = "memory:"
        with self.assertRaisesRegex(QgsProcessingException, "USSurveyFoot"):
            algorithm.processAlgorithm(params, self.context, self.feedback)
        self.assertNotEqual(Qgis.DistanceUnit.FeetUSSurvey, Qgis.DistanceUnit.Feet)
        generic = self._params(
            str(ROOT / "tests" / "fixtures" / "generic" / "multiple.xml"),
            "EPSG:2277",
        )
        generic["OUTPUT"] = "memory:"
        with self.assertRaisesRegex(QgsProcessingException, "foot"):
            self.algorithms["landxml_alignments_to_vector"].processAlgorithm(
                generic, self.context, self.feedback
            )

    def test_openroads_breakline_attributes_and_3d(self):
        params = self._params(OPENROADS, "EPSG:2277")
        params["OUTPUT"] = "memory:"
        result = self.algorithms["landxml_breaklines"].processAlgorithm(
            params, self.context, self.feedback
        )
        layer = self.context.getMapLayer(result["OUTPUT"])
        self.assertIsNotNone(layer)
        self.assertEqual(layer.featureCount(), 1)
        feature = next(layer.getFeatures())
        self.assertEqual(feature["surface_name"], "Synthetic terrain")
        self.assertEqual(feature["feature_code"], "Breakline")
        self.assertEqual(feature["source_user_id"], "42")
        self.assertTrue(QgsWkbTypes.hasZ(feature.geometry().wkbType()))

    def test_civil3d_alignment_profile_and_sections(self):
        alignment = self._params(CIVIL3D, "EPSG:26915")
        alignment["OUTPUT"] = "memory:"
        result = self.algorithms["landxml_alignments_to_vector"].processAlgorithm(
            alignment, self.context, self.feedback
        )
        layer = self.context.getMapLayer(result["OUTPUT"])
        self.assertEqual(layer.featureCount(), 1)
        feature = next(layer.getFeatures())
        self.assertEqual(feature["geometry_type"], "compound")
        self.assertEqual(feature["source_vendor"], "Civil 3D")

        profile = self.algorithms["landxml_profiles_to_vector"].processAlgorithm(
            {"INPUT": CIVIL3D, "OUTPUT": "memory:", "CONTROL_POINTS": "memory:"},
            self.context,
            self.feedback,
        )
        graph = self.context.getMapLayer(profile["OUTPUT"])
        points = self.context.getMapLayer(profile["CONTROL_POINTS"])
        self.assertEqual(graph.featureCount(), 1)
        self.assertEqual(points.featureCount(), 3)
        self.assertFalse(graph.crs().isValid())

        sections = self._params(CIVIL3D, "EPSG:26915")
        sections.update(OUTPUT="memory:", POINTS="memory:")
        result = self.algorithms["landxml_cross_sections"].processAlgorithm(
            sections, self.context, self.feedback
        )
        line_layer = self.context.getMapLayer(result["OUTPUT"])
        point_layer = self.context.getMapLayer(result["POINTS"])
        self.assertEqual(line_layer.featureCount(), 1)
        self.assertEqual(point_layer.featureCount(), 2)
        self.assertEqual(next(point_layer.getFeatures())["elevation"], 1)

    def test_tin_geotiff_and_complete_export(self):
        with tempfile.TemporaryDirectory() as directory:
            raster = self._params(OPENROADS, "EPSG:2277")
            raster.update(OUTPUT=str(Path(directory) / "tiny.tif"), RESOLUTION=1)
            self.algorithms["landxml_tin_to_geotiff"].processAlgorithm(
                raster, self.context, self.feedback
            )
            dataset = gdal.Open(raster["OUTPUT"])
            self.assertIsNotNone(dataset)
            self.assertEqual(dataset.RasterCount, 1)
            self.assertIn("2277", dataset.GetProjection())
            dataset = None

            folder = Path(directory) / "complete"
            complete = self._params(OPENROADS, "EPSG:2277")
            complete["OUTPUT_DIR"] = str(folder)
            for key in (
                "INCLUDE_ALIGNMENTS",
                "INCLUDE_CENTERLINES",
                "INCLUDE_PROFILES",
                "INCLUDE_STATIONS",
                "INCLUDE_CROSSSECTIONS",
                "INCLUDE_FEATURELINES",
                "INCLUDE_SURFACE_BOUNDARY",
                "INCLUDE_DEM",
                "INCLUDE_CONTOURS",
            ):
                complete[key] = False
            complete["INCLUDE_BREAKLINES"] = True
            self.algorithms["landxml_complete_road_design"].processAlgorithm(
                complete, self.context, self.feedback
            )
            package = ogr.Open(str(folder / "terrain_minimal_GIS.gpkg"))
            self.assertIsNotNone(package)
            layer = package.GetLayerByName("breaklines")
            self.assertIsNotNone(layer)
            self.assertEqual(layer.GetFeatureCount(), 1)
            self.assertEqual(next(iter(layer)).GetField("source_user_id"), "42")
            package = None

    def test_complete_export_alignment_elements_and_profile_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            params = self._params(CIVIL3D, "EPSG:26915")
            params["OUTPUT_DIR"] = directory
            for key in (
                "INCLUDE_CENTERLINES",
                "INCLUDE_STATIONS",
                "INCLUDE_CROSSSECTIONS",
                "INCLUDE_FEATURELINES",
                "INCLUDE_BREAKLINES",
                "INCLUDE_SURFACE_BOUNDARY",
                "INCLUDE_DEM",
                "INCLUDE_CONTOURS",
            ):
                params[key] = False
            params["INCLUDE_ALIGNMENTS"] = True
            params["INCLUDE_PROFILES"] = True
            self.algorithms["landxml_complete_road_design"].processAlgorithm(
                params, self.context, self.feedback
            )
            package = ogr.Open(str(Path(directory) / "road_minimal_GIS.gpkg"))
            alignments = package.GetLayerByName("alignments")
            self.assertEqual(alignments.GetFeatureCount(), 1)
            alignment = next(iter(alignments))
            self.assertEqual(alignment.GetField("source_vendor"), "Civil 3D")
            self.assertEqual(alignment.GetField("source_name"), "Road A")
            self.assertEqual(alignment.GetField("station_start"), 100)
            self.assertEqual(
                package.GetLayerByName("alignment_elements").GetFeatureCount(), 3
            )
            graph = package.GetLayerByName("profiles")
            self.assertEqual(graph.GetFeatureCount(), 1)
            self.assertIsNone(graph.GetSpatialRef())
            controls = package.GetLayerByName("profile_controls")
            self.assertEqual(controls.GetFeatureCount(), 3)
            package = None


if __name__ == "__main__":
    unittest.main()

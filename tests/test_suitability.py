import unittest

from scene_factory.suitability import screen


class SuitabilityTests(unittest.TestCase):
    def test_named_low_diversity_screen(self):
        names = ["House_Material.006_0", "Land_Material.002_0", "Water_Material.001_0",
                 "Barrel_Material.004_0", "Barrel.001_Material.004_0",
                 "door_Material.006_0", "beam 1_Material.007_0"]
        result = screen({"meshes": [{"name": n} for n in names]})
        self.assertEqual(result["status"], "likely_unsuitable")
        self.assertEqual(result["named_prop_categories"], ["barrel"])

    def test_generic_names_are_not_rejected(self):
        result = screen({"meshes": [{"name": "Cube.001"}, {"name": "Cube.002"},
                                     {"name": "Barrel_Material.004_0"}]})
        self.assertEqual(result["status"], "needs_agent_review")

    def test_namespaced_export_uses_semantic_object_suffix(self):
        result = screen({"meshes": [
            {"name": "Room:polySurface694_Room:House_Floor_Main_0"},
            {"name": "Room:polySurface735_Room:Book_Cook_0"},
            {"name": "Room:polySurface736_Room:Cup_Blue_0"},
        ]})
        self.assertEqual(result["status"], "needs_agent_review")
        self.assertEqual(result["named_prop_categories"], ["book", "cup"])


if __name__ == "__main__":
    unittest.main()

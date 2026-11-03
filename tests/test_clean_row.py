import os
import tempfile
import unittest

from tagger.bigquery_source import MISC_OPTION, clean_row, read_csv


class ReadCsvTest(unittest.TestCase):
    def test_renames_columns_keeps_others_and_drops_blank_rows(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig") as f:
            f.write("sourceRowId,baseValue,options,tag_1\nr1,Titos,A | B,Others: X\n,,,\n")
        try:
            self.assertEqual(read_csv(f.name), [
                {"source_row_id": "r1", "base_value": "Titos", "options": "A | B", "tag_1": "Others: X"}])
        finally:
            os.unlink(f.name)


class CleanRowTest(unittest.TestCase):
    def test_only_misc_or_none_is_tagged_without_candidates(self):
        for raw in ("Misc - Not a Liquor", "None", "Misc - Not a Liquor | None", ""):
            self.assertEqual(clean_row("1", "DeKuyper Triple Sec", raw).options, [MISC_OPTION], raw)

    def test_one_real_candidate_is_invalid(self):
        self.assertIsNone(clean_row("1", "Item", "Only Option"))

    def test_candidates_are_trimmed_and_deduplicated(self):
        self.assertEqual(clean_row("1", "Item", " A | B | A ").options, ["A", "B"])


if __name__ == "__main__":
    unittest.main()

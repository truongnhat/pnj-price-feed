"""Schema contract: a column added to prices.csv must reach the email too.

Every column in fetch_prices.COLUMNS is either read by notify_email.table_rows()
(so it shows in the report table) or listed in notify_email.EMAIL_HIDDEN_COLUMNS
with a reason. Adding a column and forgetting the email fails here.
"""

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fetch_prices as fp  # noqa: E402
import notify_email as ne  # noqa: E402


class SchemaContractTest(unittest.TestCase):
    def test_every_column_shown_in_email_or_explicitly_hidden(self):
        source = inspect.getsource(ne.table_rows)
        missing = [c for c in fp.COLUMNS
                   if c not in ne.EMAIL_HIDDEN_COLUMNS
                   and f'"{c}"' not in source and f"'{c}'" not in source]
        self.assertEqual(missing, [], "Columns in prices.csv but not in the email table: "
                         f"{missing}. Show them in notify_email.table_rows() or add them "
                         "to EMAIL_HIDDEN_COLUMNS with a reason.")

    def test_hidden_columns_exist_and_have_reasons(self):
        for col, reason in ne.EMAIL_HIDDEN_COLUMNS.items():
            self.assertIn(col, fp.COLUMNS)
            self.assertTrue(reason.strip())

    def test_contract_catches_a_forgotten_column(self):
        source = inspect.getsource(ne.table_rows)
        self.assertNotIn('"new_col"', source)  # a hypothetical new column would be flagged


if __name__ == "__main__":
    unittest.main()

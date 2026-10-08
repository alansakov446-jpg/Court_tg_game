import unittest
from unittest.mock import patch

from db.session import database_url


class DatabaseUrlTests(unittest.TestCase):
    def test_neon_libpq_url_is_normalized_for_asyncpg(self):
        raw = (
            "postgresql://user:pw@ep-x.neon.tech/db"
            "?sslmode=require&channel_binding=require"
        )
        self.assertEqual(
            database_url(raw),
            "postgresql+asyncpg://user:pw@ep-x.neon.tech/db?ssl=require",
        )

    def test_postgres_scheme_and_channel_binding_only(self):
        self.assertEqual(
            database_url("postgres://u:p@host/db?channel_binding=require"),
            "postgresql+asyncpg://u:p@host/db",
        )

    def test_asyncpg_url_with_ssl_is_kept(self):
        raw = "postgresql+asyncpg://u:p@host/db?ssl=require"
        self.assertEqual(database_url(raw), raw)

    def test_url_without_query_is_unchanged(self):
        self.assertEqual(
            database_url("postgresql+asyncpg://u:p@localhost:5432/court_test"),
            "postgresql+asyncpg://u:p@localhost:5432/court_test",
        )

    def test_reads_environment_by_default(self):
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://u:p@host/db?sslmode=require&channel_binding=require"},
        ):
            self.assertEqual(
                database_url(), "postgresql+asyncpg://u:p@host/db?ssl=require"
            )


if __name__ == "__main__":
    unittest.main()

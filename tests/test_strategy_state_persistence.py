import unittest

from services.strategy_state_persistence import StrategyStateRepository


class MemoryCollection:
    def __init__(self):
        self.documents = {}
        self.indexes = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    def find_one(self, identity):
        return self.documents.get((identity["trading_date"], identity["underlying"]))

    def update_one(self, identity, update, upsert=False):
        key = (identity["trading_date"], identity["underlying"])
        self.documents.setdefault(key, {}).update(update["$set"])


class StrategyStatePersistenceTests(unittest.TestCase):
    def test_one_collection_scopes_documents_by_date_and_underlying(self):
        collection = MemoryCollection()
        repository = StrategyStateRepository(collection=collection)
        for underlying in ("NIFTY", "SENSEX", "BANKNIFTY"):
            self.assertTrue(repository.save({
                "trading_date": "2026-10-02",
                "underlying": underlying,
                "underlying_instrument_key": f"INDEX|{underlying}",
                "selected_instrument": {"selected": False},
            }))

        self.assertEqual(len(collection.documents), 3)
        self.assertEqual(repository.load("2026-10-02", "SENSEX")["underlying"], "SENSEX")
        unique_indexes = [kwargs for _, kwargs in collection.indexes if kwargs.get("unique")]
        self.assertEqual(len(unique_indexes), 1)
        self.assertEqual(
            collection.indexes[0][0],
            [("trading_date", 1), ("underlying", 1)],
        )

    def test_upsert_replaces_only_the_same_date_underlying_document(self):
        collection = MemoryCollection()
        repository = StrategyStateRepository(collection=collection)
        repository.save({"trading_date": "2026-10-02", "underlying": "NIFTY", "value": 1})
        repository.save({"trading_date": "2026-10-03", "underlying": "NIFTY", "value": 2})
        repository.save({"trading_date": "2026-10-02", "underlying": "SENSEX", "value": 3})
        repository.save({"trading_date": "2026-10-02", "underlying": "NIFTY", "value": 4})

        self.assertEqual(len(collection.documents), 3)
        self.assertEqual(repository.load("2026-10-02", "NIFTY")["value"], 4)
        self.assertEqual(repository.load("2026-10-03", "NIFTY")["value"], 2)

    def test_identity_rejects_non_calendar_dates(self):
        with self.assertRaises(ValueError):
            StrategyStateRepository._identity("2026-99-99", "NIFTY")


if __name__ == "__main__":
    unittest.main()
